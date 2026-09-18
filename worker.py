"""One-shot render worker started automatically by the Streamlit interface."""
import argparse
import fcntl
from pathlib import Path
from dotenv import load_dotenv

from publi.database import init_db
from publi.worker import process_next


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process one job and exit")
    parser.add_argument("--db", default="publi.db")
    parser.add_argument("--output", default="output")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    init_db(db_path)
    lock_path = db_path.with_suffix(db_path.suffix + ".worker.lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        process_next(db_path, args.output, include_legacy=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
