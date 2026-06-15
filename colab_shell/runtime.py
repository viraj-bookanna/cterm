"""Runtime allocation, keep-alive, and teardown."""

from __future__ import annotations

import json
import sys
import threading

import requests

from .client import ColabClient
from .constants import KEEP_ALIVE_INTERVAL
from .utils import err, log


def server_id_of(d: dict) -> str | None:
    """Extract the runtime/server identifier from an assignment dict.

    The extension uses ``endpoint`` everywhere; older shapes used other names.
    """
    return (
        d.get("endpoint")
        or d.get("serverId")
        or d.get("server_id")
        or d.get("vmName")
    )


class RuntimeManager:
    """Allocates (or discovers) a Colab runtime and keeps it alive."""

    def __init__(self, client: ColabClient) -> None:
        self.client = client
        self.server_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _extract_proxy_info(self, assignment: dict) -> None:
        """Seed proxy token/url from the assignment, then refresh via GAPI."""
        rpi = assignment.get("runtimeProxyInfo")
        if isinstance(rpi, dict):
            if rpi.get("token"):
                self.client.proxy_token = rpi["token"]
            if rpi.get("url"):
                self.client.proxy_url = rpi["url"]

        # Always refresh to get a live token (the one in the assignment may
        # have been minted during a previous session and could be expired).
        server_id = server_id_of(assignment)
        if server_id:
            fresh = self.client.refresh_proxy_token(server_id)
            if fresh:
                log("Runtime proxy token refreshed.")
            else:
                log("Using cached runtime proxy token.")

    def get_or_create_runtime(
        self,
        force_new: bool = False,
        variant: str | None = None,
        accelerator: str | None = None,
    ) -> str:
        """Find or allocate a runtime.

        If ``force_new`` is False, an existing runtime is reused regardless of
        the requested type (switching type requires ``--new``).
        ``variant`` / ``accelerator`` are the raw API values and are forwarded
        verbatim to ``assign()``; both ``None`` gives a CPU runtime.
        """
        if force_new:
            log("Forcing a new Colab runtime (ignoring existing ones)...")
        else:
            log("Checking for existing Colab runtimes...")
            assignments = self.client.list_assignments()
            if assignments:
                a = assignments[0]
                self.server_id = server_id_of(a)
                self._extract_proxy_info(a)
                if self.server_id:
                    log(f"Found existing runtime: {self.server_id[:12]}...")
                    return self.server_id

        log("Allocating new Colab runtime (this may take 30-60 s)...")
        result = self.client.assign(variant=variant, accelerator=accelerator)
        self.server_id = server_id_of(result)
        self._extract_proxy_info(result)
        if not self.server_id:
            err(
                "Could not extract server ID from assignment:\n"
                + json.dumps(result, indent=2)
            )
            sys.exit(1)

        log(f"Runtime allocated: {self.server_id[:12]}...")
        return self.server_id

    # -- keep-alive -------------------------------------------------------

    def start_keep_alive(self) -> None:
        """Ping the runtime on a fixed interval so it doesn't idle out.

        The wait is interruptible via an Event so the thread stops promptly
        when the session ends.
        """
        self._stop.clear()

        def _worker() -> None:
            while not self._stop.is_set() and self.server_id:
                self.client.keep_alive(self.server_id)
                # Responsive sleep: wakes immediately when stop is set.
                self._stop.wait(KEEP_ALIVE_INTERVAL)

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def stop_keep_alive(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    # -- teardown ---------------------------------------------------------

    def delete_runtime(self) -> bool:
        """Release the current runtime to stop consuming usage hours."""
        if not self.server_id:
            return False
        self.stop_keep_alive()
        try:
            log(f"Deleting runtime {self.server_id[:12]}...")
            self.client.unassign(self.server_id)
            log("Runtime deleted.")
            return True
        except requests.RequestException as exc:
            err(f"Failed to delete runtime: {exc}")
            return False
