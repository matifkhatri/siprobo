# siprobo — Python SIP Auto-Dialler for Asterisk

> **Outbound SIP calls · WAV playback over RTP · RFC 2833 DTMF detection · Blind call transfer · Zero dependencies**

[![PyPI version](https://img.shields.io/pypi/v/siprobo.svg?color=blue)](https://pypi.org/project/siprobo/)
[![Python versions](https://img.shields.io/pypi/pyversions/siprobo.svg)](https://pypi.org/project/siprobo/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI downloads](https://img.shields.io/pypi/dm/siprobo.svg)](https://pypi.org/project/siprobo/)
[![GitHub stars](https://img.shields.io/github/stars/matifkhatri/siprobo?style=social)](https://github.com/matifkhatri/siprobo)

**siprobo** is a lightweight, pure-Python SIP client that dials any phone number through an **Asterisk PBX**, plays a **WAV audio file** over RTP (G.711 PCMU/PCMA), detects **DTMF keypresses** (RFC 2833), and performs **blind call transfers** — all with zero external dependencies and full Python 3.8–3.13 support.

---

## Key Features

| Feature | Detail |
|---|---|
| **Outbound SIP INVITE** | RFC 3261 compliant — works with Asterisk, FreePBX, FusionPBX |
| **SIP Digest Authentication** | Auto-handles 401/407 challenges (MD5, qop=auth) |
| **WAV Playback over RTP** | Any sample rate, bit depth, mono/stereo — auto-converts to G.711 |
| **Codec Auto-Negotiation** | Detects PCMU (PT=0) or PCMA (PT=8) from SDP answer |
| **RFC 2833 DTMF Detection** | Listens for telephone-event (PT=101) packets in real time |
| **Blind Call Transfer** | Sends SIP REFER on configurable DTMF digit |
| **Re-INVITE Handling** | Responds correctly to mid-call server re-INVITEs |
| **Transport Auto-Detection** | `--transport auto` probes UDP then TCP |
| **Retry with Backoff** | Configurable exponential backoff on failed attempts |
| **Concurrent Calls** | Thread-pool based parallel dialling via `make_calls_concurrent()` |
| **JSON Call Logging** | Appends structured records to a log file per call |
| **Graceful Interrupt** | Ctrl+C sends CANCEL (ringing) or BYE (answered) cleanly |
| **Zero Dependencies** | Pure Python standard library — no pip installs required |
| **Python 3.8–3.13** | Works on all modern Python versions |

---

## Installation

```bash
pip install siprobo
```

### Python 3.13+

`audioop` was removed from the stdlib in Python 3.13. Install the drop-in replacement:

```bash
pip install "siprobo[py313]"
```

---

## Quick Start

### 1. Place a call and play a WAV message

```bash
siprobo \
  --host  192.0.2.10 \
  --phone 15551234567 \
  --wav   /path/to/message.wav \
  --caller-id 1001 \
  --auth-user 1001 \
  --auth-pass yourpassword
```

What happens:
1. Connects to your Asterisk server via SIP/UDP
2. Handles Digest auth challenge automatically
3. When answered, streams the WAV over RTP (G.711 PCMU/PCMA)
4. Sends a clean in-dialog BYE when playback finishes

---

### 2. DTMF-triggered blind transfer

```bash
siprobo \
  --host           192.0.2.10 \
  --phone          15551234567 \
  --wav            message.wav \
  --caller-id      1001 \
  --auth-user      1001 \
  --auth-pass      yourpassword \
  --forward-number 15559876543 \
  --forward-digits 0,1
```

If the remote party presses **0** or **1**, siprobo sends a SIP REFER and blind-transfers the call to `15559876543`.

---

### 3. Auto transport + retry + logging

```bash
siprobo \
  --host           192.0.2.10 \
  --phone          15551234567 \
  --wav            message.wav \
  --caller-id      1001 \
  --auth-user      1001 \
  --auth-pass      yourpassword \
  --transport      auto \
  --max-retries    3 \
  --retry-delay    5 \
  --log-file       calls.log \
  -v
```

- `--transport auto` probes UDP first, falls back to TCP
- `--max-retries 3` retries with 5 s → 10 s → 20 s backoff
- `--log-file` appends one JSON record per attempt
- `-v` prints raw SIP messages for debugging

---

### 4. SIP connectivity diagnostics

```bash
siprobo-diag \
  --host      192.0.2.10 \
  --caller-id 1001 \
  --auth-user 1001 \
  --auth-pass yourpassword
```

Runs four checks in sequence: **ICMP ping → TCP port → UDP port → SIP OPTIONS**.

Add `--call <number>` to run a live INVITE test:

```bash
siprobo-diag \
  --host      192.0.2.10 \
  --auth-user 1001 \
  --auth-pass yourpassword \
  --call      15551234567 \
  --hangup-after 5 \
  -v
```

---

## All CLI Options

### `siprobo`

```
required:
  --host HOST             Asterisk IP or hostname
  --phone PHONE           Destination number / extension
  --wav FILE              WAV file to play (any sample rate / bit depth)

SIP:
  --port PORT             SIP port (default: 5060)
  --transport {udp,tcp,auto}
                          Transport protocol (default: udp)
  --caller-id CALLER_ID   SIP From extension / caller-ID
  --auth-user AUTH_USER   Digest auth username
  --auth-pass AUTH_PASS   Digest auth password
  --source-ip SOURCE_IP   Local IP to bind (default: auto-detect)
  --dial-prefix PREFIX    Prefix prepended to --phone in Request-URI
  --ring-timeout SECONDS  Seconds to wait for answer (default: 60)

DTMF / transfer:
  --forward-number NUMBER Blind-transfer destination on DTMF match
  --forward-digits DIGITS Comma-separated trigger digits (default: 0)

retry:
  --max-retries N         Total call attempts (default: 1)
  --retry-delay SECONDS   Initial wait between retries (default: 5.0)
  --backoff-factor MULT   Exponential backoff multiplier (default: 2.0)

  --log-file FILE         Append JSON call records to this file
  -v, --verbose           Print raw SIP messages and RTP diagnostics
```

### `siprobo-diag`

```
  --host HOST             Asterisk IP or hostname (required)
  --port PORT             SIP port (default: 5060)
  --transport {udp,tcp}   Transport (default: udp)
  --caller-id CALLER_ID   SIP caller-ID
  --auth-user AUTH_USER   Digest auth username
  --auth-pass AUTH_PASS   Digest auth password
  --call PHONE            Run a live INVITE test to this number
  --dial-prefix PREFIX    Prefix for Request-URI
  --hangup-after SECONDS  Seconds after answer to send BYE (0 = wait)
  --max-call-seconds N    Safety limit for answered calls (default: 60)
  --skip-ping             Skip the ICMP ping test
  -v, --verbose           Print raw SIP messages
```

---

## Python API

### Single call

```python
from siprobo.caller import make_call

answered = make_call(
    host           = "192.0.2.10",
    port           = 5060,
    phone          = "15551234567",
    transport      = "udp",        # or "tcp" or "auto"
    caller_id      = "1001",
    auth_user      = "1001",
    auth_pass      = "yourpassword",
    wav_path       = "/path/to/message.wav",
    forward_number = "15559876543",
    forward_digits = ["0", "1"],
    verbose        = False,
    ring_timeout   = 60,
    log_file       = "calls.log",
)
print("Answered:", answered)
```

### Retry with exponential backoff

```python
from siprobo.caller import make_call_with_retry

answered = make_call_with_retry(
    max_retries    = 3,
    retry_delay    = 5.0,
    backoff_factor = 2.0,
    # --- make_call kwargs below ---
    host      = "192.0.2.10",
    phone     = "15551234567",
    wav_path  = "message.wav",
    auth_user = "1001",
    auth_pass = "yourpassword",
)
```

### Concurrent calls (bulk dialling)

```python
from siprobo.caller import make_calls_concurrent

configs = [
    {"host": "192.0.2.10", "phone": "15551001001", "wav_path": "msg.wav",
     "caller_id": "100", "auth_user": "100", "auth_pass": "secret"},
    {"host": "192.0.2.10", "phone": "15551001002", "wav_path": "msg.wav",
     "caller_id": "100", "auth_user": "100", "auth_pass": "secret"},
    {"host": "192.0.2.10", "phone": "15551001003", "wav_path": "msg.wav",
     "caller_id": "100", "auth_user": "100", "auth_pass": "secret"},
]

results = make_calls_concurrent(configs, max_workers=10)
# results = [True, False, True]  — one bool per call
answered_count = sum(results)
print(f"{answered_count}/{len(configs)} calls answered")
```

### Diagnostics

```python
from siprobo.sip_core import test_sip_options, test_sip_invite

# Check if Asterisk is reachable
ok = test_sip_options(
    host      = "192.0.2.10",
    port      = 5060,
    auth_user = "1001",
    auth_pass = "yourpassword",
)

# Run a full live call test
ok = test_sip_invite(
    host         = "192.0.2.10",
    port         = 5060,
    phone_number = "15551234567",
    auth_user    = "1001",
    auth_pass    = "yourpassword",
    hangup_after = 5,
)
```

---

## Asterisk Setup

siprobo talks directly to Asterisk via raw SIP — no special modules required.

### Minimal PJSIP endpoint (`pjsip.conf`)

```ini
[1001]
type=endpoint
context=from-internal
auth=1001-auth
aors=1001

[1001-auth]
type=auth
auth_type=userpass
username=1001
password=yourpassword

[1001]
type=aor
max_contacts=5
```

### Dialplan (`extensions.conf`)

```ini
[from-internal]
; siprobo dials: sip:<phone>@<asterisk>
exten => _X.,1,NoOp(siprobo outbound to ${EXTEN})
 same =>       n,Dial(PJSIP/${EXTEN}@your-trunk)
 same =>       n,Hangup()
```

Reload after changes:

```bash
asterisk -rx "dialplan reload"
asterisk -rx "pjsip reload"
```

---

## WAV File Requirements

siprobo accepts **any WAV file** and auto-converts on-the-fly:

| Input | Output |
|---|---|
| Any sample rate | 8 000 Hz |
| Mono or stereo | Mono |
| Any bit depth (8/16/24/32-bit PCM) | G.711 mu-law (PCMU) or A-law (PCMA) |

The codec (PCMU vs PCMA) is **automatically selected** from the SDP answer — no manual configuration needed.

---

## Call Log Format

Each call appends one JSON line to `--log-file`:

```json
{
  "timestamp": "2026-04-09T12:00:00.000000+00:00",
  "phone": "15551234567",
  "host": "192.0.2.10",
  "caller_id": "1001",
  "status": "answered",
  "duration_s": 45.2,
  "dtmf_received": ["0"],
  "transferred_to": "15559876543"
}
```

Status values: `answered` · `no_answer` · `error`

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `audioop not found` | Python 3.13+ | `pip install audioop-lts` |
| 401/407 loop | Wrong credentials | Check `--auth-user` / `--auth-pass` |
| 404 Not Found | Dialplan mismatch | Check `--dial-prefix` and dialplan context |
| No answer / ring timeout | Firewall or wrong number | Run `siprobo-diag` first |
| One-way audio | NAT / firewall on RTP port | Ensure UDP 20000–29999 is open |
| WAV sounds wrong | Codec mismatch | Use `-v` to see which codec was negotiated |
| BYE not matching dialog | Missing To-tag | Expected behaviour; siprobo extracts it from 200 OK |

---

## Supported Environments

| PBX / Server | Status |
|---|---|
| Asterisk 16 / 18 / 20 / 21 | Tested |
| FreePBX (Asterisk backend) | Compatible |
| FusionPBX (FreeSWITCH backend) | Compatible (SIP standard) |
| Any RFC 3261 compliant SIP server | Should work |

---

## Project Structure

```
siprobo/
├── siprobo/
│   ├── __init__.py      # Package root, version
│   ├── sip_core.py      # SIP protocol helpers + siprobo-diag CLI
│   └── caller.py        # Call flow, RTP streaming + siprobo CLI
├── README.md
├── LICENSE              # MIT
└── pyproject.toml       # Build config (PEP 621)
```

---

## Contributing

Pull requests are welcome at [github.com/matifkhatri/siprobo](https://github.com/matifkhatri/siprobo).

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request

---

## Author

**matifkhatri** — [github.com/matifkhatri](https://github.com/matifkhatri)

---

## License

MIT — see [LICENSE](LICENSE) for full text.

---

## Related Projects & Keywords

`python sip client` · `asterisk autodialer` · `sip ivr python` · `voip python library` ·
`rtp wav playback` · `dtmf detection python` · `sip blind transfer` · `g711 python` ·
`asterisk sip invite python` · `python pbx automation` · `sip refer python` ·
`rfc3261 python` · `rfc2833 dtmf` · `python voip autodialer` · `asterisk python bot`
