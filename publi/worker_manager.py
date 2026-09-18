"""Start and monitor short-lived render workers for the Streamlit interface."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

from .database import DEFAULT_DB, connect

STALE_AFTER_SECONDS = 15 * 60
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pid_is_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        if stat.split()[2] == "Z":
            return False
    except (OSError, ValueError, IndexError):
        return False
    return True


def recover_failed_workers(db_path=DEFAULT_DB, stale_after=STALE_AFTER_SECONDS):
    """Turn crashed or silent workers into retryable errors."""
    now = datetime.now(timezone.utc)
    recovered = []
    with connect(db_path) as conn:
        jobs = conn.execute(
            "SELECT id,worker_pid,heartbeat_at FROM video_render_jobs "
            "WHERE status='renderizando'"
        ).fetchall()
        for job in jobs:
            heartbeat = job["heartbeat_at"]
            try:
                timestamp = datetime.fromisoformat(heartbeat.replace(" ", "T") + "+00:00")
                stale = (now - timestamp).total_seconds() > stale_after
            except (AttributeError, TypeError, ValueError):
                stale = True
            dead = not _pid_is_alive(job["worker_pid"])
            if not (dead or stale):
                continue
            reason = "O worker encerrou inesperadamente." if dead else "O worker parou de responder."
            changed = conn.execute(
                """UPDATE video_render_jobs
                   SET status='erro',error=?,worker_pid=NULL,heartbeat_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='renderizando'""",
                (reason + " Use Renderizar novamente.", job["id"]),
            ).rowcount
            if changed:
                recovered.append(job["id"])
    return recovered


def start_worker_if_needed(db_path=DEFAULT_DB, output_dir="output"):
    """Spawn a fresh one-job worker when a video is waiting."""
    recover_failed_workers(db_path)
    with connect(db_path) as conn:
        active = conn.execute("SELECT 1 FROM video_render_jobs WHERE status='renderizando' LIMIT 1").fetchone()
        pending = conn.execute(
            """SELECT 1 FROM video_render_jobs
               WHERE status='na_fila'
                  OR (status='concluida' AND shorts_copy_status='na_fila')
               LIMIT 1"""
        ).fetchone()
    if not pending or active:
        return None

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "worker.log").open("ab")
    command = [
        sys.executable, str(PROJECT_ROOT / "worker.py"), "--once",
        "--db", str(Path(db_path).resolve()),
        "--output", str(Path(output_dir).resolve()),
    ]
    try:
        process = subprocess.Popen(
            command, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except Exception as exc:
        with connect(db_path) as conn:
            render = conn.execute(
                "SELECT id FROM video_render_jobs WHERE status='na_fila' ORDER BY id LIMIT 1"
            ).fetchone()
            if render:
                conn.execute(
                    """UPDATE video_render_jobs SET status='erro',error=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (f"Não foi possível iniciar o worker: {exc}", render["id"]),
                )
            else:
                conn.execute(
                    """UPDATE video_render_jobs
                       SET shorts_copy_status='erro',shorts_copy_error=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=(SELECT id FROM video_render_jobs
                         WHERE status='concluida' AND shorts_copy_status='na_fila'
                         ORDER BY id LIMIT 1)""",
                    (f"Não foi possível iniciar o worker: {exc}",),
                )
        raise
    finally:
        log.close()
    return process.pid
