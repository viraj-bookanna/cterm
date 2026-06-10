"""Central TLS helpers that force the OS/system CA trust store.

``requests``/``urllib3`` default to the bundled ``certifi`` CA set and ignore
the system trust store.  On machines with a corporate TLS-inspection proxy or
a custom root CA this causes ``SSLEOFError`` / ``SSLError`` even though
``truststore.inject_into_ssl()`` patches the stdlib ``ssl`` module.

Use :func:`make_ssl_context` for ``websocket-client`` (pass via
``sslopt={"context": make_ssl_context()}``) and mount
:class:`TruststoreAdapter` on every ``requests.Session``.
"""

from __future__ import annotations

import ssl

import truststore
from requests.adapters import HTTPAdapter


def make_ssl_context() -> ssl.SSLContext:
    """Return an SSLContext verified against the OS trust store."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ctx


class TruststoreAdapter(HTTPAdapter):
    """An HTTPAdapter that validates certificates against the OS trust store.

    Mount on a ``requests.Session`` for both http and https prefixes:

        session.mount("https://", TruststoreAdapter())
    """

    def init_poolmanager(self, *args, **kwargs) -> None:  # type: ignore[override]
        kwargs["ssl_context"] = make_ssl_context()
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):  # type: ignore[override]
        proxy_kwargs["ssl_context"] = make_ssl_context()
        return super().proxy_manager_for(proxy, **proxy_kwargs)
