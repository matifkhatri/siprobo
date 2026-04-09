#!/usr/bin/env python3
"""
siprobo.caller
==============
High-level SIP auto-dialler: places an outbound call via Asterisk, streams
a WAV file as G.711 over RTP, and hangs up cleanly when playback finishes.

New in v1.1
-----------
- **Codec auto-negotiation** — detects PCMU (PT=0) or PCMA (PT=8) from the
  SDP answer and re-encodes the WAV accordingly.
- **Re-INVITE handling** — responds correctly (100 Trying + 200 OK + SDP)
  when Asterisk sends a mid-call re-INVITE.
- **Transport auto-detection** — ``--transport auto`` probes UDP then TCP.
- **JSON-lines call log** — ``--log-file calls.log`` appends a record per
  call with timestamp, outcome, duration, and DTMF digits received.
- **Retry with exponential backoff** — ``make_call_with_retry()`` /
  ``--max-retries`` CLI flag.
- **Concurrent calls** — ``make_calls_concurrent()`` runs a list of call
  configs in parallel via a thread pool.
- **Graceful KeyboardInterrupt** — sends CANCEL (ringing) or BYE (answered)
  before exiting.
- **Full try/except coverage** throughout every I/O and network path.
- ``CallSession`` class encapsulates all per-call state; multiple instances
  can run concurrently with no shared globals.

Command-line usage
------------------
::

    # Minimal:
    siprobo --host <ip> --phone <number> --wav message.wav \\
            --caller-id 1001 --auth-user 1001 --auth-pass secret

    # With DTMF transfer and retry:
    siprobo --host <ip> --phone <number> --wav msg.wav \\
            --caller-id 1001 --auth-user 1001 --auth-pass secret \\
            --forward-number <target> --forward-digits 0,1 \\
            --transport auto --max-retries 3 --log-file calls.log

Python 3.13+ note
-----------------
``audioop`` was removed in Python 3.13.  Install the drop-in::

    pip install audioop-lts
"""

import argparse
import concurrent.futures
import json
import os
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import wave
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .sip_core import (
    Colors,
    get_local_ip,
    _rand_branch,
    _extract_authenticate_header,
    _build_digest_authorization,
    _build_invite_request,
    _build_200_ok_for_request,
    _parse_sip_headers,
)

# ---------------------------------------------------------------------------
# audioop compatibility shim  (removed in Python 3.13 — audioop-lts replaces it)
# ---------------------------------------------------------------------------
try:
    import audioop as _audioop
except ImportError:
    try:
        import audioop_lts as _audioop          # pip install audioop-lts
    except ImportError:
        _audioop = None


def _require_audioop():
    if _audioop is None:
        raise RuntimeError(
            "The 'audioop' module is not available (removed in Python 3.13).\n"
            "Install the drop-in replacement:  pip install audioop-lts"
        )
    return _audioop


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RTP_PTIME_MS    = 20                                       # packet interval ms
RTP_SAMPLE_RATE = 8000                                     # G.711 sample rate
SAMPLES_PER_PKT = RTP_SAMPLE_RATE * RTP_PTIME_MS // 1000  # 160 bytes/packet

_CODEC_NAMES: Dict[int, str] = {0: "PCMU", 8: "PCMA"}

# RFC 2833 event-code → DTMF character
DTMF_MAP: Dict[int, str] = {
     0: "0",  1: "1",  2: "2",  3: "3",
     4: "4",  5: "5",  6: "6",  7: "7",
     8: "8",  9: "9", 10: "*", 11: "#",
    12: "A", 13: "B", 14: "C", 15: "D",
}

# ---------------------------------------------------------------------------
# ASCII-safe log helpers  (safe on Windows cp1252)
# ---------------------------------------------------------------------------
def _ok(msg):   print(f"  {Colors.GREEN}[OK]  {Colors.END}{msg}")
def _fail(msg): print(f"  {Colors.RED}[ERR] {Colors.END}{msg}")
def _warn(msg): print(f"  {Colors.YELLOW}[!]   {Colors.END}{msg}")
def _info(msg): print(f"  {Colors.BLUE}[i]   {Colors.END}{msg}")


# ---------------------------------------------------------------------------
# CallLogger — JSON-lines file logger
# ---------------------------------------------------------------------------
class CallLogger:
    """
    Appends one JSON record per call to *log_file* after the call ends.

    Thread-safe — multiple ``CallSession`` instances share one logger safely.
    Pass ``log_file=None`` (default) to disable file logging entirely.
    """

    def __init__(self, log_file: Optional[str] = None) -> None:
        self.log_file = log_file
        self._lock    = threading.Lock()

    def log(self, event: dict) -> None:
        if not self.log_file:
            return
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        with self._lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
            except OSError as exc:
                _warn(f"Log write failed ({self.log_file}): {exc}")


# ---------------------------------------------------------------------------
# WAV loading  (returns raw 16-bit PCM; encoding happens after SDP negotiation)
# ---------------------------------------------------------------------------

def _load_wav_as_pcm16(wav_path: str) -> bytes:
    """
    Load *wav_path* and return 16-bit signed PCM at 8 kHz mono.

    Accepts any WAV encoding (8/16/24/32-bit, any sample rate, mono/stereo)
    and converts on-the-fly with ``audioop``.

    Raises
    ------
    RuntimeError
        On WAV read or conversion errors, or missing ``audioop`` module.
    """
    ao = _require_audioop()

    try:
        with wave.open(wav_path, "rb") as wf:
            channels  = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            raw       = wf.readframes(wf.getnframes())
    except (wave.Error, OSError) as exc:
        raise RuntimeError(f"Cannot open WAV '{wav_path}': {exc}") from exc

    dur = len(raw) / max(sampwidth, 1) / max(channels, 1) / max(framerate, 1)
    _info(f"WAV  : {os.path.basename(wav_path)}")
    _info(f"       {framerate} Hz | {channels} ch | {sampwidth * 8}-bit | {dur:.1f} s")

    try:
        if channels == 2:
            raw = ao.tomono(raw, sampwidth, 0.5, 0.5)
        if sampwidth != 2:
            raw       = ao.lin2lin(raw, sampwidth, 2)
            sampwidth = 2
        if framerate != RTP_SAMPLE_RATE:
            raw, _ = ao.ratecv(raw, sampwidth, 1, framerate, RTP_SAMPLE_RATE, None)
            _info(f"       Resampled {framerate} Hz -> {RTP_SAMPLE_RATE} Hz")
    except Exception as exc:
        raise RuntimeError(f"WAV conversion error: {exc}") from exc

    _info(f"       PCM16 : {len(raw)} bytes (~{len(raw) / 2 / RTP_SAMPLE_RATE:.1f} s)")
    return raw


def _encode_pcm(pcm16: bytes, payload_type: int) -> bytes:
    """
    Encode 16-bit 8 kHz mono PCM to the RTP codec specified by
    *payload_type* (0 = PCMU / mu-law, 8 = PCMA / A-law).

    Raises
    ------
    RuntimeError
        On encoding failure or unsupported payload type.
    """
    ao    = _require_audioop()
    codec = _CODEC_NAMES.get(payload_type, f"PT{payload_type}")
    try:
        encoded = ao.lin2alaw(pcm16, 2) if payload_type == 8 else ao.lin2ulaw(pcm16, 2)
    except Exception as exc:
        raise RuntimeError(f"PCM encode error ({codec}): {exc}") from exc
    _info(f"       {codec}  : {len(encoded)} bytes (~{len(encoded) / RTP_SAMPLE_RATE:.1f} s)")
    return encoded


# ---------------------------------------------------------------------------
# RTP helpers
# ---------------------------------------------------------------------------

def _build_rtp_packet(
    payload: bytes, seq: int, ts: int, ssrc: int, payload_type: int = 0
) -> bytes:
    """Minimal RTP/3550 header + *payload*; *payload_type* sets the PT field."""
    return struct.pack(
        "!BBHII",
        0x80,                    # V=2, P=0, X=0, CC=0
        payload_type & 0x7F,     # M=0, PT
        seq  & 0xFFFF,
        ts   & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    ) + payload


def _parse_rtp(data: bytes) -> Tuple[Optional[int], Optional[bytes], Optional[int]]:
    """Unpack raw UDP packet; returns ``(payload_type, payload, timestamp)``."""
    if len(data) < 12:
        return None, None, None
    pt = data[1] & 0x7F
    ts = struct.unpack("!I", data[4:8])[0]
    return pt, data[12:], ts


# ---------------------------------------------------------------------------
# SDP helpers
# ---------------------------------------------------------------------------

def _parse_sdp_rtp(sip_msg: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract the remote audio RTP ``(ip, port)`` from the SDP body."""
    ip, port, in_body = None, None, False
    for line in sip_msg.split("\r\n"):
        if not in_body:
            if line == "":
                in_body = True
            continue
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        if line.startswith("m=audio "):
            try:
                port = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return ip, port


def _negotiate_codec(sip_msg: str) -> Tuple[int, str]:
    """
    Detect the codec chosen by the server from the SDP answer.

    The first payload type listed in the ``m=audio`` line that maps to a
    supported G.711 codec (PT 0 = PCMU, PT 8 = PCMA) is selected.

    Returns
    -------
    (payload_type, codec_name) — defaults to ``(0, "PCMU")`` on failure.
    """
    in_body = False
    for line in sip_msg.split("\r\n"):
        if not in_body:
            if line == "":
                in_body = True
            continue
        if line.startswith("m=audio "):
            parts = line.split()           # m=audio <port> RTP/AVP <pt1> ...
            for pt_str in parts[3:]:
                try:
                    pt = int(pt_str)
                    if pt in _CODEC_NAMES:
                        return pt, _CODEC_NAMES[pt]
                except ValueError:
                    continue
    return 0, "PCMU"                       # safe default


# ---------------------------------------------------------------------------
# SIP header parsers
# ---------------------------------------------------------------------------

def _extract_to_tag(sip_msg: str) -> str:
    """Return the ``tag=`` value from the To: header, or ``""``."""
    for line in sip_msg.split("\r\n"):
        if line.lower().startswith("to:"):
            m = re.search(r"tag=([^\s;>,\r\n]+)", line, re.IGNORECASE)
            if m:
                return m.group(1)
    return ""


def _get_cseq_method(sip_msg: str) -> str:
    """Return the method token from the CSeq header (e.g. ``'INVITE'``)."""
    for line in sip_msg.split("\r\n"):
        if line.lower().startswith("cseq:"):
            parts = line.split(":", 1)[1].strip().split()
            if len(parts) >= 2:
                return parts[-1].upper()
    return ""


# ---------------------------------------------------------------------------
# SIP in-dialog message builders
# ---------------------------------------------------------------------------

def _build_ack(
    host, port, exten, local_ip, local_port,
    call_id, from_tag, to_tag, from_user, cseq,
) -> str:
    return (
        f"ACK sip:{exten}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={_rand_branch()};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={from_tag}\r\n'
        f"To: <sip:{exten}@{host}>;tag={to_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} ACK\r\n"
        f"Max-Forwards: 70\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_bye(
    host, port, exten, local_ip, local_port,
    call_id, from_tag, to_tag, from_user, cseq,
) -> str:
    """In-dialog BYE — To-tag is mandatory for Asterisk to match the dialog."""
    return (
        f"BYE sip:{exten}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={_rand_branch()};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={from_tag}\r\n'
        f"To: <sip:{exten}@{host}>;tag={to_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} BYE\r\n"
        f"Max-Forwards: 70\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_refer(
    host, port, exten, local_ip, local_port,
    call_id, from_tag, to_tag, from_user, cseq,
    refer_to_uri: str,
) -> str:
    """SIP REFER — instructs Asterisk to blind-transfer the active call."""
    return (
        f"REFER sip:{exten}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={_rand_branch()};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={from_tag}\r\n'
        f"To: <sip:{exten}@{host}>;tag={to_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} REFER\r\n"
        f"Contact: <sip:{from_user}@{local_ip}:{local_port}>\r\n"
        f"Max-Forwards: 70\r\n"
        f"Refer-To: <{refer_to_uri}>\r\n"
        f"Referred-By: <sip:{from_user}@{local_ip}>\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_100trying(request_str: str) -> str:
    """Build a 100 Trying response that mirrors the request's Via/From/To/CSeq."""
    hdrs    = _parse_sip_headers(request_str)
    via     = hdrs.get("via", "")
    from_h  = hdrs.get("from", "")
    to_h    = hdrs.get("to", "")
    call_id = hdrs.get("call-id", "")
    cseq    = hdrs.get("cseq", "")
    return (
        "SIP/2.0 100 Trying\r\n"
        f"Via: {via}\r\n"
        f"From: {from_h}\r\n"
        f"To: {to_h}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _build_200ok_for_reinvite(
    request_str: str, local_ip: str, local_port: int, from_user: str, sdp: str
) -> str:
    """
    200 OK for a server-initiated re-INVITE, including our SDP so that
    Asterisk can update the media path or codec selection.
    """
    hdrs    = _parse_sip_headers(request_str)
    via     = hdrs.get("via", "")
    from_h  = hdrs.get("from", "")
    to_h    = hdrs.get("to", "")
    call_id = hdrs.get("call-id", "")
    cseq    = hdrs.get("cseq", "")
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {via}\r\n"
        f"From: {from_h}\r\n"
        f"To: {to_h}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        f"Contact: <sip:{from_user}@{local_ip}:{local_port}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n"
        f"{sdp}"
    )


# ---------------------------------------------------------------------------
# Transport auto-detection
# ---------------------------------------------------------------------------

def _auto_detect_transport(host: str, port: int) -> str:
    """
    Probe *host:port* to choose the best SIP transport.

    1. Sends a minimal UDP packet and waits up to 2 s for any response.
       If one arrives, ``"udp"`` is returned.
    2. Falls back to a TCP connect attempt (3 s timeout).
       If successful, ``"tcp"`` is returned.
    3. Defaults to ``"udp"`` if both probes are inconclusive.
    """
    _info("Transport auto-detect: probing UDP ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        probe = (
            b"OPTIONS sip:probe SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 0.0.0.0:0;branch=z9hG4bKprobe\r\n"
            b"Max-Forwards: 1\r\nContent-Length: 0\r\n\r\n"
        )
        sock.sendto(probe, (host, port))
        try:
            sock.recvfrom(512)
            sock.close()
            _info("Transport auto-detect: UDP responded -> using UDP")
            return "udp"
        except socket.timeout:
            sock.close()
    except Exception:
        pass

    _info("Transport auto-detect: probing TCP ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((host, port))
        sock.close()
        _info("Transport auto-detect: TCP reachable -> using TCP")
        return "tcp"
    except Exception:
        pass

    _warn("Transport auto-detect: both probes inconclusive — defaulting to UDP")
    return "udp"


# ---------------------------------------------------------------------------
# RTP streaming thread  (WAV → G.711)
# ---------------------------------------------------------------------------

def _stream_rtp(
    rtp_sock:     socket.socket,
    remote_ip:    str,
    remote_port:  int,
    audio_data:   bytes,
    payload_type: int,
    stop_event:   threading.Event,
    on_finish:    Callable,
    verbose:      bool,
) -> None:
    """
    Send *audio_data* (PCMU or PCMA) as an RTP stream at 20 ms ptime.

    Calls *on_finish()* when the full payload has been transmitted and
    *stop_event* has not been set.
    """
    ssrc       = random.randint(0, 0xFFFFFFFF)
    seq        = random.randint(0, 0xFFFF)
    ts         = random.randint(0, 0xFFFFFFFF)
    # G.711 silence: 0xFF for PCMU (mu-law), 0xD5 for PCMA (A-law)
    sil_byte   = 0xD5 if payload_type == 8 else 0xFF
    silence    = bytes([sil_byte]) * SAMPLES_PER_PKT
    offset     = 0
    total      = len(audio_data)
    next_send  = time.time()
    codec      = _CODEC_NAMES.get(payload_type, f"PT{payload_type}")

    if verbose:
        _info(f"RTP -> {remote_ip}:{remote_port} [{codec}] ({total / RTP_SAMPLE_RATE:.1f} s)")

    while offset < total and not stop_event.is_set():
        chunk = audio_data[offset: offset + SAMPLES_PER_PKT]
        if len(chunk) < SAMPLES_PER_PKT:
            chunk = chunk + silence[len(chunk):]

        try:
            rtp_sock.sendto(
                _build_rtp_packet(chunk, seq, ts, ssrc, payload_type),
                (remote_ip, remote_port),
            )
        except OSError as exc:
            if not stop_event.is_set() and verbose:
                _warn(f"RTP send error: {exc}")
            break
        except Exception as exc:
            if not stop_event.is_set() and verbose:
                _warn(f"RTP unexpected error: {exc}")
            break

        seq        = (seq + 1) & 0xFFFF
        ts         = (ts  + SAMPLES_PER_PKT) & 0xFFFFFFFF
        offset    += SAMPLES_PER_PKT
        next_send += RTP_PTIME_MS / 1000.0
        sleep_for  = next_send - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)

    if not stop_event.is_set():
        _ok("WAV playback complete — hanging up.")
        try:
            on_finish()
        except Exception as exc:
            _warn(f"on_finish callback error: {exc}")


# ---------------------------------------------------------------------------
# RTP listener thread  (RFC 2833 DTMF)
# ---------------------------------------------------------------------------

def _rtp_listener(
    rtp_sock:      socket.socket,
    stop_event:    threading.Event,
    dtmf_callback: Callable,
    verbose:       bool,
) -> None:
    """
    Receive incoming RTP and fire *dtmf_callback(digit)* for every unique
    RFC 2833 telephone-event (PT=101) end-packet, de-duplicated by timestamp.
    """
    seen_ts: set = set()

    while not stop_event.is_set():
        try:
            ready, _, _ = select.select([rtp_sock], [], [], 0.3)
            if not ready:
                continue
            data, _ = rtp_sock.recvfrom(2048)
        except OSError:
            break
        except Exception:
            continue

        try:
            pt, payload, rtp_ts = _parse_rtp(data)
            if pt != 101 or not payload or len(payload) < 4:
                continue

            event_code = payload[0]
            is_end     = bool(payload[1] & 0x80)
            digit      = DTMF_MAP.get(event_code)

            if digit and is_end and rtp_ts not in seen_ts:
                seen_ts.add(rtp_ts)
                if len(seen_ts) > 500:     # prevent unbounded growth
                    seen_ts.pop()
                if verbose:
                    _info(f"DTMF event: '{digit}'  (PT=101, ts={rtp_ts})")
                try:
                    dtmf_callback(digit)
                except Exception as exc:
                    if verbose:
                        _warn(f"DTMF callback error: {exc}")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# CallSession — full per-call state machine
# ---------------------------------------------------------------------------

class CallSession:
    """
    Manages a single outbound SIP call end-to-end.

    All state is instance-local — multiple ``CallSession`` objects can
    run concurrently in different threads with no shared globals.
    """

    def __init__(
        self, *,
        host:           str,
        port:           int,
        phone:          str,
        transport:      str,
        source_ip:      Optional[str],
        caller_id:      str,
        auth_user:      str,
        auth_pass:      str,
        dial_prefix:    str,
        wav_path:       str,
        forward_number: str,
        forward_digits: List[str],
        verbose:        bool,
        ring_timeout:   int,
        logger:         Optional[CallLogger] = None,
    ) -> None:
        # --- Configuration --------------------------------------------------
        self.host           = host
        self.port           = port
        self.phone          = phone
        self.transport      = transport
        self.source_ip      = source_ip
        self.from_user      = caller_id or "siprobo"
        self.auth_user      = auth_user  or ""
        self.auth_pass      = auth_pass  or ""
        self.dial_prefix    = dial_prefix or ""
        self.wav_path       = wav_path
        self.forward_number = forward_number or ""
        self.forward_digits = forward_digits or []
        self.verbose        = verbose
        self.ring_timeout   = ring_timeout
        self.logger         = logger or CallLogger()

        # --- Derived identity -----------------------------------------------
        self.local_ip    = get_local_ip(host, port, source_ip)
        self.local_port  = random.randint(10_000, 19_999)
        self.rtp_port    = random.randint(20_000, 29_999)
        self.call_id     = f"{random.randint(100_000, 999_999)}@{self.local_ip}"
        self.from_tag    = str(random.randint(100_000, 999_999))
        self.exten       = f"{self.dial_prefix}{self.phone}"
        self.request_uri = f"sip:{self.exten}@{host}:{port}"
        self.to_uri      = f"sip:{self.exten}@{host}"

        # --- Call state -----------------------------------------------------
        self.to_tag           = ""
        self.cseq             = 1
        self.active_cseq      = 1
        self.auth_tried       = False
        self.answered         = False
        self.transferred      = False
        self.bye_sent         = False
        self.bye_sent_at      = 0.0
        self.rtp_payload_type = 0     # 0=PCMU, 8=PCMA — set after SDP negotiation

        # --- Metadata (for logging) -----------------------------------------
        self._start_time:   float      = 0.0
        self._answer_time:  float      = 0.0
        self._dtmf_received: List[str] = []

        # --- Threading ------------------------------------------------------
        self._stop_rtp      = threading.Event()
        self._stop_listen   = threading.Event()
        self._sip_done      = threading.Event()
        self._cseq_lock     = threading.Lock()
        self._rtp_thread:    Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._sip_sock:      Optional[socket.socket]    = None
        self._rtp_sock:      Optional[socket.socket]    = None

        # --- Audio ----------------------------------------------------------
        self._raw_pcm:       Optional[bytes] = None   # 16-bit 8 kHz mono PCM
        self._encoded_audio: Optional[bytes] = None   # PCMU or PCMA

        # --- SDP (built once, reused for re-INVITE responses) ---------------
        self._sdp = self._make_sdp()

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _make_sdp(self) -> str:
        return (
            f"v=0\r\n"
            f"o=- {random.randint(1000, 9999)} 1 IN IP4 {self.local_ip}\r\n"
            f"s=siprobo\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-16\r\n"
            f"a=sendrecv\r\n"
        )

    def _next_cseq(self) -> int:
        with self._cseq_lock:
            self.cseq += 1
            return self.cseq

    def _log(self, status: str = "", **kwargs) -> None:
        self.logger.log({
            "phone":     self.phone,
            "host":      self.host,
            "caller_id": self.from_user,
            "status":    status,
            **kwargs,
        })

    # -------------------------------------------------------------------------
    # Public run
    # -------------------------------------------------------------------------

    def run(self) -> bool:
        """
        Execute the call.  Returns ``True`` if the call was answered.
        Safe to call from any thread.
        """
        self._start_time = time.time()

        # 1. Load WAV --------------------------------------------------------
        if not os.path.isfile(self.wav_path):
            _fail(f"WAV not found: {self.wav_path}")
            self._log("error", error="wav_not_found")
            return False

        try:
            print()
            self._raw_pcm = _load_wav_as_pcm16(self.wav_path)
            print()
        except Exception as exc:
            _fail(f"WAV load error: {exc}")
            self._log("error", error=str(exc))
            return False

        # 2. Open sockets ----------------------------------------------------
        bind_ip = self.local_ip if self.source_ip else ""

        try:
            self._sip_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sip_sock.settimeout(2)
            self._sip_sock.bind((bind_ip, self.local_port))
        except OSError as exc:
            _fail(f"SIP socket bind {bind_ip}:{self.local_port} failed: {exc}")
            self._log("error", error=f"sip_bind: {exc}")
            return False

        try:
            self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rtp_sock.bind((bind_ip, self.rtp_port))
        except OSError as exc:
            _fail(f"RTP socket bind {bind_ip}:{self.rtp_port} failed: {exc}")
            self._log("error", error=f"rtp_bind: {exc}")
            try:
                self._sip_sock.close()
            except Exception:
                pass
            return False

        # 3. Send initial INVITE ---------------------------------------------
        try:
            self._do_invite()
        except Exception as exc:
            _fail(f"INVITE failed: {exc}")
            self._log("error", error=f"invite: {exc}")
            self._cleanup()
            return False

        # 4. SIP receive loop ------------------------------------------------
        ring_start = time.time()
        try:
            while not self._sip_done.is_set():

                if not self.answered and (time.time() - ring_start) > self.ring_timeout:
                    _warn(f"No answer within {self.ring_timeout} s — giving up.")
                    break

                if self.bye_sent and (time.time() - self.bye_sent_at) > 8:
                    _ok("BYE timeout — assuming call ended on server.")
                    break

                try:
                    data, _ = self._sip_sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._sip_done.is_set():
                        _fail(f"SIP recv error: {exc}")
                    break

                try:
                    self._dispatch(data.decode("utf-8", errors="ignore"))
                except Exception as exc:
                    if self.verbose:
                        _warn(f"Dispatch error: {exc}")
                    continue

        except KeyboardInterrupt:
            _warn("Interrupted — terminating call ...")
            try:
                if self.answered:
                    self.send_bye()
                else:
                    self._send_cancel()
                time.sleep(0.8)
            except Exception:
                pass

        finally:
            self._cleanup()

        # 5. Log outcome -----------------------------------------------------
        duration = (time.time() - self._answer_time) if self.answered else 0.0
        self._log(
            status         = "answered" if self.answered else "no_answer",
            duration_s     = round(duration, 2),
            dtmf_received  = self._dtmf_received,
            transferred_to = self.forward_number if self.transferred else None,
        )
        return self.answered

    # -------------------------------------------------------------------------
    # SIP message dispatch
    # -------------------------------------------------------------------------

    def _dispatch(self, msg: str) -> None:
        """Route an incoming SIP message to the appropriate handler."""
        first_line = msg.split("\r\n")[0].strip()
        method     = _get_cseq_method(msg)

        if self.verbose:
            print(f"\n{Colors.CYAN}<<< {first_line}{Colors.END}")

        # Digest auth challenge
        sc, hdr_name, hdr_value = _extract_authenticate_header(msg)
        if not self.auth_tried and sc in (401, 407) and hdr_name and hdr_value:
            self._handle_auth(hdr_name, hdr_value)
            return

        # Provisional
        if "SIP/2.0 100" in first_line:
            print(f"  {Colors.BLUE}...{Colors.END}  100 Trying")
            return
        if "SIP/2.0 180" in first_line:
            print(f"  {Colors.GREEN}~~~{Colors.END}  180 Ringing")
            return
        if "SIP/2.0 183" in first_line:
            print(f"  {Colors.GREEN}~~~{Colors.END}  183 Session Progress")
            return

        # 200 OK to INVITE — call answered
        if "SIP/2.0 200" in first_line and method == "INVITE" and not self.answered:
            self._on_answer(msg)
            return

        # 200 OK to BYE
        if "SIP/2.0 200" in first_line and method == "BYE":
            _ok("BYE acknowledged — call ended cleanly.")
            self._sip_done.set()
            return

        # 200 OK / 202 to REFER
        if "SIP/2.0 200" in first_line and method == "REFER":
            _ok("REFER acknowledged.")
            return
        if "SIP/2.0 202" in first_line:
            _ok("Transfer accepted — waiting for NOTIFY ...")
            return

        # NOTIFY (transfer progress)
        if msg.startswith("NOTIFY "):
            self._on_notify(msg)
            return

        # BYE from server
        if msg.startswith("BYE "):
            self._on_server_bye(msg)
            return

        # Mid-call re-INVITE from server
        if msg.startswith("INVITE ") and self.answered:
            self._on_reinvite(msg)
            return

        # 481 — dialog already cleared (harmless after BYE)
        if "SIP/2.0 481" in first_line and self.bye_sent:
            _ok("Call ended (481 — dialog cleared on server).")
            self._sip_done.set()
            return

        # 4xx / 5xx / 6xx errors
        if re.match(r"SIP/2\.0 [456]\d\d", first_line):
            _fail(f"SIP error: {first_line}")
            self._stop_rtp.set()
            self._stop_listen.set()
            self._sip_done.set()

    # -------------------------------------------------------------------------
    # SIP event handlers
    # -------------------------------------------------------------------------

    def _handle_auth(self, hdr_name: str, hdr_value: str) -> None:
        """Retry the INVITE with Digest credentials after a 401/407."""
        self.auth_tried = True
        is_proxy        = hdr_name == "proxy-authenticate"
        _warn("Auth challenge — retrying with credentials ...")
        try:
            auth_hdr = _build_digest_authorization(
                hdr_value, "INVITE", self.request_uri,
                username=self.auth_user, password=self.auth_pass,
                is_proxy=is_proxy,
            )
            self.cseq       += 1
            self.active_cseq = self.cseq
            self._do_invite(auth_header=auth_hdr)
        except Exception as exc:
            _fail(f"Auth retry failed: {exc}")

    def _on_answer(self, msg: str) -> None:
        """200 OK to INVITE — negotiate codec, encode audio, start media."""
        self.answered     = True
        self._answer_time = time.time()
        print(f"  {Colors.GREEN}[OK]{Colors.END}  200 OK — call answered!")

        # Extract To-tag (critical for in-dialog BYE / REFER)
        self.to_tag = _extract_to_tag(msg)
        if self.verbose:
            _info(f"To-tag: '{self.to_tag}'")
        if not self.to_tag:
            _warn("No To-tag in 200 OK — BYE may not match dialog on server")

        # Parse remote RTP endpoint
        rem_ip, rem_port = _parse_sdp_rtp(msg)
        if not rem_ip or not rem_port:
            rem_ip, rem_port = self.host, self.rtp_port
            _warn(f"SDP parse failed — fallback RTP: {rem_ip}:{rem_port}")
        else:
            _info(f"Remote RTP: {rem_ip}:{rem_port}")

        # Auto-negotiate codec from SDP answer
        self.rtp_payload_type, codec_name = _negotiate_codec(msg)
        _info(f"Codec     : {codec_name} (PT={self.rtp_payload_type})")

        # Encode the pre-loaded PCM with the negotiated codec
        try:
            self._encoded_audio = _encode_pcm(self._raw_pcm, self.rtp_payload_type)
        except Exception as exc:
            _fail(f"Audio encode failed: {exc}")
            self.send_bye()
            return

        # Send ACK
        try:
            ack = _build_ack(
                self.host, self.port, self.exten,
                self.local_ip, self.local_port,
                self.call_id, self.from_tag, self.to_tag,
                self.from_user, self.active_cseq,
            )
            self._sip_sock.sendto(ack.encode(), (self.host, self.port))
            print(f"  {Colors.BLUE}>>>{Colors.END}  ACK sent")
        except Exception as exc:
            _fail(f"ACK send error: {exc}")

        # Start RTP streaming thread
        print()
        _info(f"Playing : {os.path.basename(self.wav_path)}")
        self._rtp_thread = threading.Thread(
            target=_stream_rtp,
            args=(
                self._rtp_sock, rem_ip, rem_port,
                self._encoded_audio, self.rtp_payload_type,
                self._stop_rtp, self.send_bye, self.verbose,
            ),
            daemon=True,
        )
        self._rtp_thread.start()

        # Start DTMF listener thread
        if self.forward_digits and self.forward_number:
            _info(f"DTMF    : press {self.forward_digits} -> forward to {self.forward_number}")
        self._listen_thread = threading.Thread(
            target=_rtp_listener,
            args=(self._rtp_sock, self._stop_listen, self._on_dtmf, self.verbose),
            daemon=True,
        )
        self._listen_thread.start()

    def _on_reinvite(self, msg: str) -> None:
        """
        Handle a mid-call re-INVITE from the server.

        Responds with 100 Trying + 200 OK (with our SDP).
        If the server changed its codec offer, the audio is re-encoded.
        """
        if self.verbose:
            _info("Re-INVITE received from server")

        try:
            self._sip_sock.sendto(
                _build_100trying(msg).encode(), (self.host, self.port)
            )
        except Exception as exc:
            _warn(f"Re-INVITE 100 Trying error: {exc}")

        try:
            ok200 = _build_200ok_for_reinvite(
                msg, self.local_ip, self.local_port, self.from_user, self._sdp
            )
            self._sip_sock.sendto(ok200.encode(), (self.host, self.port))
            _info("Re-INVITE: replied 200 OK + SDP")
        except Exception as exc:
            _warn(f"Re-INVITE 200 OK error: {exc}")

        # Re-negotiate codec if the offer changed
        new_pt, new_codec = _negotiate_codec(msg)
        if new_pt != self.rtp_payload_type:
            old_codec = _CODEC_NAMES.get(self.rtp_payload_type, "?")
            _info(f"Re-INVITE: codec change {old_codec} -> {new_codec}")
            self.rtp_payload_type = new_pt
            try:
                self._encoded_audio = _encode_pcm(self._raw_pcm, new_pt)
            except Exception as exc:
                _warn(f"Re-encode for {new_codec} failed: {exc}")

        # Log RTP endpoint change if present
        new_ip, new_port = _parse_sdp_rtp(msg)
        if new_ip and new_port and self.verbose:
            _info(f"Re-INVITE: remote RTP -> {new_ip}:{new_port}")

    def _on_notify(self, msg: str) -> None:
        """Handle NOTIFY for blind-transfer progress."""
        try:
            self._sip_sock.sendto(
                _build_200_ok_for_request(msg).encode(), (self.host, self.port)
            )
        except Exception as exc:
            _warn(f"NOTIFY 200 OK error: {exc}")

        if self.verbose:
            _info("NOTIFY received -> 200 OK sent")

        if "SIP/2.0 200" in msg and "Subscription-State: terminated" in msg:
            _ok("Transfer complete — remote connected to forward target.")
            self._stop_listen.set()
            self._stop_rtp.set()
            self.send_bye()

    def _on_server_bye(self, msg: str) -> None:
        """Handle BYE from the server (remote hang-up)."""
        _info("Server sent BYE — remote party hung up.")
        try:
            self._sip_sock.sendto(
                _build_200_ok_for_request(msg).encode(), (self.host, self.port)
            )
        except Exception as exc:
            _warn(f"BYE 200 OK error: {exc}")
        self._stop_rtp.set()
        self._stop_listen.set()
        self._sip_done.set()

    def _on_dtmf(self, digit: str) -> None:
        """Callback invoked by the DTMF listener thread."""
        print()
        _info(f"DTMF  : '{digit}' pressed by remote party")
        self._dtmf_received.append(digit)
        if digit in self.forward_digits:
            if self.forward_number:
                _ok(f"Digit '{digit}' matched — forwarding to {self.forward_number} ...")
                self.send_refer()
            else:
                _info("Digit matched but --forward-number is not configured")
        else:
            _info(f"Digit '{digit}' not in trigger list {self.forward_digits} — ignored")

    # -------------------------------------------------------------------------
    # SIP senders
    # -------------------------------------------------------------------------

    def _do_invite(self, auth_header: str = "") -> None:
        """Build and transmit the INVITE (or auth-retry INVITE)."""
        invite = _build_invite_request(
            host=self.host, port=self.port, transport=self.transport,
            request_uri=self.request_uri, to_uri=self.to_uri,
            local_ip=self.local_ip, local_port=self.local_port,
            caller_id=self.from_user, call_id=self.call_id,
            branch=_rand_branch(), tag=self.from_tag,
            cseq=self.cseq, sdp=self._sdp, auth_header=auth_header,
        )
        self._sip_sock.sendto(invite.encode(), (self.host, self.port))
        if self.verbose:
            print(f"\n{Colors.CYAN}--- INVITE (CSeq {self.cseq}) ---\n{invite[:700]}{Colors.END}")
        _info(f"INVITE -> sip:{self.exten}@{self.host}:{self.port}")
        _info(f"Local SIP : {self.local_ip}:{self.local_port}")
        _info(f"Local RTP : {self.local_ip}:{self.rtp_port}")
        print()

    def send_bye(self) -> None:
        """Send in-dialog BYE.  Idempotent — safe to call multiple times."""
        if self.bye_sent:
            return
        self.bye_sent    = True
        self.bye_sent_at = time.time()
        c   = self._next_cseq()
        msg = _build_bye(
            self.host, self.port, self.exten,
            self.local_ip, self.local_port,
            self.call_id, self.from_tag, self.to_tag,
            self.from_user, c,
        )
        try:
            self._sip_sock.sendto(msg.encode(), (self.host, self.port))
            if self.verbose:
                print(f"\n{Colors.CYAN}--- BYE (CSeq {c}) ---\n{msg}{Colors.END}")
            else:
                _info(f"BYE sent (CSeq {c}) — ending call.")
        except Exception as exc:
            _warn(f"BYE send error: {exc}")
        self._stop_rtp.set()
        self._stop_listen.set()

    def send_refer(self) -> None:
        """Send SIP REFER for a blind transfer.  Idempotent."""
        if self.transferred or not self.forward_number:
            return
        self.transferred = True
        c         = self._next_cseq()
        refer_uri = f"sip:{self.forward_number}@{self.host}"
        msg = _build_refer(
            self.host, self.port, self.exten,
            self.local_ip, self.local_port,
            self.call_id, self.from_tag, self.to_tag,
            self.from_user, c, refer_uri,
        )
        try:
            self._sip_sock.sendto(msg.encode(), (self.host, self.port))
            if self.verbose:
                print(f"\n{Colors.CYAN}--- REFER (CSeq {c}) ---\n{msg}{Colors.END}")
            _ok(f"REFER sent — transferring to {self.forward_number}")
        except Exception as exc:
            _fail(f"REFER send error: {exc}")
        self._stop_rtp.set()

    def _send_cancel(self) -> None:
        """CANCEL an unanswered (ringing) call."""
        cancel = (
            f"CANCEL sip:{self.exten}@{self.host}:{self.port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port}"
            f";branch={_rand_branch()};rport\r\n"
            f'From: "{self.from_user}" <sip:{self.from_user}@{self.local_ip}>;tag={self.from_tag}\r\n'
            f"To: <sip:{self.exten}@{self.host}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.active_cseq} CANCEL\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        try:
            self._sip_sock.sendto(cancel.encode(), (self.host, self.port))
            _info("CANCEL sent")
        except Exception as exc:
            _warn(f"CANCEL send error: {exc}")

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Stop threads and close all sockets."""
        self._stop_rtp.set()
        self._stop_listen.set()
        for t, timeout in [(self._rtp_thread, 3), (self._listen_thread, 2)]:
            if t:
                try:
                    t.join(timeout=timeout)
                except Exception:
                    pass
        for sock in (self._sip_sock, self._rtp_sock):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        print()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_call(
    *,
    host:           str,
    port:           int           = 5060,
    phone:          str,
    transport:      str           = "udp",
    source_ip:      Optional[str] = None,
    caller_id:      str           = "",
    auth_user:      str           = "",
    auth_pass:      str           = "",
    dial_prefix:    str           = "",
    wav_path:       str,
    forward_number: str           = "",
    forward_digits: Optional[List[str]] = None,
    verbose:        bool          = False,
    ring_timeout:   int           = 60,
    log_file:       Optional[str] = None,
) -> bool:
    """
    Place a single SIP call, stream *wav_path*, and hang up when done.

    Parameters
    ----------
    host
        Asterisk IP or hostname.
    port
        SIP port (default 5060).
    phone
        Destination number or extension.
    transport
        ``"udp"``, ``"tcp"``, or ``"auto"`` (probe the server first).
    source_ip
        Local IP to bind; ``None`` = auto-detect.
    caller_id
        SIP From extension / caller-ID.
    auth_user
        Digest auth username (empty = no auth).
    auth_pass
        Digest auth password (empty = no auth).
    dial_prefix
        Prefix prepended to *phone* in the Request-URI.
    wav_path
        Path to the WAV file to play (any format accepted).
    forward_number
        Blind-transfer target when a matching DTMF digit is pressed.
    forward_digits
        List of digit strings (e.g. ``["0", "1"]``) that trigger transfer.
    verbose
        Print raw SIP messages and RTP diagnostics.
    ring_timeout
        Seconds to wait for an answer before giving up (default 60).
    log_file
        Path to a JSON-lines call log; ``None`` disables logging.

    Returns
    -------
    bool
        ``True`` if the call was answered.
    """
    if transport == "auto":
        transport = _auto_detect_transport(host, port)

    return CallSession(
        host           = host,
        port           = port,
        phone          = phone,
        transport      = transport,
        source_ip      = source_ip,
        caller_id      = caller_id,
        auth_user      = auth_user,
        auth_pass      = auth_pass,
        dial_prefix    = dial_prefix,
        wav_path       = wav_path,
        forward_number = forward_number,
        forward_digits = forward_digits or [],
        verbose        = verbose,
        ring_timeout   = ring_timeout,
        logger         = CallLogger(log_file),
    ).run()


def make_call_with_retry(
    *,
    max_retries:    int   = 3,
    retry_delay:    float = 5.0,
    backoff_factor: float = 2.0,
    **call_kwargs,
) -> bool:
    """
    Call :func:`make_call` up to *max_retries* times with exponential backoff.

    Parameters
    ----------
    max_retries
        Maximum number of attempts (default 3).
    retry_delay
        Initial wait between attempts in seconds (default 5.0).
    backoff_factor
        Multiplier applied to *retry_delay* after each attempt (default 2.0).
        Example: delay=5, factor=2 → waits 5 s, 10 s, 20 s, …
    **call_kwargs
        All keyword arguments accepted by :func:`make_call`.

    Returns
    -------
    bool
        ``True`` if any attempt was answered.
    """
    for attempt in range(1, max_retries + 1):
        _info(f"Attempt {attempt}/{max_retries} ...")
        try:
            if make_call(**call_kwargs):
                return True
            _warn(f"Attempt {attempt}/{max_retries}: call did not connect")
        except Exception as exc:
            _warn(f"Attempt {attempt}/{max_retries} error: {exc}")

        if attempt < max_retries:
            delay = retry_delay * (backoff_factor ** (attempt - 1))
            _info(f"Retrying in {delay:.1f} s ...")
            time.sleep(delay)

    _fail(f"All {max_retries} attempts failed.")
    return False


def make_calls_concurrent(
    call_configs: List[dict],
    max_workers:  int = 10,
) -> List[bool]:
    """
    Run multiple calls in parallel using a thread pool.

    Parameters
    ----------
    call_configs
        List of dicts; each dict contains keyword arguments for
        :func:`make_call` (``host``, ``phone``, ``wav_path``, …).
    max_workers
        Maximum concurrent calls (default 10).

    Returns
    -------
    List[bool]
        One result per entry in *call_configs* — ``True`` = answered.

    Example
    -------
    >>> results = make_calls_concurrent([
    ...     {"host": "192.0.2.10", "phone": "1001", "wav_path": "msg.wav",
    ...      "caller_id": "100", "auth_user": "100", "auth_pass": "secret"},
    ...     {"host": "192.0.2.10", "phone": "1002", "wav_path": "msg.wav",
    ...      "caller_id": "100", "auth_user": "100", "auth_pass": "secret"},
    ... ])
    """
    results: List[bool] = [False] * len(call_configs)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(make_call, **cfg): idx
            for idx, cfg in enumerate(call_configs)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                _warn(f"call_configs[{idx}] raised: {exc}")
                results[idx] = False

    return results


# ---------------------------------------------------------------------------
# CLI entry point  (siprobo)
# ---------------------------------------------------------------------------

def main(argv=None):
    """Entry point for the ``siprobo`` command."""
    parser = argparse.ArgumentParser(
        prog="siprobo",
        description="SIP auto-dialler: call, play WAV, hang up, optionally transfer on DTMF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Minimal:
  siprobo --host 192.0.2.10 --phone 15551234567 --wav message.wav \\
          --caller-id 1001 --auth-user 1001 --auth-pass secret

  # DTMF transfer + auto transport + retry + logging:
  siprobo --host 192.0.2.10 --phone 15551234567 --wav message.wav \\
          --caller-id 1001 --auth-user 1001 --auth-pass secret \\
          --forward-number 15559876543 --forward-digits 0,1 \\
          --transport auto --max-retries 3 --log-file calls.log -v

Python 3.13+ note:
  audioop was removed — install:  pip install audioop-lts
""",
    )

    req = parser.add_argument_group("required")
    req.add_argument("--host",  required=True, help="Asterisk IP or hostname")
    req.add_argument("--phone", required=True, help="Destination number / extension")
    req.add_argument("--wav",   required=True, metavar="FILE",
                     help="WAV file to play (any sample rate / bit depth)")

    sip = parser.add_argument_group("SIP")
    sip.add_argument("--port", type=int, default=5060,
                     help="SIP port (default: 5060)")
    sip.add_argument("--transport", default="udp",
                     choices=("udp", "tcp", "auto"),
                     help="Transport: udp, tcp, or auto-detect (default: udp)")
    sip.add_argument("--caller-id",   default="", help="SIP From extension / caller-ID")
    sip.add_argument("--auth-user",   default="", help="Digest auth username")
    sip.add_argument("--auth-pass",   default="", help="Digest auth password")
    sip.add_argument("--source-ip",   default="auto",
                     help="Local IP to bind (default: auto-detect)")
    sip.add_argument("--dial-prefix", default="",
                     help='Prefix prepended to --phone in Request-URI (e.g. "call-")')
    sip.add_argument("--ring-timeout", type=int, default=60,
                     help="Seconds to wait for answer (default: 60)")

    xfer = parser.add_argument_group("DTMF / transfer")
    xfer.add_argument("--forward-number", default="",
                      help="Blind-transfer target when a matching DTMF digit is pressed")
    xfer.add_argument("--forward-digits", default="0",
                      help='Comma-separated trigger digits (default: "0")')

    retry = parser.add_argument_group("retry")
    retry.add_argument("--max-retries", type=int, default=1,
                       help="Total call attempts (default: 1; set >1 to retry on failure)")
    retry.add_argument("--retry-delay", type=float, default=5.0,
                       help="Initial wait between retries in seconds (default: 5.0)")
    retry.add_argument("--backoff-factor", type=float, default=2.0,
                       help="Exponential backoff multiplier (default: 2.0)")

    parser.add_argument("--log-file", default="",
                        help="Append a JSON call record to this file after each attempt")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print raw SIP messages and RTP diagnostics")

    args       = parser.parse_args(argv)
    source_ip  = None if args.source_ip == "auto" else args.source_ip
    fwd_digits = [d.strip() for d in args.forward_digits.split(",") if d.strip()]
    wav_path   = os.path.abspath(args.wav)
    log_file   = args.log_file or None

    print(f"""
{Colors.BOLD}{Colors.CYAN}============================================================
  siprobo  --  SIP Auto-Dialler + WAV Playback
============================================================{Colors.END}

  Asterisk    : {args.host}:{args.port} ({args.transport.upper()})
  Caller-ID   : {args.caller_id or '(not set)'}
  Calling     : {Colors.BOLD}{args.phone}{Colors.END}
  WAV file    : {Colors.BOLD}{os.path.basename(wav_path)}{Colors.END}
  Forward on  : {fwd_digits} -> {Colors.BOLD}{args.forward_number or '(not set)'}{Colors.END}
  Retries     : {args.max_retries}
  Log file    : {log_file or '(disabled)'}
""")

    call_kwargs = dict(
        host           = args.host,
        port           = args.port,
        phone          = args.phone,
        transport      = args.transport,
        source_ip      = source_ip,
        caller_id      = args.caller_id,
        auth_user      = args.auth_user,
        auth_pass      = args.auth_pass,
        dial_prefix    = args.dial_prefix,
        wav_path       = wav_path,
        forward_number = args.forward_number,
        forward_digits = fwd_digits,
        verbose        = args.verbose,
        ring_timeout   = args.ring_timeout,
        log_file       = log_file,
    )

    if args.max_retries > 1:
        success = make_call_with_retry(
            max_retries    = args.max_retries,
            retry_delay    = args.retry_delay,
            backoff_factor = args.backoff_factor,
            **call_kwargs,
        )
    else:
        success = make_call(**call_kwargs)

    if success:
        _ok("Session complete.")
    else:
        _fail("Call did not complete.")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
