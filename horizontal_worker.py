"""One-shot horizontal/proxy worker with a category-specific lock."""
import argparse
import fcntl
from pathlib import Path

from dotenv import load_dotenv

from publi.database import init_db
from publi.horizontal_assets import prepare_next


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default="publi.db")
    parser.add_argument("--live-dir", default="live")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    init_db(db_path)
    lock_path = db_path.with_suffix(db_path.suffix + ".horizontal-worker.lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        prepare_next(db_path, args.live_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
