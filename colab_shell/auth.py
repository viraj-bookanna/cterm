"""Google OAuth2 authentication reusing the Colab VS Code extension client."""

from __future__ import annotations

import json
import time

import requests

from .constants import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    TOKEN_DIR,
    TOKEN_FILE,
)
from .utils import log


class ColabAuth:
    """OAuth2 flow reusing the Colab VS Code extension's public client.

    Mirrors the extension's loopback flow:
      - redirect to ``http://127.0.0.1:<random_port>`` with PKCE (S256)
      - ``access_type=offline`` + ``prompt=consent`` to obtain a refresh token
      - scopes ``email https://www.googleapis.com/auth/colaboratory profile``
    """

    def __init__(self, force_reauth: bool = False) -> None:
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expiry: float = 0
        self.scopes: list[str] = []
        self.force_reauth = force_reauth

    def ensure_authenticated(self) -> str:
        """Return a valid access token, refreshing or re-authing as needed."""
        if not self.force_reauth and self._try_load_cached():
            # Re-authenticate if the cached token was minted with a
            # different scope set than we now require.
            if self._scopes_match():
                if self.access_token and time.time() < self.expiry - 60:
                    return self.access_token
                if self.refresh_token:
                    try:
                        self._refresh()
                        return self.access_token  # type: ignore[return-value]
                    except (requests.RequestException, KeyError, ValueError):
                        pass
            else:
                log("Cached token scopes differ from required; re-authing...")

        self._interactive_login()
        return self.access_token  # type: ignore[return-value]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.ensure_authenticated()}"}

    def _scopes_match(self) -> bool:
        return set(self.scopes) == set(SCOPES)

    # -- interactive login ------------------------------------------------

    def _interactive_login(self) -> None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        client_config = {
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://127.0.0.1"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
        log("Opening browser for Google sign-in...")
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=0,  # random free port, like the extension
            open_browser=True,
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )
        self.access_token = creds.token
        self.refresh_token = creds.refresh_token
        self.expiry = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3600
        )
        self.scopes = list(creds.scopes) if creds.scopes else list(SCOPES)
        self._save()
        log("Sign-in complete; credentials cached.")

    # -- refresh ----------------------------------------------------------

    def _refresh(self) -> None:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            timeout=30,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.expiry = time.time() + data.get("expires_in", 3600)
        self._save()

    # -- persistence ------------------------------------------------------

    def _save(self) -> None:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expiry": self.expiry,
                    "scopes": self.scopes,
                }
            )
        )

    def _try_load_cached(self) -> bool:
        if not TOKEN_FILE.exists():
            return False
        try:
            data = json.loads(TOKEN_FILE.read_text())
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.expiry = data.get("expiry", 0)
            self.scopes = data.get("scopes", [])
            return True
        except (json.JSONDecodeError, OSError, KeyError):
            return False

    @staticmethod
    def clear_cache() -> None:
        try:
            TOKEN_FILE.unlink()
            log("Cleared cached credentials.")
        except FileNotFoundError:
            pass
