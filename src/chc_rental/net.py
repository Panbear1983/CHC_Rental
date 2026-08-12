"""TLS context for outbound HTTPS from source adapters.

Same rationale as `chc_rental.notify.telegram`: TLS on this machine is
intercepted by a locally-trusted root that Python's bundled CA store does not
know, so the macOS system bundle is preferred. Verification is never disabled.
"""

from __future__ import annotations

import os
import ssl

_SYSTEM_CA_BUNDLES = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")


def ssl_context() -> ssl.SSLContext:
    for bundle in _SYSTEM_CA_BUNDLES:
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()
