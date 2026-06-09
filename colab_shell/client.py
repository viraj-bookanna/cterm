"""HTTP client mirroring the calls the Colab VS Code extension makes."""

from __future__ import annotations

import json
import uuid

import requests

from .auth import ColabAuth
from .constants import (
    APP_NAME,
    COLAB_API,
    COLAB_GAPI,
    EXTENSION_VERSION,
    HDR_APP_NAME,
    HDR_CLIENT_AGENT,
    HDR_EXT_VERSION,
    HDR_XSRF,
    TUNNEL_PREFIX,
)
from .utils import log, notebook_hash, strip_xss


class ColabClient:
    """Mirrors the HTTP calls the VS Code extension makes."""

    def __init__(self, auth: ColabAuth) -> None:
        self.auth = auth
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # Set once a runtime is assigned/reused.
        self.proxy_token: str | None = None
        # Base URL for the runtime's proxy (from runtimeProxyInfo.url),
        # used to build the /colab/tty WebSocket URL.
        self.proxy_url: str | None = None

    def _headers(self) -> dict[str, str]:
        return {
            **self.auth.headers(),
            "Content-Type": "application/json",
            HDR_CLIENT_AGENT[0]: HDR_CLIENT_AGENT[1],
            HDR_APP_NAME: APP_NAME,
            HDR_EXT_VERSION: EXTENSION_VERSION,
        }

    def _get_json(self, url: str, **kw) -> dict | list:
        resp = self.session.get(url, headers=self._headers(), timeout=30, **kw)
        resp.raise_for_status()
        return json.loads(strip_xss(resp.text))

    def _post_json(
        self,
        url: str,
        extra_headers: dict | None = None,
        **kw,
    ) -> dict | list | None:
        hdrs = self._headers()
        if extra_headers:
            hdrs.update(extra_headers)
        resp = self.session.post(url, headers=hdrs, timeout=60, **kw)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text:
            return None
        return json.loads(strip_xss(text))

    # -- user info --------------------------------------------------------

    def get_user_info(self) -> dict:
        return self._get_json(f"{COLAB_GAPI}/v1/user-info")

    # -- assignments ------------------------------------------------------

    def list_assignments(self) -> list[dict]:
        data = self._get_json(f"{COLAB_GAPI}/v1/assignments")
        return data.get("assignments", []) if isinstance(data, dict) else data

    def assign(self, notebook_id: str | None = None) -> dict:
        """Allocate a runtime, mirroring the extension's two-step assign flow.

        GET ``/tun/m/assign?nbh=...`` returns a pending assignment with an
        xsrf ``token`` field.  POST the same URL with that token to provision
        the runtime and receive the ``endpoint`` + ``runtimeProxyInfo``.
        """
        if notebook_id is None:
            notebook_id = str(uuid.uuid4())

        nbh = notebook_hash(notebook_id)
        url = f"{COLAB_API}{TUNNEL_PREFIX}/assign?nbh={nbh}&authuser=0"

        data = self._get_json(url)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected assign response: {data!r}")

        # Already assigned: endpoint present, no xsrf token to exchange.
        if data.get("endpoint") and "token" not in data:
            return data

        token = data.get("token")
        if not token:
            return data

        result = self._post_json(url, extra_headers={HDR_XSRF: token})
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected assign POST response: {result!r}")

        outcome = result.get("outcome")
        if outcome in (1, 2):
            raise RuntimeError("Insufficient quota to assign a Colab runtime.")
        if outcome == 5:
            raise RuntimeError(
                "This account is blocked from accessing Colab servers."
            )
        return result

    def unassign(self, server_id: str) -> None:
        """Delete (release) a runtime. Mirrors the extension's unassign."""
        url = f"{COLAB_API}{TUNNEL_PREFIX}/unassign/{server_id}?authuser=0"
        data = self._get_json(url)
        token = data.get("token", "") if isinstance(data, dict) else ""
        self._post_json(url, extra_headers={HDR_XSRF: token})

    # -- runtime proxy token ----------------------------------------------

    def refresh_proxy_token(self, server_id: str) -> str | None:
        """Fetch a fresh runtime proxy token (mirrors refreshConnection).

        Hits ``COLAB_GAPI/v1/runtime-proxy-token?endpoint=...&port=8080``
        and stores both the token and the proxy base URL on self.
        """
        url = f"{COLAB_GAPI}/v1/runtime-proxy-token"
        try:
            resp = self.session.get(
                url,
                params={"endpoint": server_id, "port": "8080"},
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = json.loads(strip_xss(resp.text))
            if isinstance(data, dict):
                new_token = data.get("token")
                new_url = data.get("url")
                if new_token:
                    self.proxy_token = new_token
                if new_url:
                    self.proxy_url = new_url
                return new_token or None
        except requests.RequestException as exc:
            log(f"Could not refresh proxy token: {exc}")
        return None

    # -- keep-alive -------------------------------------------------------

    def keep_alive(self, server_id: str) -> None:
        url = f"{COLAB_API}{TUNNEL_PREFIX}/{server_id}/keep-alive/?authuser=0"
        try:
            self.session.get(url, headers=self._headers(), timeout=10)
        except requests.RequestException:
            pass
