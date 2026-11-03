"""Command line: one shot, or the schedule plus the admin dashboard."""

from __future__ import annotations

import argparse
import os
import sys

from immich_archive.config import load_config
from immich_archive.runner import run_backup
from immich_archive.schedule import serve


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    parser = argparse.ArgumentParser(prog="immich-archive")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Copy originals, the newest dump, and the manifest once")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hash and HEAD as needed. Do not upload or write the checkpoint",
    )
    sub.add_parser("serve", help="Run backups on the cron schedule and serve the admin dashboard")
    args = parser.parse_args(argv)
    config = load_config()
    if args.command == "run":
        return run_backup(config, dry_run=args.dry_run).exit_code
    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
