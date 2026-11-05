#!/usr/bin/env python3
"""Inspect or explicitly migrate credentials with control services offline."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "control-plane/backend"), str(ROOT / "worker")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "copy", "clear"), default="inspect", nargs="?")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--fingerprint")
    parser.add_argument("--verified-backup-and-offline", action="store_true")
    args = parser.parse_args()
    if args.mode != "inspect" and (
        not args.verified_backup_and_offline
        or args.expected_count is None
        or not args.fingerprint
    ):
        parser.error("Copy and clear require a verified backup, offline services and an inspection fingerprint")

    from app.credential_migration import inventory, migrate_credentials
    from app.database import database
    from app.settings import AppSettings

    try:
        settings = AppSettings.load()
        engine, sessions = database(settings.database_url)
        try:
            with sessions.begin() as db:
                if args.mode == "inspect":
                    result = inventory(db)
                else:
                    result = migrate_credentials(
                        db, settings, expected_count=args.expected_count,
                        clear=args.mode == "clear", fingerprint=args.fingerprint,
                    )
            print(json.dumps(result))
        finally:
            engine.dispose()
    except Exception:
        print("Credential maintenance failed. Check configuration, quiescence and inventory.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
