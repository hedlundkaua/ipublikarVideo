"""One-shot worker for a queued pair of YouTube uploads."""
import argparse
import fcntl
from pathlib import Path

from dotenv import load_dotenv

from publi.database import init_db
from publi.youtube import process_next_publication


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default="publi.db")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    init_db(db_path)
    lock_path = db_path.with_suffix(db_path.suffix + ".youtube-worker.lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        process_next_publication(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
