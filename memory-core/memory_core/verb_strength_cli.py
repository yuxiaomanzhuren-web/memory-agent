from __future__ import annotations
import argparse, json, os, sqlite3
from pathlib import Path
from .verb_strength import audit_db

def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only modal-word audit')
    parser.add_argument('--db', default=os.environ.get('MEMORY_DB'))
    parser.add_argument('--terms')
    args = parser.parse_args(argv)
    if not args.db: parser.error('--db or MEMORY_DB is required')
    db = Path(args.db).resolve().as_posix()
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True); conn.row_factory = sqlite3.Row
    try: print(json.dumps(audit_db(conn, args.terms), ensure_ascii=False))
    finally: conn.close()
    return 0

if __name__ == '__main__': raise SystemExit(main())
