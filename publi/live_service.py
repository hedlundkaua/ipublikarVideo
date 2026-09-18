"""Daily dual-output live controller (America/Sao_Paulo, 09:00–17:00)."""
import argparse
import fcntl
import os
import signal
import subprocess
import time
import math
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from dotenv import load_dotenv

from .database import DEFAULT_DB, connect, init_db
from .live_assets import discover_completed
from .media import with_ffmpeg_threads
from .horizontal_manager import start_horizontal_worker_if_needed


ZONE = ZoneInfo("America/Sao_Paulo")
OPEN_TIME = clock_time(9, 0)
CLOSE_TIME = clock_time(17, 0)
LOOKAHEAD = 3


def broadcast_window(now=None):
    now = now or datetime.now(ZONE)
    start = datetime.combine(now.date(), OPEN_TIME, ZONE)
    end = datetime.combine(now.date(), CLOSE_TIME, ZONE)
    return start, end


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def enqueue_command(command, db_path=DEFAULT_DB):
    if command not in {"stop", "restart", "skip"}:
        raise ValueError("Comando de live inválido.")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO live_commands(command) VALUES(?)", (command,))


class LiveService:
    def __init__(self, db_path=DEFAULT_DB, root="live"):
        self.db_path = Path(db_path)
        self.root = Path(root)
        self.hls = self.root / "hls"
        self.processes = {}
        self.manifest_generation = 0
        self.manifest_start_sequence = None
        self.running = True
        init_db(self.db_path)

    def _session(self, now):
        date = now.date().isoformat()
        with connect(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO live_sessions(service_date) VALUES(?)", (date,))
            return conn.execute("SELECT * FROM live_sessions WHERE service_date=?", (date,)).fetchone()

    def _queue_ahead(self, session_id, now):
        """Maintain three synchronized items, rotating least-played ready assets."""
        _, end = broadcast_window(now)
        with connect(self.db_path) as conn:
            queued = conn.execute(
                "SELECT * FROM live_playback WHERE session_id=? AND status IN ('queued','playing') ORDER BY sequence",
                (session_id,),
            ).fetchall()
            last_finish = float(queued[-1]["scheduled_at"]) if queued else 0
            cursor = max(now, datetime.fromtimestamp(last_finish, ZONE)) if last_finish else now
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0) n FROM live_playback WHERE session_id=?", (session_id,)).fetchone()["n"]
            while len(queued) < LOOKAHEAD and cursor < end:
                asset = conn.execute(
                    """SELECT a.*,COUNT(p.id) plays FROM live_assets a
                       LEFT JOIN live_playback p ON p.asset_id=a.id AND p.status IN ('played','playing','queued')
                       WHERE a.status='ready' GROUP BY a.id ORDER BY plays,a.completed_at,a.id LIMIT 1"""
                ).fetchone()
                if not asset:
                    break
                duration = float(asset["duration_seconds"])
                if cursor + timedelta(seconds=duration) > end:
                    break
                sequence += 1
                finish = cursor + timedelta(seconds=duration)
                conn.execute(
                    """INSERT INTO live_playback(session_id,asset_id,sequence,cycle,status,scheduled_at)
                       VALUES(?,?,?,?, 'queued',?)""",
                    (session_id, asset["id"], sequence, int(asset["plays"]) + 1, finish.timestamp()),
                )
                queued.append({"asset_id": asset["id"]})
                cursor = finish

    def _segment_asset(self, asset, orientation):
        source = Path(asset[f"{orientation}_proxy_path"])
        target = self.hls / f"asset_{asset['id']}_{orientation}"
        playlist = target / "index.m3u8"
        if playlist.exists() and playlist.stat().st_mtime >= source.stat().st_mtime:
            return playlist
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(with_ffmpeg_threads([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(target / "seg_%05d.ts"), str(playlist),
        ]), check=True, capture_output=True, text=True)
        return playlist

    def _filler_playlist(self, orientation):
        target = self.hls / f"filler_{orientation}"
        playlist = target / "index.m3u8"
        if playlist.exists():
            return playlist
        target.mkdir(parents=True, exist_ok=True)
        size = (720, 1280) if orientation == "vertical" else (1280, 720)
        image = Image.new("RGB", size, "#101426")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48 if orientation == "vertical" else 54)
        except OSError:
            font = ImageFont.load_default()
        message = "Voltamos amanhã às 9h"
        draw.multiline_text((size[0] // 2, size[1] // 2), message, font=font, fill="white", anchor="mm", align="center")
        still = target / "filler.png"
        image.save(still)
        subprocess.run(with_ffmpeg_threads([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "10", "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-b:v", "4000k",
            "-minrate", "4000k", "-maxrate", "4000k", "-bufsize", "8000k", "-g", "60",
            "-keyint_min", "60", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(target / "seg_%05d.ts"), str(playlist),
        ]), check=True, capture_output=True, text=True)
        return playlist

    def _event_manifest(self, session_id, orientation):
        with connect(self.db_path) as conn:
            if self.manifest_start_sequence is None:
                first = conn.execute(
                    """SELECT MIN(sequence) value FROM live_playback
                       WHERE session_id=? AND status IN ('queued','playing')""", (session_id,),
                ).fetchone()["value"]
                self.manifest_start_sequence = first
            entries = conn.execute(
                """SELECT p.sequence,p.scheduled_at,a.* FROM live_playback p JOIN live_assets a ON a.id=p.asset_id
                   WHERE p.session_id=? AND p.sequence>=? AND p.status NOT IN ('failed','skipped')
                   ORDER BY p.sequence""",
                (session_id, self.manifest_start_sequence or 0),
            ).fetchall()
            session = conn.execute("SELECT service_date FROM live_sessions WHERE id=?", (session_id,)).fetchone()
            minimum = conn.execute("SELECT MIN(duration_seconds) value FROM live_assets WHERE status='ready'").fetchone()["value"]
        body = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-TARGETDURATION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
        for entry in entries:
            child = self._segment_asset(entry, orientation)
            lines = child.read_text(encoding="utf-8").splitlines()
            body.append("#EXT-X-DISCONTINUITY")
            for index, line in enumerate(lines):
                if line.startswith("#EXTINF"):
                    body.append(line)
                    body.append(str((child.parent / lines[index + 1]).resolve()))
        if entries and minimum:
            last_finish = float(entries[-1]["scheduled_at"])
            close = datetime.combine(datetime.fromisoformat(session["service_date"]).date(), CLOSE_TIME, ZONE).timestamp()
            remaining = max(0.0, close - last_finish)
            if 0 < remaining < float(minimum):
                filler = self._filler_playlist(orientation)
                filler_lines = filler.read_text(encoding="utf-8").splitlines()
                segments = [(line, str((filler.parent / filler_lines[i + 1]).resolve()))
                            for i, line in enumerate(filler_lines) if line.startswith("#EXTINF")]
                count = math.ceil(remaining / 2.0)
                for index in range(count):
                    if index and index % len(segments) == 0:
                        body.append("#EXT-X-DISCONTINUITY")
                    body.extend(segments[index % len(segments)])
        manifest = self.hls / f"session_{session_id}_{self.manifest_generation}_{orientation}.m3u8"
        atomic_write(manifest, "\n".join(body) + "\n")
        return manifest

    def _destination(self, orientation):
        base = os.getenv("LIVE_RTMPS_URL", "").rstrip("/")
        key = os.getenv(f"LIVE_{orientation.upper()}_KEY", "")
        if not base or not key:
            raise RuntimeError("Defina LIVE_RTMPS_URL e as chaves LIVE_VERTICAL_KEY/LIVE_HORIZONTAL_KEY no arquivo de ambiente.")
        return f"{base}/{key}"

    def _start_publishers(self, session_id):
        for orientation in ("vertical", "horizontal"):
            current = self.processes.get(orientation)
            if current and current.poll() is None:
                continue
            manifest = self._event_manifest(session_id, orientation)
            destination = self._destination(orientation)
            process = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
                "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
                "-live_start_index", "0", "-i", str(manifest),
                "-c", "copy", "-f", "flv", destination,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.processes[orientation] = process
        with connect(self.db_path) as conn:
            conn.execute(
                """UPDATE live_sessions SET status='live',started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                   vertical_pid=?,horizontal_pid=?,vertical_health='healthy',horizontal_health='healthy',
                   error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (self.processes["vertical"].pid, self.processes["horizontal"].pid, session_id),
            )

    def _stop_publishers(self, session_id, status="finished", error=None):
        for process in self.processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in self.processes.values():
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes.clear()
        self.manifest_generation += 1
        self.manifest_start_sequence = None
        with connect(self.db_path) as conn:
            conn.execute(
                """UPDATE live_sessions SET status=?,ended_at=CURRENT_TIMESTAMP,vertical_pid=NULL,horizontal_pid=NULL,
                   vertical_health='stopped',horizontal_health='stopped',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, error, session_id),
            )

    def _sync_playback(self, session_id, now):
        with connect(self.db_path) as conn:
            current = conn.execute(
                "SELECT * FROM live_playback WHERE session_id=? AND status IN ('playing','queued') ORDER BY sequence LIMIT 1",
                (session_id,),
            ).fetchone()
            if not current:
                return
            if current["status"] == "queued":
                conn.execute("UPDATE live_playback SET status='playing',started_at=CURRENT_TIMESTAMP WHERE id=?", (current["id"],))
                conn.execute("UPDATE live_sessions SET current_asset_id=? WHERE id=?", (current["asset_id"], session_id))
            if now.timestamp() >= float(current["scheduled_at"]):
                conn.execute("UPDATE live_playback SET status='played',ended_at=CURRENT_TIMESTAMP WHERE id=?", (current["id"],))

    def _commands(self, session_id):
        with connect(self.db_path) as conn:
            commands = conn.execute("SELECT * FROM live_commands WHERE status='pending' ORDER BY id").fetchall()
        for command in commands:
            error = None
            try:
                if command["command"] == "skip":
                    with connect(self.db_path) as conn:
                        conn.execute("UPDATE live_playback SET status='skipped',ended_at=CURRENT_TIMESTAMP WHERE id=(SELECT id FROM live_playback WHERE session_id=? AND status='playing' ORDER BY sequence LIMIT 1)", (session_id,))
                    self._stop_publishers(session_id, "recovering")
                elif command["command"] == "restart":
                    self._stop_publishers(session_id, "recovering")
                else:
                    self._stop_publishers(session_id, "stopped")
            except Exception as exc:
                error = str(exc)
            with connect(self.db_path) as conn:
                conn.execute("UPDATE live_commands SET status=?,handled_at=CURRENT_TIMESTAMP,error=? WHERE id=?", ("error" if error else "done", error, command["id"]))

    def tick(self, now=None):
        now = now or datetime.now(ZONE)
        discover_completed(self.db_path)
        start_horizontal_worker_if_needed(self.db_path, self.root)
        start, end = broadcast_window(now)
        session = self._session(now)
        self._commands(session["id"])
        with connect(self.db_path) as conn:
            session = conn.execute("SELECT * FROM live_sessions WHERE id=?", (session["id"],)).fetchone()
        if session["status"] == "stopped":
            return
        if now < start or now >= end:
            if self.processes:
                self._stop_publishers(session["id"])
            return
        self._queue_ahead(session["id"], now)
        with connect(self.db_path) as conn:
            available = conn.execute("SELECT 1 FROM live_playback WHERE session_id=? AND status IN ('queued','playing')", (session["id"],)).fetchone()
        if not available:
            return
        self._event_manifest(session["id"], "vertical")
        self._event_manifest(session["id"], "horizontal")
        self._start_publishers(session["id"])
        self._sync_playback(session["id"], now)
        failed = [name for name, process in self.processes.items() if process.poll() is not None]
        if failed:
            with connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE live_playback SET status='failed',ended_at=CURRENT_TIMESTAMP,
                       error=? WHERE session_id=? AND status='playing'""",
                    (f"Publisher {failed[0]} encerrou.", session["id"]),
                )
            self._stop_publishers(session["id"], "recovering", f"Publisher {failed[0]} encerrou; reinício conjunto.")

    def run(self, interval=2):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = (self.root / "live-service.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("O serviço de live já está em execução.") from exc
        while self.running:
            self.tick()
            time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serviço de live dual do Publi")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--root", default="live")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--prepare-all", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    service = LiveService(args.db, args.root)
    if args.prepare_all:
        from .horizontal_assets import prepare_next
        while prepare_next(args.db, args.root) is not None:
            pass
    elif args.once:
        service.tick()
    else:
        service.run()


if __name__ == "__main__":
    main()
