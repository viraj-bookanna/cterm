"""Constants extracted from the Google Colab VS Code extension (v0.8.1)."""

from __future__ import annotations

from pathlib import Path

CLIENT_ID = (
    "1014160490159-cvot3bea7tgkp72a4m29h20d9ddo6bne.apps.googleusercontent.com"
)
CLIENT_SECRET = "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"

COLAB_API = "https://colab.research.google.com"
COLAB_GAPI = "https://colab.pa.googleapis.com"
TUNNEL_PREFIX = "/tun/m"

# Per-request headers the extension sends on every issueRequest call.
EXTENSION_VERSION = "0.8.1"
APP_NAME = "Cursor"
HDR_CLIENT_AGENT = ("X-Colab-Client-Agent", "vscode")
HDR_APP_NAME = "X-Colab-VS-Code-App-Name"
HDR_EXT_VERSION = "X-Colab-VS-Code-Extension-Version"
HDR_XSRF = "X-Goog-Colab-Token"  # xsrf token header for assign/unassign POST
HDR_PROXY_TOKEN = "X-Colab-Runtime-Proxy-Token"

SCOPES = [
    "email",
    "https://www.googleapis.com/auth/colaboratory",
    "profile",
]

TOKEN_DIR = Path.home() / ".colab-shell"
TOKEN_FILE = TOKEN_DIR / "token.json"

XSS_PREFIX = ")]}'\n"

# How often (seconds) to ping the runtime to keep it from idling out.
KEEP_ALIVE_INTERVAL = 30
