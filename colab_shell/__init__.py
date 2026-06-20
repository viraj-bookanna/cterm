"""cterm: drop into a Google Colab terminal from your local machine.

Authenticates with Google using the same OAuth2 flow as the official Colab
VS Code extension, allocates (or reuses) a Colab runtime, and bridges your
local terminal to the runtime's /colab/tty WebSocket.
"""

from __future__ import annotations

import os
import warnings

import urllib3

# Suppress urllib3's InsecureRequestWarning — truststore already injects the
# OS trust store, so unverified-cert warnings are noise rather than signal.
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

# Use the OS trust store for TLS so corporate / OS-managed root CAs work.
import truststore

truststore.inject_into_ssl()

# Google may add `openid` to the granted scopes, which otherwise makes
# google-auth-oauthlib raise "Scope has changed". Relax it before the
# oauthlib machinery is imported/used.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

__version__ = "1.1.0"

__all__ = ["__version__"]
