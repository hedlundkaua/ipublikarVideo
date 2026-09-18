"""Start and recover the independent horizontal asset worker."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

from .database import DEFAULT_DB, connect, init_db
from .live_assets import discover_completed

STALE_AFTER_SECONDS = 15 * 60
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return Path(f"/proc/{int(pid)}/stat").read_text().split()[2] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def recover_horizontal_workers(db_path=DEFAULT_DB, stale_after=STALE_AFTER_SECONDS):
    now, recovered = datetime.now(timezone.utc), []
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id,worker_pid,heartbeat_at,horizontal_status,proxy_status FROM live_assets WHERE horizontal_status='building' OR proxy_status='building'"
        ).fetchall()
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row["heartbeat_at"].replace(" ", "T") + "+00:00")
                stale = (now - stamp).total_seconds() > stale_after
            except (AttributeError, TypeError, ValueError):
                stale = True
            if _pid_alive(row["worker_pid"]) and not stale:
                continue
            phase = "horizontal" if row["horizontal_status"] == "building" else "proxy"
            reason = "O worker horizontal foi interrompido; tente novamente."
            conn.execute(
                f"""UPDATE live_assets SET status='error',{phase}_status='error',
                   {phase}_error=?,error=?,worker_pid=NULL,heartbeat_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""", (reason, reason, row["id"]),
            )
            recovered.append(row["id"])
    return recovered


def start_horizontal_worker_if_needed(db_path=DEFAULT_DB, live_dir="live"):
    init_db(db_path)
    discover_completed(db_path)
    recover_horizontal_workers(db_path)
    with connect(db_path) as conn:
        pending = conn.execute("""SELECT 1 FROM live_assets a JOIN video_render_jobs r ON r.id=a.render_job_id
          WHERE r.status='concluida' AND (a.horizontal_status='pending' OR (a.horizontal_status='ready' AND a.proxy_status='pending')) LIMIT 1""").fetchone()
        active = conn.execute("""SELECT 1 FROM live_assets
          WHERE horizontal_status='building' OR proxy_status='building' LIMIT 1""").fetchone()
    if not pending or active:
        return None
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "horizontal-worker.log").open("ab")
    try:
        process = subprocess.Popen([
            sys.executable, str(PROJECT_ROOT / "horizontal_worker.py"), "--once",
            "--db", str(Path(db_path).resolve()), "--live-dir", str(Path(live_dir).resolve()),
        ], cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log,
           stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log.close()
    return process.pid
