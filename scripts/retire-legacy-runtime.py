#!/usr/bin/env python3
"""Inspect retired execution data. This command cannot delete records."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'control-plane/backend'), str(ROOT / 'worker')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='New private manifest file; existing paths are refused')
    args = parser.parse_args()
    url = os.environ.get('CONTROL_DATABASE_URL')
    if not url:
        parser.error('CONTROL_DATABASE_URL is required in the environment')
    from app.database import database
    from app.legacy_retirement import inspect_retirement
    from sqlalchemy import text

    engine = None
    try:
        engine, sessions = database(url)
        with sessions.begin() as db:
            if engine.dialect.name == 'postgresql':
                db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            elif engine.dialect.name == 'sqlite':
                db.execute(text('PRAGMA query_only=ON'))
            else:
                raise ValueError('Unsupported maintenance database')
            manifest = inspect_retirement(db)
        # O_EXCL also refuses a symlink, and permissions apply at file creation.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(manifest, output, sort_keys=True, indent=2)
            output.write('\n')
        print(json.dumps({'mode': 'inspect', 'workers': len(manifest['worker_ids']),
                          'events': len(manifest['event_ids']),
                          'conversations': len(manifest['conversation_ids'])}))
        return 0
    except Exception:
        # Connection exceptions can contain the database URL and its credentials.
        print('Inspection failed. Check database access, retirement eligibility '
              'and that the output path does not already exist.', file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    sys.exit(main())
