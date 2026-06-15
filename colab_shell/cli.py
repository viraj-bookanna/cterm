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
    variant: str | None = None,
    accelerator: str | None = None,
) -> tuple[RuntimeManager, str]:
    """Find or allocate a runtime; return (manager, server_id).

    ``variant`` and ``accelerator`` are passed verbatim to the assign API.
    Exits with code 1 on error or if no proxy URL could be obtained.
    """
    runtime = RuntimeManager(client)
    try:
        server_id = runtime.get_or_create_runtime(
            force_new=force_new, variant=variant, accelerator=accelerator
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        err(f"Runtime allocation failed (HTTP {status}): {exc}")
        if status == 400 and variant:
            err(
                f"The API rejected variant={variant!r}. "
                "Run `cterm types` to see valid values."
            )
        sys.exit(1)
    except (requests.RequestException, RuntimeError) as exc:
        err(f"Could not allocate runtime: {exc}")
        sys.exit(1)
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
    runtime, server_id = _resolve_runtime(
        client,
        force_new=args.new,
        variant=getattr(args, "variant", None),
        accelerator=getattr(args, "accelerator", None),
    )

    if getattr(args, "mount_drive", False):
        from .drive import mount_drive
        if not mount_drive(client, server_id):
            err("Drive mount failed; continuing without Drive.")

    runtime.start_keep_alive()

    # Startup keystrokes: clean up the tmux bar and clear the screen.
    # Drive is mounted before the bridge connects (above), so /content/drive
    # is already present when the shell first appears.
    startup: list[str] = [
        "tmux set -g status off 2>/dev/null\r",
        "clear\r",
    ]

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
            log("Leaving runtime running (--keep). Use 'cterm kill' to remove it later.")
        else:
            runtime.delete_runtime()

    return 0


def cmd_types(args: argparse.Namespace) -> int:
    """Print eligible runtime types straight from the API."""
    client = _make_client(force_reauth=False)
    try:
        info = client.get_user_info()
    except requests.RequestException as exc:
        err(f"Could not fetch runtime types: {exc}")
        return 1

    eligible = info.get("eligibleAccelerators") or []

    print("\nEligible runtime types for this account:\n")
    print(f"  {'VARIANT':<20}  ACCELERATOR(S)")
    print(f"  {'-'*20}  {'-'*20}")
    print(f"  {'(none)':<20}  CPU  (default, no flags needed)")

    for entry in eligible:
        raw_variant = entry.get("variant", "")
        # Strip VARIANT_ prefix so the displayed value matches what --variant accepts.
        variant = raw_variant[len("VARIANT_"):] if raw_variant.upper().startswith("VARIANT_") else raw_variant
        models = entry.get("models") or []
        accel_str = ", ".join(models) if models else "(none)"
        print(f"  {variant:<20}  {accel_str}")

    print()
    print("Usage:")
    print("  cterm --new                          CPU runtime (default)")
    print("  cterm --variant GPU --new            GPU, API picks accelerator")
    print("  cterm --variant GPU --accelerator T4 --new")
    print("  cterm ssh --variant GPU --accelerator T4 --new")
    print()
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
    """Mount Google Drive at /content/drive on the active runtime."""
    from .drive import mount_drive
    client = _make_client(force_reauth=False)
    _, server_id = _resolve_runtime(client)
    if not mount_drive(client, server_id):
        return 1
    log("Google Drive is mounted at /content/drive on the runtime.")
    log("Connect with 'cterm' to access it.")
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


def cmd_ssh(args: argparse.Namespace) -> int:
    """[EXPERIMENTAL] SSH shell via Colab's muxed terminal WebSocket."""
    from .ssh import run_ssh

    client = _make_client(force_reauth=getattr(args, "reauth", False))
    runtime, _ = _resolve_runtime(
        client,
        force_new=getattr(args, "new", False),
        variant=getattr(args, "variant", None),
        accelerator=getattr(args, "accelerator", None),
    )
    runtime.start_keep_alive()

    # Strip a leading '--' separator that argparse may leave in REMAINDER
    extra = list(getattr(args, "ssh_extra_args", None) or [])
    if extra and extra[0] == "--":
        extra = extra[1:]

    try:
        rc = run_ssh(
            client=client,
            local_port=getattr(args, "port", 2222),
            extra_ssh_args=extra,
        )
    finally:
        runtime.stop_keep_alive()
        if getattr(args, "keep", False):
            log("Leaving runtime running (--keep). Use 'cterm kill' to remove later.")
        else:
            runtime.delete_runtime()

    return rc


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _add_runtime_type_flags(parser: argparse.ArgumentParser) -> None:
    """Add --variant and --accelerator flags to a subparser."""
    parser.add_argument(
        "--variant",
        metavar="VARIANT",
        default=None,
        help=(
            "Runtime variant to request, e.g. VARIANT_GPU, VARIANT_TPU. "
            "Run `cterm types` to see what your account has available. "
            "Requires --new when no existing runtime is running."
        ),
    )
    parser.add_argument(
        "--accelerator",
        metavar="MODEL",
        default=None,
        help=(
            "Specific accelerator model, e.g. T4, V5E1. "
            "Only meaningful with --variant. "
            "Omit to let the API pick the default for the variant."
        ),
    )


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
        help="Allocate a fresh runtime even if one already exists.",
    )
    p_connect.add_argument(
        "--mount-drive",
        action="store_true",
        dest="mount_drive",
        help="Mount Google Drive automatically at /content/drive on connect.",
    )
    _add_runtime_type_flags(p_connect)
    p_connect.set_defaults(func=cmd_connect)

    # types
    p_types = sub.add_parser(
        "types",
        help="List eligible runtime types (variants + accelerators) for this account.",
    )
    p_types.set_defaults(func=cmd_types)

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
        help="Mount Google Drive at /content/drive on the active runtime.",
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

    # ssh (experimental)
    p_ssh = sub.add_parser(
        "ssh",
        help="[EXPERIMENTAL] Interactive SSH shell via the Colab tunnel.",
    )
    p_ssh.add_argument(
        "--port",
        type=int,
        default=2222,
        help="Local TCP port for the SSH tunnel (default: 2222).",
    )
    p_ssh.add_argument(
        "--new",
        action="store_true",
        help="Allocate a fresh runtime even if one already exists.",
    )
    p_ssh.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the runtime when the SSH session ends.",
    )
    p_ssh.add_argument("--reauth", action="store_true", help=argparse.SUPPRESS)
    _add_runtime_type_flags(p_ssh)
    p_ssh.add_argument(
        "ssh_extra_args",
        nargs=argparse.REMAINDER,
        help=(
            "Extra arguments forwarded to the ssh client (e.g. -L 1234:host:443 -N). "
            "Everything after the known cterm flags is passed through unchanged."
        ),
    )
    p_ssh.set_defaults(func=cmd_ssh)

    return parser


_COMMANDS = {
    "connect", "types", "list", "kill", "logout",
    "stats", "push", "pull", "drive", "proxy", "ssh",
}
_TOP_LEVEL = {"-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Default to the `connect` command when no subcommand is given, so that
    # `cterm`, `cterm --new`, and `cterm --variant VARIANT_GPU --new` all work.
    if not argv:
        argv = ["connect"]
    elif argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL:
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
