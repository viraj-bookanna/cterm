"""Command-line interface for colab-shell."""

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


def _make_client(force_reauth: bool = False) -> ColabClient:
    if force_reauth:
        ColabAuth.clear_cache()
    auth = ColabAuth(force_reauth=force_reauth)
    auth.ensure_authenticated()
    log("Authenticated.")
    return ColabClient(auth)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_connect(args: argparse.Namespace) -> int:
    print("=" * 60)
    print("  colab-shell: Colab terminal on your machine")
    print("=" * 60)
    print()

    client = _make_client(force_reauth=args.reauth)

    runtime = RuntimeManager(client)
    runtime.get_or_create_runtime(force_new=args.new)

    if not client.proxy_url:
        err("Could not obtain runtime proxy URL. Cannot connect.")
        return 1

    # Keep the runtime alive for the whole interactive session.
    runtime.start_keep_alive()

    bridge = ColabTtyBridge(client)
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
        # Delete the runtime on exit by default to save usage hours.
        if args.keep:
            log("Leaving runtime running (--keep). Use 'colab-shell kill' "
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
        # Match by exact id or unique prefix.
        matches = [s for s in ids if s == args.id or s.startswith(args.id)]
        if not matches:
            err(f"No runtime matching '{args.id}'. Use 'colab-shell list'.")
            return 1
        if len(matches) > 1:
            err(f"'{args.id}' is ambiguous; matches {len(matches)} runtimes:")
            for s in matches:
                err(f"  {s}")
            return 1
        targets = matches
    else:
        err("Specify a runtime id or --all. See 'colab-shell list'.")
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


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colab-shell",
        description="Drop into a Google Colab terminal from your machine.",
    )
    parser.add_argument(
        "--version", action="version", version=f"colab-shell {__version__}"
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

    return parser


_COMMANDS = {"connect", "list", "kill", "logout"}
_TOP_LEVEL = {"-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Default to the `connect` command when no subcommand is given, so that
    # `colab-shell` and `colab-shell --reauth` both work.
    if not argv:
        argv = ["connect"]
    elif argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL:
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
