from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.storage_import import import_legacy_assignment_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy local assignment files into configured object storage")
    parser.add_argument("--dry-run", action="store_true", help="inspect and count files without writing objects")
    args = parser.parse_args()

    with SessionLocal() as db:
        result = import_legacy_assignment_files(db, dry_run=args.dry_run)
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
