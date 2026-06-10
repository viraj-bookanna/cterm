"""File transfer between the local machine and the Colab runtime.

Uses the standard Jupyter contents API (``/api/contents/{path}``) exposed
by the runtime proxy.  Files are transferred base64-encoded so binary files
(images, model weights, zip archives, etc.) round-trip intact.

Push (local -> runtime):
    cterm push ./weights.pt /content/weights.pt
    cterm push ./my_dir      /content/my_dir      # recursive

Pull (runtime -> local):
    cterm pull /content/output.csv ./output.csv
    cterm pull /content/results    ./results      # recursive directory
"""

from __future__ import annotations

import base64
from pathlib import Path

import requests

from .client import ColabClient
from .utils import err, log


# ---------------------------------------------------------------------------
# Push helpers (local -> runtime)
# ---------------------------------------------------------------------------

def _push_file(client: ColabClient, local: Path, remote: str) -> None:
    raw = local.read_bytes()
    content = base64.b64encode(raw).decode("ascii")
    model = {"type": "file", "format": "base64", "content": content}
    client.contents_put(remote, model)
    log(f"  uploaded {local} -> {remote} ({len(raw):,} bytes)")


def _push_dir(client: ColabClient, local: Path, remote: str) -> None:
    # Ensure the remote directory exists.
    try:
        client.contents_put(remote, {"type": "directory"})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            pass  # already exists
        else:
            raise

    for child in sorted(local.iterdir()):
        child_remote = remote.rstrip("/") + "/" + child.name
        if child.is_dir():
            _push_dir(client, child, child_remote)
        else:
            _push_file(client, child, child_remote)


def push(
    client: ColabClient,
    local_path: str,
    remote_path: str | None = None,
) -> int:
    """Push a local file or directory to the Colab runtime."""
    local = Path(local_path).expanduser().resolve()
    if not local.exists():
        err(f"Local path does not exist: {local}")
        return 1

    if remote_path is None:
        remote_path = "/content/" + local.name

    log(f"Pushing {local} -> {remote_path} ...")
    try:
        if local.is_dir():
            _push_dir(client, local, remote_path)
        else:
            _push_file(client, local, remote_path)
    except requests.RequestException as exc:
        err(f"Upload failed: {exc}")
        return 1

    log("Push complete.")
    return 0


# ---------------------------------------------------------------------------
# Pull helpers (runtime -> local)
# ---------------------------------------------------------------------------

def _pull_file(client: ColabClient, remote: str, local: Path) -> None:
    entry = client.contents_get(remote, content=True)
    fmt = entry.get("format", "text")
    raw_content = entry.get("content", "")

    local.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "base64":
        local.write_bytes(base64.b64decode(raw_content))
    else:
        local.write_text(raw_content, encoding="utf-8")
    log(f"  downloaded {remote} -> {local} ({local.stat().st_size:,} bytes)")


def _pull_dir(client: ColabClient, remote: str, local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    entry = client.contents_get(remote, content=True)
    children = entry.get("content") or []
    for child in children:
        child_name = child.get("name", "")
        child_remote = remote.rstrip("/") + "/" + child_name
        child_local = local / child_name
        if child.get("type") == "directory":
            _pull_dir(client, child_remote, child_local)
        else:
            _pull_file(client, child_remote, child_local)


def pull(
    client: ColabClient,
    remote_path: str,
    local_path: str | None = None,
) -> int:
    """Pull a file or directory from the Colab runtime."""
    remote_name = remote_path.rstrip("/").split("/")[-1] or "content"
    if local_path is None:
        local_path = remote_name

    local = Path(local_path).expanduser()

    log(f"Pulling {remote_path} -> {local} ...")
    try:
        entry = client.contents_get(remote_path, content=False)
        entry_type = entry.get("type", "file")
        if entry_type == "directory":
            _pull_dir(client, remote_path, local)
        else:
            _pull_file(client, remote_path, local)
    except requests.RequestException as exc:
        err(f"Download failed: {exc}")
        return 1

    log("Pull complete.")
    return 0
