#!/usr/bin/env python3
"""
siprobo.sip_core
================
Low-level SIP/RTP protocol helpers and a stand-alone connectivity
diagnostic tool (``siprobo-diag``).

Features
--------
- ICMP ping, TCP/UDP port checks
- SIP OPTIONS + INVITE tests with Digest-auth retry
- RFC 3261-compliant message builders (INVITE, ACK, BYE, CANCEL)
- MD5 Digest-auth (RFC 2617 / RFC 3261) — supports qop=auth

No credentials, IP addresses, or phone numbers are hard-coded.
Supply all values on the command line or via the Python API.
"""

import argparse
import hashlib
import platform
import random
import re
import socket
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Default constants  (all intentionally empty / generic)
# ---------------------------------------------------------------------------
DEFAULT_SIP_PORT = 5060
DEFAULT_TRANSPORT = "udp"
DEFAULT_SOURCE_IP = "auto"

# ---------------------------------------------------------------------------
# Terminal colours (degrade gracefully on non-ANSI terminals)
# ---------------------------------------------------------------------------
class Colors:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    END    = "\033[0m"


def _ok(msg):   print(f"  {Colors.GREEN}[OK]  {Colors.END}{msg}")
def _fail(msg): print(f"  {Colors.RED}[ERR] {Colors.END}{msg}")
def _warn(msg): print(f"  {Colors.YELLOW}[!]   {Colors.END}{msg}")
def _info(msg): print(f"  {Colors.BLUE}[i]   {Colors.END}{msg}")
def _hdr(msg):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}\n  {msg}\n{'=' * 60}{Colors.END}")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def get_local_ip(target_host: str, target_port: int, source_ip: str = None) -> str:
    """
    Return the local IP address that the OS would use to reach
    *target_host:target_port*.  If *source_ip* is provided (and is not
    the sentinel ``"auto"``), that value is returned as-is so the caller
    can bind to a specific interface.
    """
    if source_ip and source_ip != "auto":
        return source_ip
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((target_host, target_port))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# SIP Digest authentication helpers  (RFC 2617 / RFC 3261)
# ---------------------------------------------------------------------------

def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _parse_digest_challenge(header_value: str) -> dict:
    """
    Parse a ``WWW-Authenticate`` / ``Proxy-Authenticate`` header value
    (everything after the header name + colon) into a parameter dict.
    Keys are lower-cased; quoted strings are unquoted.
    """
    hv = header_value.strip()
    if hv.lower().startswith("digest"):
        hv = hv.split(" ", 1)[1].strip()
    params = {}
    for m in re.finditer(r'(\w+)\s*=\s*("([^"]*)"|([^,\s]+))', hv):
        k = m.group(1)
        v = m.group(3) if m.group(3) is not None else m.group(4)
        params[k.lower()] = v
    return params


def _extract_authenticate_header(response_str: str):
    """
    Scan a raw SIP response for a ``WWW-Authenticate`` or
    ``Proxy-Authenticate`` challenge.

    Returns
    -------
    (status_code, header_name, header_value)
        *header_name* is ``"www-authenticate"`` or ``"proxy-authenticate"``.
        All three are ``None`` if no challenge is found.
    """
    lines = response_str.split("\r\n")
    if not lines:
        return None, None, None

    status_code = None
    m = re.match(r"^SIP/2\.0\s+(\d{3})\b", lines[0].strip())
    if m:
        status_code = int(m.group(1))

    hdrs = {}
    for ln in lines[1:]:
        if not ln or ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        hdrs[k.strip().lower()] = v.strip()

    if "www-authenticate" in hdrs:
        return status_code, "www-authenticate", hdrs["www-authenticate"]
    if "proxy-authenticate" in hdrs:
        return status_code, "proxy-authenticate", hdrs["proxy-authenticate"]
    return status_code, None, None


def _build_digest_authorization(
    challenge_header_value: str,
    method: str,
    uri: str,
    username: str,
    password: str,
    is_proxy: bool = False,
) -> str:
    """
    Build an ``Authorization`` (or ``Proxy-Authorization``) header line
    for a SIP Digest challenge.  Supports ``qop=auth`` and plain MD5.

    Returns a complete header line ending with ``\\r\\n``.
    """
    ch        = _parse_digest_challenge(challenge_header_value)
    realm     = ch.get("realm", "")
    nonce     = ch.get("nonce", "")
    algorithm = (ch.get("algorithm") or "MD5").upper()
    qop       = ch.get("qop")
    opaque    = ch.get("opaque")

    if algorithm not in ("MD5",):
        algorithm = "MD5"

    ha1 = _md5_hex(f"{username}:{realm}:{password}")
    ha2 = _md5_hex(f"{method}:{uri}")

    auth_params = {
        "username": username,
        "realm":    realm,
        "nonce":    nonce,
        "uri":      uri,
    }

    if qop:
        qop_token = qop.split(",")[0].strip()
        nc        = "00000001"
        cnonce    = _md5_hex(str(random.random()))[:16]
        response  = _md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_token}:{ha2}")
        auth_params.update({"response": response, "qop": qop_token,
                            "nc": nc, "cnonce": cnonce})
    else:
        auth_params["response"] = _md5_hex(f"{ha1}:{nonce}:{ha2}")

    if opaque:
        auth_params["opaque"] = opaque
    auth_params["algorithm"] = algorithm

    hdr_name = "Proxy-Authorization" if is_proxy else "Authorization"
    parts = [
        f'username="{auth_params["username"]}"',
        f'realm="{auth_params["realm"]}"',
        f'nonce="{auth_params["nonce"]}"',
        f'uri="{auth_params["uri"]}"',
        f'response="{auth_params["response"]}"',
    ]
    if "opaque" in auth_params:
        parts.append(f'opaque="{auth_params["opaque"]}"')
    parts.append(f'algorithm={auth_params["algorithm"]}')
    if "qop" in auth_params:
        parts.append(f'qop={auth_params["qop"]}')
        parts.append(f'nc={auth_params["nc"]}')
        parts.append(f'cnonce="{auth_params["cnonce"]}"')

    return f"{hdr_name}: Digest " + ", ".join(parts) + "\r\n"


# ---------------------------------------------------------------------------
# SIP message builders
# ---------------------------------------------------------------------------

def _rand_branch() -> str:
    return f"z9hG4bK{random.randint(100_000, 999_999)}"


def _build_options_request(
    host, port, transport, local_ip, local_port,
    caller_id, call_id, branch, tag, cseq, auth_header=""
):
    from_user = caller_id or "siprobo"
    uri = f"sip:{host}:{port}"
    msg = (
        f"OPTIONS {uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/{transport.upper()} {local_ip}:{local_port}"
        f";branch={branch};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={tag}\r\n'
        f"To: <{uri}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} OPTIONS\r\n"
        f"Contact: <sip:{from_user}@{local_ip}:{local_port}>\r\n"
        f"Max-Forwards: 70\r\n"
        f"User-Agent: siprobo/{_get_version()}\r\n"
        f"Accept: application/sdp\r\n"
        f"{auth_header}"
        f"Content-Length: 0\r\n\r\n"
    )
    return msg, uri


def _build_invite_request(
    host, port, transport, request_uri, to_uri,
    local_ip, local_port, caller_id, call_id,
    branch, tag, cseq, sdp, auth_header=""
):
    from_user = caller_id or "siprobo"
    msg = (
        f"INVITE {request_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/{transport.upper()} {local_ip}:{local_port}"
        f";branch={branch};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={tag}\r\n'
        f"To: <{to_uri}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} INVITE\r\n"
        f"Contact: <sip:{from_user}@{local_ip}:{local_port}>\r\n"
        f"Max-Forwards: 70\r\n"
        f"User-Agent: siprobo/{_get_version()}\r\n"
        f"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REFER, NOTIFY\r\n"
        f"Content-Type: application/sdp\r\n"
        f"{auth_header}"
        f"Content-Length: {len(sdp)}\r\n\r\n"
        f"{sdp}"
    )
    return msg


def _parse_sip_headers(msg: str) -> dict:
    """Return a lower-cased key dict of SIP headers found in *msg*."""
    headers = {}
    for line in msg.split("\r\n"):
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return headers


def _build_200_ok_for_request(request_str: str) -> str:
    """
    Build a minimal ``SIP/2.0 200 OK`` that mirrors the Via/From/To/
    Call-ID/CSeq headers of *request_str*.  Suitable for responding to
    in-dialog BYE and NOTIFY requests.
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
        "Content-Length: 0\r\n\r\n"
    )


def create_ack(
    host, port, phone, local_ip, local_port,
    call_id, tag, response, caller_id=None, invite_cseq=1
):
    """Build an ACK for a 200 OK to INVITE, extracting the To-tag."""
    from_user = caller_id or "siprobo"
    to_tag = ""
    for line in response.split("\r\n"):
        if line.lower().startswith("to:") and "tag=" in line:
            to_tag = ";tag=" + line.split("tag=")[1].split(";")[0].split(">")[0]
            break
    return (
        f"ACK sip:{phone}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port}"
        f";branch={_rand_branch()};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={tag}\r\n'
        f"To: <sip:{phone}@{host}>{to_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {invite_cseq} ACK\r\n"
        f"Max-Forwards: 70\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def create_bye(
    host, port, phone, local_ip, local_port,
    call_id, tag, caller_id=None, bye_cseq=2
):
    """Build a BYE request."""
    from_user = caller_id or "siprobo"
    return (
        f"BYE sip:{phone}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port}"
        f";branch={_rand_branch()};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={tag}\r\n'
        f"To: <sip:{phone}@{host}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {bye_cseq} BYE\r\n"
        f"Max-Forwards: 70\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def create_cancel(
    host, port, phone, local_ip, local_port,
    call_id, tag, branch, caller_id=None, invite_cseq=1
):
    """Build a CANCEL request (must reuse the same branch as the INVITE)."""
    from_user = caller_id or "siprobo"
    return (
        f"CANCEL sip:{phone}@{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r\n"
        f'From: "{from_user}" <sip:{from_user}@{local_ip}>;tag={tag}\r\n'
        f"To: <sip:{phone}@{host}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {invite_cseq} CANCEL\r\n"
        f"Max-Forwards: 70\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


# ---------------------------------------------------------------------------
# Network diagnostic tests
# ---------------------------------------------------------------------------

def test_ping(host: str, verbose: bool = False) -> bool:
    """ICMP ping the host; return ``True`` if reachable."""
    _hdr(f"TEST 1: ICMP Ping -> {host}")
    try:
        if platform.system().lower().startswith("win"):
            cmd = ["ping", "-n", "3", "-w", "2000", host]
        else:
            cmd = ["ping", "-c", "3", "-W", "2", host]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "avg" in line or "rtt" in line:
                    _info(f"Latency: {line.strip()}")
                    break
            _ok(f"{host} is reachable via ICMP")
            return True
        _warn("ICMP ping failed (may be blocked by firewall)")
        if verbose:
            print(f"    {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        _warn("Ping timed out")
        return False
    except FileNotFoundError:
        _warn("'ping' command not found")
        return False
    except Exception as exc:
        _fail(f"Ping error: {exc}")
        return False


def test_tcp_port(host: str, port: int, verbose: bool = False) -> bool:
    """TCP-connect to *host:port*; return ``True`` if the port is open."""
    _hdr(f"TEST 2: TCP Port {port} on {host}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        t0  = time.time()
        err = sock.connect_ex((host, port))
        lat = (time.time() - t0) * 1000
        sock.close()
        if err == 0:
            _ok(f"TCP port {port} is OPEN ({lat:.1f} ms)")
            return True
        _fail(f"TCP port {port} is CLOSED or filtered (error {err})")
        return False
    except socket.timeout:
        _fail("TCP connection timed out after 5 s")
        return False
    except Exception as exc:
        _fail(f"TCP test error: {exc}")
        return False


def test_udp_port(host: str, port: int, verbose: bool = False) -> bool:
    """
    Send a minimal SIP probe over UDP.  Because UDP is stateless a timeout
    is treated as a pass (the port is *probably* open).
    """
    _hdr(f"TEST 3: UDP Port {port} on {host}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        sock.sendto(b"OPTIONS sip:test SIP/2.0\r\n\r\n", (host, port))
        try:
            data, _ = sock.recvfrom(1024)
            _ok(f"UDP port {port} responded ({len(data)} bytes)")
            if verbose:
                print(f"    {data[:100]}")
        except socket.timeout:
            _warn("No UDP response — normal for SIP; will verify with OPTIONS")
        sock.close()
        return True
    except Exception as exc:
        _fail(f"UDP test error: {exc}")
        return False


def test_sip_options(
    host: str,
    port: int,
    transport: str = "udp",
    verbose: bool = False,
    source_ip: str = None,
    caller_id: str = None,
    auth_user: str = None,
    auth_pass: str = None,
) -> bool:
    """
    Send a SIP OPTIONS request.  If the server responds with 401/407,
    automatically retries with Digest credentials.

    Returns ``True`` if the server is reachable (any meaningful SIP response).
    """
    _hdr(f"TEST 4: SIP OPTIONS ({transport.upper()}) -> {host}:{port}")
    local_ip   = get_local_ip(host, port, source_ip)
    local_port = 5060 + random.randint(1, 5000)
    call_id    = f"{random.randint(100_000, 999_999)}@{local_ip}"
    branch     = _rand_branch()
    tag        = str(random.randint(100_000, 999_999))
    cseq       = 1

    options_msg, uri = _build_options_request(
        host, port, transport, local_ip, local_port,
        caller_id, call_id, branch, tag, cseq,
    )
    _info(f"Local: {local_ip}:{local_port}  |  Caller-ID: {caller_id or 'siprobo'}")
    _info(f"Sending OPTIONS -> sip:{host}:{port}")
    if verbose:
        print(f"\n{Colors.CYAN}--- OPTIONS ---\n{options_msg}{Colors.END}")

    try:
        if transport == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_ip = local_ip if source_ip else ""
            sock.bind((bind_ip, local_port))

        sock.settimeout(5)

        if transport == "tcp":
            sock.send(options_msg.encode())
            resp_raw = sock.recv(4096)
        else:
            sock.sendto(options_msg.encode(), (host, port))
            resp_raw, _ = sock.recvfrom(4096)

        resp = resp_raw.decode("utf-8", errors="ignore")
        if verbose:
            print(f"\n{Colors.CYAN}--- Response ---\n{resp[:500]}{Colors.END}")

        first_line = resp.split("\r\n")[0]
        _info(f"Response: {first_line}")

        sc, hdr_name, hdr_value = _extract_authenticate_header(resp)
        if sc in (401, 407) and hdr_name and hdr_value:
            _warn("Digest challenge — retrying with credentials ...")
            is_proxy   = hdr_name == "proxy-authenticate"
            auth_hdr   = _build_digest_authorization(
                hdr_value, "OPTIONS", uri,
                username=auth_user or "",
                password=auth_pass or "",
                is_proxy=is_proxy,
            )
            cseq  += 1
            msg2, _ = _build_options_request(
                host, port, transport, local_ip, local_port,
                caller_id, call_id, _rand_branch(), tag, cseq, auth_hdr,
            )
            if transport == "tcp":
                sock.send(msg2.encode())
                resp2 = sock.recv(4096).decode("utf-8", errors="ignore")
            else:
                sock.sendto(msg2.encode(), (host, port))
                resp2, _ = sock.recvfrom(4096)
                resp2 = resp2.decode("utf-8", errors="ignore")
            first_line = resp2.split("\r\n")[0]
            _info(f"Response (auth retry): {first_line}")
            resp = resp2

        sock.close()

        if "200" in first_line:
            _ok("SIP server responded 200 OK")
            for line in resp.split("\r\n"):
                if line.lower().startswith("allow:"):
                    _info(f"Allow: {line.split(':', 1)[1].strip()}")
                    break
            return True
        if "401" in first_line or "407" in first_line:
            _warn("Credentials rejected or not supplied")
            return False
        if first_line:
            _warn(f"SIP reachable — response: {first_line}")
            return True
        return False

    except socket.timeout:
        _fail("No SIP response (timeout 5 s)")
        _info("Check: server running? firewall allowing UDP 5060? correct IP?")
        return False
    except ConnectionRefusedError:
        _fail("Connection refused — SIP server may not be running")
        return False
    except Exception as exc:
        _fail(f"SIP OPTIONS error: {exc}")
        if verbose:
            import traceback
            traceback.print_exc()
        return False


def test_sip_invite(
    host: str,
    port: int,
    phone_number: str,
    transport: str = "udp",
    verbose: bool = False,
    source_ip: str = None,
    caller_id: str = None,
    auth_user: str = None,
    auth_pass: str = None,
    dial_prefix: str = "",
    hangup_after: int = 0,
    max_call_seconds: int = 60,
) -> bool:
    """
    Send a SIP INVITE to *phone_number* via *host* and follow the call
    through ringing / answer / BYE.  Returns ``True`` if the call reached
    the destination (ring or answer).
    """
    _hdr(f"TEST 5: SIP INVITE -> {phone_number}")
    local_ip       = get_local_ip(host, port, source_ip)
    local_port     = 5060 + random.randint(1, 5000)
    rtp_port       = 10000 + random.randint(0, 10000)
    call_id        = f"{random.randint(100_000, 999_999)}@{local_ip}"
    branch         = _rand_branch()
    tag            = str(random.randint(100_000, 999_999))
    cseq           = 1
    active_cseq    = cseq
    from_user      = caller_id or "siprobo"
    exten          = f"{dial_prefix}{phone_number}"
    request_uri    = f"sip:{exten}@{host}:{port}"
    to_uri         = f"sip:{exten}@{host}"

    sdp = (
        f"v=0\r\n"
        f"o=- {random.randint(1000, 9999)} 1 IN IP4 {local_ip}\r\n"
        f"s=siprobo\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        f"t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-16\r\n"
        f"a=sendrecv\r\n"
    )

    invite = _build_invite_request(
        host=host, port=port, transport=transport,
        request_uri=request_uri, to_uri=to_uri,
        local_ip=local_ip, local_port=local_port,
        caller_id=from_user, call_id=call_id,
        branch=branch, tag=tag, cseq=cseq, sdp=sdp,
    )
    _info(f"Request-URI : {request_uri}")
    _info(f"Local SIP   : {local_ip}:{local_port}")
    _info(f"Local RTP   : {local_ip}:{rtp_port}")
    if verbose:
        print(f"\n{Colors.CYAN}--- INVITE ---\n{invite[:600]}{Colors.END}")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        bind_ip = local_ip if source_ip else ""
        sock.bind((bind_ip, local_port))
        sock.sendto(invite.encode(), (host, port))

        responses      = []
        start          = time.time()
        auth_tried     = False
        answered       = False
        answered_at    = None
        cancel_sent    = False
        hangup_sent    = False
        bye_sent_at    = None
        ring_timeout   = 15

        while True:
            now = time.time()

            if not answered and (now - start) >= ring_timeout:
                _warn(f"No answer within {ring_timeout} s")
                break

            if answered and answered_at:
                if (not hangup_sent) and hangup_after > 0 and \
                        (now - answered_at) >= hangup_after:
                    bye = create_bye(host, port, exten, local_ip, local_port,
                                     call_id, tag, caller_id, bye_cseq=active_cseq + 1)
                    sock.sendto(bye.encode(), (host, port))
                    hangup_sent = True
                    bye_sent_at = now
                    _info("BYE sent (hangup-after reached)")

                if (not hangup_sent) and (now - answered_at) >= max_call_seconds:
                    bye = create_bye(host, port, exten, local_ip, local_port,
                                     call_id, tag, caller_id, bye_cseq=active_cseq + 1)
                    sock.sendto(bye.encode(), (host, port))
                    hangup_sent = True
                    bye_sent_at = now
                    _info("BYE sent (max-call-seconds reached)")

            if hangup_sent and bye_sent_at and (now - bye_sent_at) >= 5:
                _info("Call flow complete.")
                break

            try:
                raw, _ = sock.recvfrom(4096)
                msg    = raw.decode("utf-8", errors="ignore")
                first  = msg.split("\r\n")[0]

                if msg.startswith("BYE "):
                    _info("Server sent BYE — acknowledging")
                    sock.sendto(_build_200_ok_for_request(msg).encode(), (host, port))
                    break

                sc, hdr_name, hdr_value = _extract_authenticate_header(msg)
                if not auth_tried and sc in (401, 407) and hdr_name and hdr_value:
                    auth_tried = True
                    _warn("Digest challenge on INVITE — retrying ...")
                    is_proxy = hdr_name == "proxy-authenticate"
                    auth_hdr = _build_digest_authorization(
                        hdr_value, "INVITE", request_uri,
                        username=auth_user or "",
                        password=auth_pass or "",
                        is_proxy=is_proxy,
                    )
                    cseq       += 1
                    active_cseq = cseq
                    invite2 = _build_invite_request(
                        host=host, port=port, transport=transport,
                        request_uri=request_uri, to_uri=to_uri,
                        local_ip=local_ip, local_port=local_port,
                        caller_id=from_user, call_id=call_id,
                        branch=_rand_branch(), tag=tag,
                        cseq=cseq, sdp=sdp, auth_header=auth_hdr,
                    )
                    sock.sendto(invite2.encode(), (host, port))
                    continue

                if first not in [r["line"] for r in responses]:
                    responses.append({"line": first, "t": now - start})
                    if "100" in first:
                        print(f"  {Colors.BLUE}...{Colors.END}  {first}")
                    elif "180" in first:
                        print(f"  {Colors.GREEN}~~~{Colors.END}  {first}  <-- ringing!")
                    elif "183" in first:
                        print(f"  {Colors.GREEN}~~~{Colors.END}  {first}")
                    elif "200" in first and "INVITE" in \
                            {k.split()[-1].upper() for k in [msg.split("CSeq:")[1].split("\r\n")[0]] if "CSeq:" in msg}:
                        print(f"  {Colors.GREEN}[OK]{Colors.END}  {first}  <-- answered!")
                        ack = create_ack(host, port, exten, local_ip, local_port,
                                         call_id, tag, msg, caller_id, active_cseq)
                        sock.sendto(ack.encode(), (host, port))
                        answered    = True
                        answered_at = now
                    elif re.match(r"SIP/2\.0 [456]\d\d", first):
                        print(f"  {Colors.RED}[ERR]{Colors.END}  {first}")
                        break
                    else:
                        print(f"  {Colors.YELLOW}[?]  {Colors.END}  {first}")

                if not answered and (now - start) > 8 and not cancel_sent:
                    if any("180" in r["line"] or "183" in r["line"] for r in responses):
                        cancel = create_cancel(host, port, exten, local_ip, local_port,
                                               call_id, tag, branch, caller_id, active_cseq)
                        sock.sendto(cancel.encode(), (host, port))
                        _info("CANCEL sent (ring timeout)")
                        cancel_sent = True

            except socket.timeout:
                continue

        sock.close()

        if not responses:
            _fail("No response from SIP server")
            return False

        last = responses[-1]["line"]
        if "200" in last or any("180" in r["line"] or "183" in r["line"] for r in responses):
            _ok("Call reached destination successfully")
            return True
        if "487" in last:
            _ok("CANCEL acknowledged (487 Request Terminated)")
            return True
        if "401" in last or "407" in last:
            _warn("Authentication required — SIP reachable but credentials wrong/missing")
            return True
        _fail(f"Call did not complete: {last}")
        return False

    except Exception as exc:
        _fail(f"INVITE error: {exc}")
        if verbose:
            import traceback
            traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Version helper
# ---------------------------------------------------------------------------

def _get_version() -> str:
    try:
        from siprobo import __version__
        return __version__
    except Exception:
        return "dev"


# ---------------------------------------------------------------------------
# CLI entry point  (siprobo-diag)
# ---------------------------------------------------------------------------

def main(argv=None):
    """
    Entry point for the ``siprobo-diag`` command.

    Runs a suite of connectivity checks against an Asterisk SIP server:
    ICMP ping, TCP port, UDP port, SIP OPTIONS, and optionally SIP INVITE.
    """
    parser = argparse.ArgumentParser(
        prog="siprobo-diag",
        description="SIP connectivity diagnostic tool for Asterisk servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  siprobo-diag --host 192.0.2.10
  siprobo-diag --host 192.0.2.10 --caller-id 1001 --auth-user 1001 --auth-pass secret
  siprobo-diag --host 192.0.2.10 --call 15551234567 -v
  siprobo-diag --host 192.0.2.10 --call 15551234567 --dial-prefix "" --hangup-after 5
""",
    )
    parser.add_argument("--host", "-H", required=True,
                        help="Asterisk server IP or hostname (required)")
    parser.add_argument("--port", "-P", type=int, default=DEFAULT_SIP_PORT,
                        help=f"SIP port (default: {DEFAULT_SIP_PORT})")
    parser.add_argument("--transport", choices=("udp", "tcp"), default=DEFAULT_TRANSPORT,
                        help=f"SIP transport (default: {DEFAULT_TRANSPORT})")
    parser.add_argument("--source-ip", default=DEFAULT_SOURCE_IP,
                        help="Source IP to bind (default: auto-detect)")
    parser.add_argument("--caller-id", default="",
                        help="SIP From / Caller-ID extension (e.g. 1001)")
    parser.add_argument("--auth-user", default="",
                        help="Digest auth username")
    parser.add_argument("--auth-pass", default="",
                        help="Digest auth password")
    parser.add_argument("--call", metavar="PHONE",
                        help="Run a live INVITE test to this number")
    parser.add_argument("--dial-prefix", default="",
                        help="Prefix prepended to the dialled number in Request-URI "
                             '(e.g. "call-"); default: empty')
    parser.add_argument("--hangup-after", type=int, default=0,
                        help="Seconds after answer to send BYE (0 = wait for server BYE)")
    parser.add_argument("--max-call-seconds", type=int, default=60,
                        help="Safety limit: max seconds to keep an answered call open "
                             "(default: 60)")
    parser.add_argument("--skip-ping", action="store_true",
                        help="Skip the ICMP ping test")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print raw SIP messages")

    args      = parser.parse_args(argv)
    source_ip = None if args.source_ip == "auto" else args.source_ip
    local_ip  = get_local_ip(args.host, args.port, source_ip)

    print(f"""
{Colors.BOLD}{Colors.CYAN}============================================================
  siprobo-diag  --  SIP Connectivity Diagnostic
============================================================{Colors.END}

  Target     : {args.host}:{args.port}  ({args.transport.upper()})
  Local IP   : {local_ip}
  Caller-ID  : {args.caller_id or '(not set)'}
  Auth user  : {args.auth_user or '(not set)'}
""")

    results = {}

    if not args.skip_ping:
        results["ping"] = test_ping(args.host, args.verbose)
    else:
        _info("Skipping ping test (--skip-ping)")
        results["ping"] = None

    results["tcp"]     = test_tcp_port(args.host, args.port, args.verbose)
    results["udp"]     = test_udp_port(args.host, args.port, args.verbose)
    results["options"] = test_sip_options(
        args.host, args.port, args.transport, args.verbose,
        source_ip, args.caller_id, args.auth_user, args.auth_pass,
    )

    if args.call:
        results["invite"] = test_sip_invite(
            args.host, args.port, args.call, args.transport, args.verbose,
            source_ip, args.caller_id, args.auth_user, args.auth_pass,
            args.dial_prefix, args.hangup_after, args.max_call_seconds,
        )
    else:
        results["invite"] = None

    print(f"""
{Colors.BOLD}{Colors.CYAN}============================================================
  SUMMARY
============================================================{Colors.END}
""")
    checks = [
        ("ICMP Ping",   results["ping"]),
        ("TCP Port",    results["tcp"]),
        ("UDP Port",    results["udp"]),
        ("SIP OPTIONS", results["options"]),
        ("SIP INVITE",  results["invite"]),
    ]
    for name, result in checks:
        if result is None:
            print(f"  {Colors.YELLOW}[-]{Colors.END} {name}: Skipped")
        elif result:
            print(f"  {Colors.GREEN}[OK]{Colors.END} {name}: PASS")
        else:
            print(f"  {Colors.RED}[FAIL]{Colors.END} {name}: FAIL")

    print()
    if results["options"]:
        _ok("SIP server is reachable and responding.")
    elif results["tcp"] or results["udp"]:
        _warn("Network reachable but SIP did not respond — check SIP service.")
    else:
        _fail("Cannot reach SIP server — check IP, port, and firewall rules.")

    return 0 if results["options"] else 1


if __name__ == "__main__":
    sys.exit(main())
