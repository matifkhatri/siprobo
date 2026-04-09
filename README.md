# siprobo — Python SIP Auto-Dialler for Asterisk

> **Make outbound SIP calls from Python · Play WAV files over RTP · Detect DTMF keypresses · Blind call transfer · Zero dependencies · Python 3.8–3.13**

[![PyPI version](https://img.shields.io/pypi/v/siprobo.svg?color=blue&label=PyPI)](https://pypi.org/project/siprobo/)
[![Python versions](https://img.shields.io/pypi/pyversions/siprobo.svg)](https://pypi.org/project/siprobo/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI downloads](https://img.shields.io/pypi/dm/siprobo.svg?label=PyPI%20downloads)](https://pypi.org/project/siprobo/)
[![GitHub stars](https://img.shields.io/github/stars/matifkhatri/siprobo?style=social)](https://github.com/matifkhatri/siprobo/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/matifkhatri/siprobo?style=social)](https://github.com/matifkhatri/siprobo/network/members)

**siprobo** is a pure-Python SIP client library and command-line tool for placing **automated outbound phone calls** through an **Asterisk PBX** server. It streams any **WAV audio file** over RTP using **G.711 PCMU or PCMA** codec, listens for **RFC 2833 DTMF keypresses**, and performs **SIP REFER blind call transfers** — all with **zero external Python dependencies** and support for Python 3.8 through 3.13.

Whether you need a **Python SIP autodialler**, a **VoIP notification bot**, an **IVR system**, or a **SIP call tester**, siprobo gives you a clean Python API and a ready-to-use command-line interface.

---

## Why siprobo?

- **No SIP stack required** — pure Python stdlib, no `pjsua`, no `linphone`, no native libs
- **Works on any OS** — Windows, Linux, macOS
- **Any WAV file** — auto-resamples to 8 kHz G.711, any input format accepted
- **Production ready** — retry logic, concurrent calls, JSON logging, graceful shutdown
- **RFC compliant** — RFC 3261 (SIP), RFC 2833 (DTMF), RFC 3550 (RTP)

---

## Install

```bash
pip install siprobo
```

**Python 3.13+** (audioop removed from stdlib):

```bash
pip install "siprobo[py313]"
```

---

## Quick Start — Make a SIP Call from Python in 30 Seconds

### Command line

```bash
siprobo \
  --host  YOUR_ASTERISK_IP \
  --phone 15551234567 \
  --wav   message.wav \
  --caller-id 1001 \
  --auth-user 1001 \
  --auth-pass yourpassword
```

### Python code

```python
from siprobo.caller import make_call

answered = make_call(
    host      = "YOUR_ASTERISK_IP",
    phone     = "15551234567",
    wav_path  = "message.wav",
    caller_id = "1001",
    auth_user = "1001",
    auth_pass = "yourpassword",
)
print("Call answered:", answered)
```

---

## Features

| Feature | Detail |
|---|---|
| **Outbound SIP INVITE** | RFC 3261 — Asterisk, FreePBX, FusionPBX, any SIP server |
| **SIP Digest Authentication** | Auto 401/407 retry with MD5 Digest (qop=auth) |
| **WAV Playback over RTP** | Any sample rate / bit depth — auto G.711 conversion |
| **Codec Auto-Negotiation** | Reads SDP answer — picks PCMU (PT=0) or PCMA (PT=8) |
| **RFC 2833 DTMF Detection** | Detects telephone-event PT=101 packets in real time |
| **Blind Call Transfer** | SIP REFER on configured DTMF digit |
| **Re-INVITE Handling** | 100 Trying + 200 OK + SDP for mid-call re-INVITEs |
| **Transport Auto-Detection** | `--transport auto` — probes UDP then TCP |
| **Retry with Backoff** | Exponential backoff — configurable attempts and delay |
| **Concurrent Calls** | `make_calls_concurrent()` thread-pool dialler |
| **JSON Call Logging** | Per-call records: status, duration, DTMF, transfer target |
| **Graceful Ctrl+C** | CANCEL (ringing) or BYE (answered) on interrupt |
| **Zero Dependencies** | 100% Python standard library |
| **Python 3.8–3.13** | All modern Python versions supported |

---

## Use Cases

- **Automated voice notifications** — call customers and play a recorded message
- **IVR (Interactive Voice Response)** — detect keypress and transfer the call
- **SIP connectivity testing** — verify Asterisk is reachable before deploying
- **Bulk outbound dialling** — run 10+ concurrent calls via thread pool
- **VoIP bot / autodialler** — Python-controlled call flow with DTMF logic
- **Asterisk integration testing** — test your dialplan without a SIP phone
- **Python SIP INVITE sender** — send raw SIP INVITE with full auth handling

---

## Full Usage Examples

### 1. DTMF-triggered blind transfer

Press **0** or **1** → call is blind-transferred to another number:

```bash
siprobo \
  --host           YOUR_ASTERISK_IP \
  --phone          15551234567 \
  --wav            message.wav \
  --caller-id      1001 \
  --auth-user      1001 \
  --auth-pass      yourpassword \
  --forward-number 15559876543 \
  --forward-digits 0,1
```

### 2. Retry on failure with exponential backoff

```bash
siprobo \
  --host        YOUR_ASTERISK_IP \
  --phone       15551234567 \
  --wav         message.wav \
  --auth-user   1001 \
  --auth-pass   yourpassword \
  --max-retries 3 \
  --retry-delay 5
```

Waits: 5 s → 10 s → 20 s between attempts.

### 3. Auto transport + JSON logging + verbose SIP trace

```bash
siprobo \
  --host      YOUR_ASTERISK_IP \
  --phone     15551234567 \
  --wav       message.wav \
  --auth-user 1001 \
  --auth-pass yourpassword \
  --transport auto \
  --log-file  calls.log \
  -v
```

### 4. Concurrent bulk dialling (Python API)

```python
from siprobo.caller import make_calls_concurrent

numbers = ["15551001001", "15551001002", "15551001003", "15551001004"]

configs = [
    {
        "host": "YOUR_ASTERISK_IP", "phone": num, "wav_path": "message.wav",
        "caller_id": "1001", "auth_user": "1001", "auth_pass": "yourpassword",
    }
    for num in numbers
]

results = make_calls_concurrent(configs, max_workers=10)
print(f"{sum(results)}/{len(results)} calls answered")
```

### 5. Retry with Python API

```python
from siprobo.caller import make_call_with_retry

answered = make_call_with_retry(
    max_retries    = 3,
    retry_delay    = 5.0,
    backoff_factor = 2.0,
    host      = "YOUR_ASTERISK_IP",
    phone     = "15551234567",
    wav_path  = "message.wav",
    auth_user = "1001",
    auth_pass = "yourpassword",
)
```

### 6. SIP connectivity diagnostics

```bash
# Check if Asterisk is reachable (ICMP, TCP, UDP, SIP OPTIONS)
siprobo-diag --host YOUR_ASTERISK_IP --auth-user 1001 --auth-pass yourpassword

# Run a live INVITE test and auto-hang-up after 5 seconds
siprobo-diag --host YOUR_ASTERISK_IP --auth-user 1001 --auth-pass yourpassword \
             --call 15551234567 --hangup-after 5 -v
```

---

## All CLI Options

### `siprobo` — Make a Call

| Option | Default | Description |
|---|---|---|
| `--host` | **required** | Asterisk IP or hostname |
| `--phone` | **required** | Destination number / extension |
| `--wav` | **required** | WAV file to play (any format) |
| `--port` | `5060` | SIP port |
| `--transport` | `udp` | `udp`, `tcp`, or `auto` |
| `--caller-id` | _(empty)_ | SIP From extension |
| `--auth-user` | _(empty)_ | Digest auth username |
| `--auth-pass` | _(empty)_ | Digest auth password |
| `--source-ip` | auto | Local IP to bind |
| `--dial-prefix` | _(empty)_ | Prefix before phone in Request-URI |
| `--ring-timeout` | `60` | Seconds to wait for answer |
| `--forward-number` | _(empty)_ | Transfer target on DTMF |
| `--forward-digits` | `0` | Digits that trigger transfer |
| `--max-retries` | `1` | Total call attempts |
| `--retry-delay` | `5.0` | Seconds between retries |
| `--backoff-factor` | `2.0` | Exponential backoff multiplier |
| `--log-file` | _(none)_ | JSON call log file |
| `-v` / `--verbose` | off | Print raw SIP messages |

### `siprobo-diag` — Test Connectivity

| Option | Default | Description |
|---|---|---|
| `--host` | **required** | Asterisk IP or hostname |
| `--port` | `5060` | SIP port |
| `--auth-user` | _(empty)_ | Digest auth username |
| `--auth-pass` | _(empty)_ | Digest auth password |
| `--call` | _(none)_ | Run live INVITE to this number |
| `--hangup-after` | `0` | Seconds after answer to BYE |
| `--skip-ping` | off | Skip ICMP ping test |
| `-v` | off | Verbose SIP output |

---

## Asterisk Configuration

### PJSIP endpoint (`pjsip.conf`)

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
exten => _X.,1,NoOp(siprobo outbound call to ${EXTEN})
 same =>       n,Dial(PJSIP/${EXTEN}@your-trunk-name)
 same =>       n,Hangup()
```

```bash
asterisk -rx "dialplan reload"
asterisk -rx "pjsip reload"
```

---

## WAV File Support

| Input | Output |
|---|---|
| Any sample rate (8kHz, 16kHz, 44.1kHz, 48kHz…) | 8 000 Hz |
| Mono or stereo | Mono |
| 8-bit, 16-bit, 24-bit, 32-bit PCM | G.711 PCMU or PCMA |

Codec (PCMU vs PCMA) is **automatically selected** from the SDP answer — zero config needed.

---

## Call Log Format

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

| Problem | Cause | Solution |
|---|---|---|
| `audioop not found` | Python 3.13+ | `pip install audioop-lts` |
| 401/407 challenge loop | Wrong credentials | Check `--auth-user` / `--auth-pass` |
| 404 Not Found | Dialplan mismatch | Verify `--dial-prefix` and context |
| No answer (timeout) | Firewall or wrong IP | Run `siprobo-diag` first |
| One-way audio | NAT / RTP firewall | Open UDP ports 20000–29999 |
| WAV sounds distorted | Codec mismatch | Use `-v` to check negotiated codec |

---

## Supported PBX / SIP Servers

| Server | Status |
|---|---|
| Asterisk 16 / 18 / 20 / 21 | ✅ Tested |
| FreePBX | ✅ Compatible |
| FusionPBX / FreeSWITCH | ✅ Compatible |
| Any RFC 3261 SIP server | ✅ Should work |

---

## Project Structure

```
siprobo/
├── siprobo/
│   ├── __init__.py      # Package root, version, author
│   ├── sip_core.py      # SIP/RTP protocol + siprobo-diag CLI
│   └── caller.py        # Call engine, DTMF, RTP + siprobo CLI
├── README.md
├── LICENSE              # MIT
└── pyproject.toml       # PEP 621 build config
```

---

## Contributing

Pull requests are welcome at **[github.com/matifkhatri/siprobo](https://github.com/matifkhatri/siprobo)**.

```bash
git checkout -b feature/my-feature
git commit -m "Add my feature"
git push origin feature/my-feature
# then open a Pull Request
```

---

## Author

**matifkhatri** — [github.com/matifkhatri](https://github.com/matifkhatri)

---

## License

MIT — see [LICENSE](LICENSE) for full text.

---

## Tags & Keywords

`python sip client` · `python sip dialler` · `asterisk python` · `voip python` ·
`sip autodialler python` · `python make phone call` · `sip invite python` ·
`asterisk autodialer` · `rtp wav playback python` · `dtmf detection python` ·
`sip blind transfer python` · `g711 python` · `python pbx` · `ivr python` ·
`python voip bot` · `sip refer python` · `rfc3261 python` · `rfc2833 dtmf` ·
`python call asterisk` · `asterisk sip python library` · `siprobo`
