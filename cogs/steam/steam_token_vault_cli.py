#!/usr/bin/env python
"""CLI bridge for Steam token storage in Windows Credential Manager."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.steam.token_vault import (  # noqa: E402
    clear_tokens,
    get_refresh_token_age_days,
    machine_auth_token_exists,
    read_machine_auth_token,
    read_refresh_token,
    refresh_token_exists,
    token_storage_mode,
    write_machine_auth_token,
    write_refresh_token,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Steam token vault helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="Read a token")
    p_get.add_argument("--token", required=True, choices=("refresh", "machine"))

    p_set = sub.add_parser("set", help="Write a token")
    p_set.add_argument("--token", required=True, choices=("refresh", "machine"))
    p_set.add_argument("--value", required=True)
    p_set.add_argument("--saved-at", default=None)

    p_delete = sub.add_parser("delete", help="Delete a token")
    p_delete.add_argument("--token", required=True, choices=("refresh", "machine"))

    p_exists = sub.add_parser("exists", help="Check whether token exists")
    p_exists.add_argument("--token", required=True, choices=("refresh", "machine"))

    sub.add_parser("clear", help="Delete refresh + machine tokens")
    sub.add_parser("status", help="Return token status JSON")
    return parser


def _read(token_name: str) -> str:
    if token_name == "refresh":
        return read_refresh_token()
    return read_machine_auth_token()


def _write(token_name: str, value: str, saved_at: str | None) -> str:
    if token_name == "refresh":
        return write_refresh_token(value, saved_at_iso=saved_at)
    return write_machine_auth_token(value, saved_at_iso=saved_at)


def _delete(token_name: str) -> str:
    if token_name == "refresh":
        return write_refresh_token("")
    return write_machine_auth_token("")


def _exists(token_name: str) -> bool:
    if token_name == "refresh":
        return refresh_token_exists()
    return machine_auth_token_exists()


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "get":
            value = _read(args.token)
            if value:
                sys.stdout.write(value)
            return 0

        if args.command == "set":
            mode = _write(args.token, args.value, args.saved_at)
            sys.stdout.write(mode)
            return 0

        if args.command == "delete":
            mode = _delete(args.token)
            sys.stdout.write(mode)
            return 0

        if args.command == "exists":
            sys.stdout.write("1" if _exists(args.token) else "0")
            return 0

        if args.command == "clear":
            removed = clear_tokens()
            sys.stdout.write(json.dumps({"removed": removed}, separators=(",", ":")))
            return 0

        if args.command == "status":
            payload = {
                "storage_mode": token_storage_mode(),
                "refresh_token_present": refresh_token_exists(),
                "machine_auth_present": machine_auth_token_exists(),
                "refresh_token_age_days": get_refresh_token_age_days(),
            }
            sys.stdout.write(json.dumps(payload, separators=(",", ":")))
            return 0
    except Exception as exc:  # pragma: no cover - CLI guardrail
        sys.stderr.write(str(exc))
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
