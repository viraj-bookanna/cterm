"""Google OAuth2 authentication reusing the Colab VS Code extension client."""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser

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

    def __init__(
        self,
        force_reauth: bool = False,
        no_browser: bool | None = None,
    ) -> None:
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expiry: float = 0
        self.scopes: list[str] = []
        self.force_reauth = force_reauth
        # None = auto-detect; True = never try to launch a browser; False =
        # always try (and fall back to printing the URL on failure).
        self.no_browser = no_browser

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

    @staticmethod
    def _headless_environment() -> bool:
        """Best-effort check: can we actually open a browser on this host?

        True on headless Linux (no DISPLAY/WAYLAND_DISPLAY, no browser
        registered) and anywhere ``webbrowser.get()`` cannot resolve a usable
        handler. False on desktop OSes where a default browser is set.
        """
        # Honour explicit CI / headless env vars.
        if os.environ.get("CTERM_NO_BROWSER") or os.environ.get("CI"):
            return True
        # On Linux a browser without X / Wayland cannot be opened.
        if sys.platform.startswith("linux"):
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return True
        # Final probe: see if webbrowser has any usable handler.
        try:
            webbrowser.get()
            return False
        except webbrowser.Error:
            return True

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

        if self.no_browser is None:
            open_browser = not self._headless_environment()
        else:
            open_browser = not self.no_browser

        if open_browser:
            log("Opening browser for Google sign-in (fallback: URL will be printed)...")
            prompt_msg = (
                "If the browser did not open, visit this URL manually to "
                "authorize cterm:\n\n    {url}\n"
            )
        else:
            log("Headless environment detected; printing the sign-in URL instead of opening a browser.")
            log("Pass --browser to force the browser-open attempt.")
            prompt_msg = (
                "Open this URL in a browser (on this machine or any other, "
                "e.g. your laptop) to authorize cterm:\n\n    {url}\n\n"
                "Then come back here; this process is waiting for the redirect "
                "on 127.0.0.1 (random port). If you are on a remote host, run "
                "your terminal with SSH local port-forwarding or paste the "
                "redirect URL back after signing in."
            )

        try:
            creds = flow.run_local_server(
                host="127.0.0.1",
                port=0,  # random free port, like the extension
                open_browser=open_browser,
                authorization_prompt_message=prompt_msg,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
            )
        except webbrowser.Error as exc:
            # Browser was advertised as available but open failed at runtime -
            # retry once with open_browser=False so the URL still gets printed.
            log(f"Could not open browser ({exc}); printing URL instead.")
            creds = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=False,
                authorization_prompt_message=(
                    "Open this URL in any browser to authorize cterm:\n\n"
                    "    {url}\n"
                ),
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
        # Use a dedicated session with the OS trust store adapter so that
        # certifi does not override the system CA bundle here.
        # (InstalledAppFlow for interactive login relies on inject_into_ssl().)
        from .tls import TruststoreAdapter  # late import avoids circular dep

        sess = requests.Session()
        sess.mount("https://", TruststoreAdapter())
        resp = sess.post(
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
        try:
            os.chmod(TOKEN_DIR, 0o700)
        except OSError:
            pass
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
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

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
