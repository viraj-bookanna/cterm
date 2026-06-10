"""HTTP client mirroring the calls the Colab VS Code extension makes."""

from __future__ import annotations

import json
import uuid
from urllib.parse import quote

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
    HDR_PROXY_TOKEN,
    HDR_XSRF,
    TUNNEL_PREFIX,
)
from .tls import TruststoreAdapter
from .utils import log, notebook_hash, strip_xss


class ColabClient:
    """Mirrors the HTTP calls the VS Code extension makes."""

    def __init__(self, auth: ColabAuth) -> None:
        self.auth = auth
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.session.mount("https://", TruststoreAdapter())
        self.session.mount("http://", TruststoreAdapter())
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

    def _proxy_headers(self) -> dict[str, str]:
        """Headers for requests to the runtime proxy (no auth, proxy token)."""
        if not self.proxy_token:
            raise RuntimeError("proxy_token not set")
        return {HDR_PROXY_TOKEN: self.proxy_token}

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

    def refresh_proxy_token(self, server_id: str, port: str = "8080") -> str | None:
        """Fetch a fresh runtime proxy token (mirrors refreshConnection).

        Hits ``COLAB_GAPI/v1/runtime-proxy-token?endpoint=...&port=<port>``
        and stores both the token and the proxy base URL on self.
        """
        url = f"{COLAB_GAPI}/v1/runtime-proxy-token"
        try:
            resp = self.session.get(
                url,
                params={"endpoint": server_id, "port": port},
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

    def get_proxy_token_for_port(self, server_id: str, port: int | str) -> dict | None:
        """Fetch a proxy token for an arbitrary VM port without mutating self.

        Returns ``{"token": ..., "url": ...}`` or None on failure.
        Used by cterm proxy to front a custom port on the VM.
        """
        url = f"{COLAB_GAPI}/v1/runtime-proxy-token"
        try:
            resp = self.session.get(
                url,
                params={"endpoint": server_id, "port": str(port)},
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = json.loads(strip_xss(resp.text))
            if isinstance(data, dict) and data.get("token"):
                return {"token": data["token"], "url": data.get("url", "")}
        except requests.RequestException as exc:
            log(f"Could not get proxy token for port {port}: {exc}")
        return None

    # -- keep-alive -------------------------------------------------------

    def keep_alive(self, server_id: str) -> None:
        url = f"{COLAB_API}{TUNNEL_PREFIX}/{server_id}/keep-alive/?authuser=0"
        try:
            self.session.get(url, headers=self._headers(), timeout=10)
        except requests.RequestException:
            pass

    # -- runtime resource monitoring --------------------------------------

    def get_resources(self) -> dict:
        """Fetch RAM, disk, and GPU usage from the runtime.

        Returns a dict with keys:
          memory: {totalBytes, freeBytes}
          disks:  [{filesystem: {label, totalBytes, usedBytes}}]
          gpus:   [{name, memoryUsedBytes, memoryTotalBytes,
                    gpuUtilization, memoryUtilization}]
        """
        if not self.proxy_url:
            raise RuntimeError("proxy_url not set")
        url = self.proxy_url.rstrip("/") + "/api/colab/resources"
        resp = self.session.get(url, headers=self._proxy_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # -- Jupyter contents API (file operations) ---------------------------

    def _contents_url(self, path: str) -> str:
        if not self.proxy_url:
            raise RuntimeError("proxy_url not set")
        encoded = quote(path.lstrip("/"), safe="/")
        return self.proxy_url.rstrip("/") + "/api/contents/" + encoded

    def contents_list(self, path: str = "") -> list[dict]:
        """List the contents of a directory on the runtime."""
        url = self._contents_url(path)
        resp = self.session.get(
            url,
            headers=self._proxy_headers(),
            params={"content": "1"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("content", []) if isinstance(data, dict) else []

    def contents_get(self, path: str, content: bool = True) -> dict:
        """Get a file or directory entry from the runtime.

        With content=True, the response includes a ``content`` field:
        base64-encoded for binary files, plain text for text files.
        ``format`` is ``"base64"`` or ``"text"``.
        ``type`` is ``"file"``, ``"directory"``, or ``"notebook"``.
        """
        url = self._contents_url(path)
        resp = self.session.get(
            url,
            headers=self._proxy_headers(),
            params={"content": "1" if content else "0"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def contents_put(self, path: str, model: dict) -> dict:
        """Create or overwrite a file/directory on the runtime.

        ``model`` must have at least ``type`` (``"file"`` or ``"directory"``).
        For files, also include ``format`` (``"base64"`` or ``"text"``) and
        ``content`` (base64 string or plain text string).
        """
        url = self._contents_url(path)
        hdrs = {**self._proxy_headers(), "Content-Type": "application/json"}
        resp = self.session.put(url, headers=hdrs, json=model, timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    def contents_delete(self, path: str) -> None:
        """Delete a file or directory on the runtime."""
        url = self._contents_url(path)
        resp = self.session.delete(url, headers=self._proxy_headers(), timeout=30)
        resp.raise_for_status()

    # -- credential propagation (used for Drive mount) --------------------

    def propagate_credentials(
        self, server_id: str, auth_type: str, dry_run: bool = True
    ) -> dict:
        """Propagate Google credentials to the runtime (mirrors propagateCredentials).

        Used to enable headless Google Drive mounting. ``auth_type`` is
        ``"dfs_ephemeral"`` for Drive access or ``"auth_user_ephemeral"`` for
        broader Google Cloud access.

        Returns ``{"success": bool, "unauthorizedRedirectUri": str | None}``.
        """
        url = (
            f"{COLAB_API}{TUNNEL_PREFIX}/credentials-propagation"
            f"/{server_id}?authuser=0"
        )
        params = {
            "authtype": auth_type,
            "version": "2",
            "dryrun": "true" if dry_run else "false",
            "propagate": "true",
            "record": "false",
        }
        # Step 1: GET to obtain xsrf token.
        full_url = url + "&" + "&".join(f"{k}={v}" for k, v in params.items())
        get_resp = self.session.get(
            full_url, headers=self._headers(), timeout=30
        )
        get_resp.raise_for_status()
        get_data = json.loads(strip_xss(get_resp.text))
        xsrf_token = get_data.get("token", "") if isinstance(get_data, dict) else ""

        # Step 2: POST with the xsrf token.
        post_resp = self.session.post(
            full_url,
            headers={**self._headers(), HDR_XSRF: xsrf_token},
            timeout=30,
        )
        post_resp.raise_for_status()
        result = json.loads(strip_xss(post_resp.text))
        if not isinstance(result, dict):
            return {"success": False, "unauthorizedRedirectUri": None}
        # Normalise camelCase key from extension's zod transform.
        redirect = result.get("unauthorizedRedirectUri") or result.get(
            "unauthorized_redirect_uri"
        )
        return {"success": bool(result.get("success")), "unauthorizedRedirectUri": redirect}
