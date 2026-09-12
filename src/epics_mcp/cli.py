"""`epics-mcp` command line: policy resolution, transport choice, doctor.

Policy resolution (PLAN.md 2): a bare name with no "/" and no .yaml/.yml
suffix resolves against $EPICS_MCP_POLICY_DIR (default
~/.epics-mcp/policies/); anything else is used as a literal path.
$EPICS_MCP_POLICY supplies the default when --policy is omitted. There is
no default policy: with neither, the server refuses to start and prints
this resolution order.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from epics_mcp import __version__
from epics_mcp.ca_client import FakeBackend, PyepicsBackend
from epics_mcp.doctor import all_ok, format_report, run_doctor
from epics_mcp.policy import Policy, PolicyError

DEFAULT_POLICY_DIR = "~/.epics-mcp/policies"


def _no_policy_message() -> str:
    policy_dir = os.environ.get("EPICS_MCP_POLICY_DIR", DEFAULT_POLICY_DIR)
    return (
        "epics-mcp: no policy given -- refusing to start (no policy, no PVs; "
        "see PLAN.md 4.2).\n"
        "Resolution order:\n"
        "  1. --policy NAME_OR_PATH on the command line\n"
        "  2. $EPICS_MCP_POLICY\n"
        f"A bare NAME (no '/', no .yaml/.yml) resolves in {policy_dir!r}\n"
        "($EPICS_MCP_POLICY_DIR). Anything else is used as a literal path.\n"
        "Example: epics-mcp --policy usaxs-user"
    )


def resolve_policy_path(raw: str) -> Path:
    path = Path(raw)
    looks_like_a_path = "/" in raw or raw.endswith((".yaml", ".yml")) or path.is_absolute()
    if looks_like_a_path:
        return path.expanduser()
    policy_dir = Path(os.environ.get("EPICS_MCP_POLICY_DIR", DEFAULT_POLICY_DIR)).expanduser()
    return policy_dir / f"{raw}.yaml"


def _build_arg_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--policy",
        help="Policy name (resolved in $EPICS_MCP_POLICY_DIR) or a path to a policy YAML file. "
        "Falls back to $EPICS_MCP_POLICY if omitted.",
    )
    common.add_argument(
        "--readonly",
        action="store_true",
        help="Force read-only regardless of the policy file's mode: -- beats the file, never the reverse.",
    )

    parser = argparse.ArgumentParser(
        prog="epics-mcp",
        parents=[common],
        description="Policy-gated MCP server for EPICS Channel Access.",
    )
    parser.add_argument("--version", action="version", version=f"epics-mcp {__version__}")
    parser.add_argument(
        "--backend",
        choices=["pyepics", "fake"],
        default="pyepics",
        help="'fake' serves canned values with no EPICS installed -- for local testing/demo.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="'http' (streamable-HTTP) has no bearer-token auth yet -- PLAN.md 6 phase 7. "
        "Bind it to a LAN interface only behind something that does.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="--transport http only.")
    parser.add_argument("--port", type=int, default=8765, help="--transport http only.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the policy, print its effective rules as JSON, and exit without serving.",
    )
    parser.set_defaults(command="serve")

    subparsers = parser.add_subparsers(dest="command")
    doctor_parser = subparsers.add_parser(
        "doctor",
        parents=[common],
        help="Self-check: policy valid? catalog present? CA reachable? audit writable?",
    )
    doctor_parser.set_defaults(command="doctor")

    return parser


def _resolve_policy_or_exit(args: argparse.Namespace) -> Path:
    raw = args.policy or os.environ.get("EPICS_MCP_POLICY")
    if not raw:
        print(_no_policy_message(), file=sys.stderr)
        raise SystemExit(2)
    return resolve_policy_path(raw)


def _run_doctor(args: argparse.Namespace) -> int:
    policy_path = _resolve_policy_or_exit(args)
    results = run_doctor(policy_path, force_readonly=args.readonly)
    print(format_report(results))
    return 0 if all_ok(results) else 1


def _run_serve(args: argparse.Namespace) -> int:
    policy_path = _resolve_policy_or_exit(args)
    try:
        policy = Policy.load(policy_path, force_readonly=args.readonly)
    except PolicyError as exc:
        print(f"epics-mcp: {exc}", file=sys.stderr)
        return 2

    for warning in policy.warnings:
        print(f"epics-mcp: warning: {warning}", file=sys.stderr)

    if args.check:
        print(json.dumps(policy.describe(), indent=2, default=str))
        return 0

    # Imported here, not at module scope: importing server.py triggers
    # `from mcp.server.fastmcp import FastMCP`, and --check/doctor/--help
    # must keep working on a machine that has not installed the mcp extra.
    from epics_mcp import server

    backend = (
        FakeBackend(max_array_points=policy.max_array_points)
        if args.backend == "fake"
        else PyepicsBackend(max_array_points=policy.max_array_points)
    )
    server.configure(policy, backend=backend, client_label=f"epics-mcp-cli/{__version__}")

    if args.transport == "http":
        server.mcp.settings.host = args.host
        server.mcp.settings.port = args.port
        print(
            f"epics-mcp: serving streamable-HTTP on {args.host}:{args.port} -- "
            "no bearer-token auth yet (PLAN.md 6 phase 7); do not expose this "
            "beyond a trusted network.",
            file=sys.stderr,
        )
        server.mcp.run(transport="streamable-http")
    else:
        server.mcp.run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _run_doctor(args)
    return _run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
