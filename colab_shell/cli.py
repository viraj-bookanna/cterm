"""Command-line interface for cterm."""

from __future__ import annotations

import argparse
import sys

import requests
import websocket

from . import __version__
from .auth import ColabAuth
from .bridge import ColabTtyBridge
from .client import ColabClient
from .runtime import RuntimeManager, server_id_of
from .utils import err, log


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_client(force_reauth: bool = False) -> ColabClient:
    if force_reauth:
        ColabAuth.clear_cache()
    auth = ColabAuth(force_reauth=force_reauth)
    auth.ensure_authenticated()
    log("Authenticated.")
    return ColabClient(auth)


def _resolve_runtime(
    client: ColabClient,
    force_new: bool = False,
) -> tuple[RuntimeManager, str]:
    """Find or allocate a runtime; return (manager, server_id).

    Exits with code 1 if no proxy URL could be obtained.
    """
    runtime = RuntimeManager(client)
    server_id = runtime.get_or_create_runtime(force_new=force_new)
    if not client.proxy_url:
        err("Could not obtain runtime proxy URL.")
        sys.exit(1)
    return runtime, server_id


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_connect(args: argparse.Namespace) -> int:
    print("=" * 60)
    print("  cterm: Colab terminal on your machine")
    print("=" * 60)
    print()

    client = _make_client(force_reauth=args.reauth)
    runtime, server_id = _resolve_runtime(client, force_new=args.new)

    # Optional: mount Google Drive before entering the shell.
    if getattr(args, "mount_drive", False):
        from .drive import get_mount_command, mount_drive
        if not mount_drive(client, server_id):
            err("Drive mount failed; continuing without Drive.")

    runtime.start_keep_alive()

    # Build startup commands sent ~1.5 s after the shell is ready so that
    # tmux is fully initialised before we touch its settings.
    startup: list[str] = [
        "tmux set -g status off 2>/dev/null\r",
        "clear\r",
    ]
    if getattr(args, "mount_drive", False):
        startup.insert(0, get_mount_command())

    bridge = ColabTtyBridge(client, startup_cmds=startup)
    try:
        try:
            bridge.connect()
        except (websocket.WebSocketException, OSError, RuntimeError) as exc:
            err(f"Could not connect to Colab TTY: {exc}")
            err("The runtime may still be starting up - try again shortly.")
            return 1

        try:
            bridge.run()
        except (websocket.WebSocketException, OSError) as exc:
            err(f"Terminal session ended: {exc}")
            return 1
    finally:
        runtime.stop_keep_alive()
        if args.keep:
            log("Leaving runtime running (--keep). Use 'cterm kill' "
                "to remove it later.")
        else:
            runtime.delete_runtime()

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    client = _make_client(force_reauth=args.reauth)
    try:
        assignments = client.list_assignments()
    except requests.RequestException as exc:
        err(f"Could not list runtimes: {exc}")
        return 1

    if not assignments:
        log("No active Colab runtimes.")
        return 0

    print(f"\nActive Colab runtimes ({len(assignments)}):\n")
    for i, a in enumerate(assignments, 1):
        sid = server_id_of(a) or "(unknown)"
        accel = a.get("accelerator", "none")
        variant = a.get("variant", "")
        shape = a.get("machineShape", "")
        tier = a.get("subscriptionTier", "")
        extra = " ".join(x for x in (variant, shape, tier) if x)
        print(f"  {i}. {sid}")
        print(f"       accelerator={accel}  {extra}".rstrip())
    print()
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    client = _make_client(force_reauth=args.reauth)
    try:
        assignments = client.list_assignments()
    except requests.RequestException as exc:
        err(f"Could not list runtimes: {exc}")
        return 1

    if not assignments:
        log("No active Colab runtimes to kill.")
        return 0

    ids = [server_id_of(a) for a in assignments]
    ids = [s for s in ids if s]

    if args.all:
        targets = ids
    elif args.id:
        matches = [s for s in ids if s == args.id or s.startswith(args.id)]
        if not matches:
            err(f"No runtime matching '{args.id}'. Use 'cterm list'.")
            return 1
        if len(matches) > 1:
            err(f"'{args.id}' is ambiguous; matches {len(matches)} runtimes:")
            for s in matches:
                err(f"  {s}")
            return 1
        targets = matches
    else:
        err("Specify a runtime id or --all. See 'cterm list'.")
        return 1

    failures = 0
    for sid in targets:
        try:
            log(f"Killing runtime {sid[:12]}...")
            client.unassign(sid)
            log("  deleted.")
        except requests.RequestException as exc:
            err(f"  failed to delete {sid}: {exc}")
            failures += 1

    return 1 if failures else 0


def cmd_logout(_args: argparse.Namespace) -> int:
    ColabAuth.clear_cache()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Display runtime resource usage (RAM, disk, GPU) with sparklines."""
    from .stats import run_stats
    client = _make_client(force_reauth=False)
    _resolve_runtime(client)
    return run_stats(client, watch=args.watch, interval=args.interval)


def cmd_push(args: argparse.Namespace) -> int:
    """Upload a local file or directory to the Colab runtime."""
    from .files import push
    client = _make_client(force_reauth=False)
    _resolve_runtime(client)
    return push(client, args.local, args.remote)


def cmd_pull(args: argparse.Namespace) -> int:
    """Download a file or directory from the Colab runtime."""
    from .files import pull
    client = _make_client(force_reauth=False)
    _resolve_runtime(client)
    return pull(client, args.remote, args.local)


def cmd_drive(_args: argparse.Namespace) -> int:
    """Mount Google Drive on the active Colab runtime."""
    from .drive import get_mount_command, mount_drive
    client = _make_client(force_reauth=False)
    _, server_id = _resolve_runtime(client)
    if not mount_drive(client, server_id):
        return 1
    log("To complete the mount, run inside the Colab shell:")
    log("  " + get_mount_command().strip())
    log("Or connect with:  cterm connect --mount-drive")
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    """[EXPERIMENTAL] Route local HTTP/SOCKS5 traffic through the Colab runtime."""
    from .proxy import run_proxy

    client = _make_client(force_reauth=False)
    runtime, _ = _resolve_runtime(client)
    runtime.start_keep_alive()

    try:
        return run_proxy(
            client=client,
            local_port=args.port,
            vm_proxy_port=args.vm_proxy_port,
            use_tor=args.tor,
        )
    finally:
        runtime.stop_keep_alive()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cterm",
        description="Drop into a Google Colab terminal from your machine.",
    )
    parser.add_argument(
        "--version", action="version", version=f"cterm {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    # connect (default)
    p_connect = sub.add_parser(
        "connect", help="Connect to a Colab runtime terminal (default)."
    )
    p_connect.add_argument(
        "--reauth",
        action="store_true",
        help="Force a fresh Google sign-in, ignoring any cached token.",
    )
    p_connect.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the runtime on exit (keeps using your hours).",
    )
    p_connect.add_argument(
        "--new",
        action="store_true",
        help="Allocate a fresh runtime even if one already exists "
        "(lets you run multiple instances at once).",
    )
    p_connect.add_argument(
        "--mount-drive",
        action="store_true",
        dest="mount_drive",
        help="Mount Google Drive automatically at /content/drive on connect.",
    )
    p_connect.set_defaults(func=cmd_connect)

    # list
    p_list = sub.add_parser("list", help="List your active Colab runtimes.")
    p_list.add_argument("--reauth", action="store_true", help=argparse.SUPPRESS)
    p_list.set_defaults(func=cmd_list)

    # kill
    p_kill = sub.add_parser(
        "kill", help="Delete a specific runtime (by id) or all of them."
    )
    p_kill.add_argument(
        "id",
        nargs="?",
        help="Runtime id (or unique prefix) to delete. See 'list'.",
    )
    p_kill.add_argument(
        "--all", action="store_true", help="Delete all active runtimes."
    )
    p_kill.add_argument("--reauth", action="store_true", help=argparse.SUPPRESS)
    p_kill.set_defaults(func=cmd_kill)

    # logout
    p_logout = sub.add_parser("logout", help="Clear cached credentials.")
    p_logout.set_defaults(func=cmd_logout)

    # stats
    p_stats = sub.add_parser(
        "stats", help="Show runtime resource usage (RAM, disk, GPU)."
    )
    p_stats.add_argument(
        "--watch",
        action="store_true",
        help="Keep refreshing until Ctrl+C.",
    )
    p_stats.add_argument(
        "--interval",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Refresh interval in seconds when --watch is active (default: 2).",
    )
    p_stats.set_defaults(func=cmd_stats)

    # push
    p_push = sub.add_parser(
        "push", help="Upload a local file or directory to the runtime."
    )
    p_push.add_argument("local", help="Local file or directory to upload.")
    p_push.add_argument(
        "remote",
        nargs="?",
        default=None,
        help="Destination path on the runtime (default: /content/<name>).",
    )
    p_push.set_defaults(func=cmd_push)

    # pull
    p_pull = sub.add_parser(
        "pull", help="Download a file or directory from the runtime."
    )
    p_pull.add_argument("remote", help="Path on the runtime to download.")
    p_pull.add_argument(
        "local",
        nargs="?",
        default=None,
        help="Local destination path (default: ./<name>).",
    )
    p_pull.set_defaults(func=cmd_pull)

    # drive
    p_drive = sub.add_parser(
        "drive",
        help="Propagate Google Drive credentials to the runtime. "
        "Use --mount-drive on connect to auto-mount.",
    )
    p_drive.set_defaults(func=cmd_drive)

    # proxy (experimental)
    p_proxy = sub.add_parser(
        "proxy",
        help="[EXPERIMENTAL] Local HTTP/SOCKS5 proxy through the Colab runtime.",
    )
    p_proxy.add_argument(
        "--port",
        type=int,
        default=1080,
        help="Local TCP port to listen on (default: 1080).",
    )
    p_proxy.add_argument(
        "--vm-proxy-port",
        type=int,
        default=8764,
        dest="vm_proxy_port",
        help="Port pproxy listens on inside the VM (default: 8764).",
    )
    p_proxy.add_argument(
        "--tor",
        action="store_true",
        default=False,
        help=(
            "Route VM traffic through the Tor network. "
            "Tor is installed automatically on the VM if absent. "
            "First run is slow (~2 min for apt install + Tor bootstrap)."
        ),
    )
    p_proxy.set_defaults(func=cmd_proxy)

    return parser


_COMMANDS = {
    "connect", "list", "kill", "logout",
    "stats", "push", "pull", "drive", "proxy",
}
_TOP_LEVEL = {"-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Default to the `connect` command when no subcommand is given, so that
    # `cterm` and `cterm --reauth` both work.
    if not argv:
        argv = ["connect"]
    elif argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL:
        # If the first token looks like a subcommand attempt (no leading dash)
        # but isn't one we recognise, show a clear error rather than silently
        # treating it as a `connect` argument.
        if not argv[0].startswith("-"):
            err(
                f"Unknown command '{argv[0]}'. "
                f"Valid commands: {', '.join(sorted(_COMMANDS))}."
            )
            sys.exit(1)
        argv = ["connect", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
