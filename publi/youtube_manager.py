"""Start and recover the independent YouTube upload worker."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

from .database import DEFAULT_DB, connect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STALE_AFTER_SECONDS = 30 * 60


def _pid_is_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def recover_failed_publication_workers(db_path=DEFAULT_DB, stale_after=STALE_AFTER_SECONDS):
    now = datetime.now(timezone.utc)
    with connect(db_path) as conn:
        for row in conn.execute(
            "SELECT id,worker_pid,heartbeat_at FROM youtube_publications WHERE status='publicando'"
        ).fetchall():
            try:
                stamp = datetime.fromisoformat(row["heartbeat_at"].replace(" ", "T") + "+00:00")
                stale = (now - stamp).total_seconds() > stale_after
            except (AttributeError, TypeError, ValueError):
                stale = True
            if _pid_is_alive(row["worker_pid"]) and not stale:
                continue
            # An interrupted request is not auto-retried: the operator decides, avoiding duplicates.
            conn.execute(
                """UPDATE youtube_publications SET status=CASE
                         WHEN vertical_youtube_id IS NOT NULL OR horizontal_youtube_id IS NOT NULL
                         THEN 'parcial' ELSE 'erro' END,
                       vertical_status=CASE WHEN vertical_status='enviando' THEN 'erro' ELSE vertical_status END,
                       horizontal_status=CASE WHEN horizontal_status='enviando' THEN 'erro' ELSE horizontal_status END,
                       thumbnail_status=CASE WHEN thumbnail_status='enviando' THEN 'erro' ELSE thumbnail_status END,
                       vertical_error=CASE WHEN vertical_status='enviando' THEN 'Worker interrompido; confirme no YouTube antes de tentar novamente.' ELSE vertical_error END,
                       horizontal_error=CASE WHEN horizontal_status='enviando' THEN 'Worker interrompido; confirme no YouTube antes de tentar novamente.' ELSE horizontal_error END,
                       thumbnail_error=CASE WHEN thumbnail_status='enviando' THEN 'Worker interrompido.' ELSE thumbnail_error END,
                       worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (row["id"],)
            )


def start_youtube_worker_if_needed(db_path=DEFAULT_DB):
    recover_failed_publication_workers(db_path)
    with connect(db_path) as conn:
        pending = conn.execute(
            "SELECT 1 FROM youtube_publications WHERE status='na_fila' LIMIT 1"
        ).fetchone()
    if not pending:
        return None
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "youtube-worker.log").open("ab")
    try:
        process = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "youtube_worker.py"), "--once",
             "--db", str(Path(db_path).resolve())],
            cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        log.close()
    return process.pid
