"""
siprobo
=======
SIP auto-dialler with WAV playback, RFC 2833 DTMF detection,
and blind call-transfer via Asterisk.

Sub-modules
-----------
siprobo.sip_core
    Low-level SIP protocol helpers, Digest auth, and the
    ``siprobo-diag`` command-line diagnostic tool.

siprobo.caller
    High-level call-flow: INVITE, RTP WAV streaming, DTMF listen,
    BYE/REFER handling, and the ``siprobo`` command-line dialler.
"""

__version__ = "1.0.0"
__author__ = "siprobo contributors"
__license__ = "MIT"

__all__ = [
    "sip_core",
    "caller",
]
