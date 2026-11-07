"""HTTPS certificates checked by Windows itself, not by OpenSSL reading Windows' root store.

Python's `ssl` on Windows loads the roots that are already in the store and checks the chain with
OpenSSL. Windows does not ship every root: it downloads one the first time its own chain check
needs it. A fresh Windows (a new PC, Windows Sandbox) may therefore lack the roots behind
api.holobuilder.dev or oauth2.googleapis.com, and every request fails with CERTIFICATE_VERIFY_FAILED
("unable to get local issuer certificate"), while browsers, which carry their own roots, work.
`truststore` hands the check to Windows (CertGetCertificateChain), which fetches a missing root
the way any Windows program would.

The installer calls `use_system_trust()` at start; in the app's runtime, `sitecustomize.py`
(written by build_app.py) calls it in every process: the launcher, the server and the refresh.
"""
from __future__ import annotations


def use_system_trust() -> bool:
    """Make every `ssl` context of this process verify through the OS. False when `truststore`
    is not installed (a development checkout without it keeps Python's own check)."""
    try:
        import truststore  # noqa: PLC0415
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True
