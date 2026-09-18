"""Independent two-phase builder for horizontal masters and live proxies."""
import os
import shutil
from pathlib import Path

from .alternatives import OPTION_COLUMNS, question_labels
from .artwork import make_outro_horizontal, make_preview_horizontal
from .database import DEFAULT_DB, connect, init_db
from .live_assets import (
    _make_proxy, _render_still, _run, media_duration, validate_pair,
    discover_completed,
)
from .media import with_ffmpeg_threads


def _heartbeat(db_path, asset_id):
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE live_assets SET heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND worker_pid=?",
            (asset_id, os.getpid()),
        )


def _load(asset_id, db_path):
    with connect(db_path) as conn:
        asset = conn.execute(
            """SELECT a.*,r.render_version,r.output_path,v.position,b.theme,b.difficulty,
                      n.name niche,n.color,n.outfit_path
               FROM live_assets a JOIN video_render_jobs r ON r.id=a.render_job_id
               JOIN videos v ON v.id=a.video_id JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id WHERE a.id=?""", (asset_id,),
        ).fetchone()
        questions = conn.execute(
            """SELECT q.* FROM video_questions vq JOIN questions q ON q.id=vq.question_id
               WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position""",
            (asset["video_id"],),
        ).fetchall() if asset else []
    if not asset:
        raise RuntimeError("Ativo de live não encontrado.")
    return asset, questions


def _build_horizontal(asset, questions, root, db_path):
    vertical = Path(asset["output_path"])
    if not vertical.is_file():
        raise RuntimeError(f"Master vertical não encontrado: {vertical}")
    stem, scenes = vertical.with_suffix(""), []
    for index, question in enumerate(questions, 1):
        options = [question[column] for column in OPTION_COLUMNS]
        for kind, reveal in (("q", False), ("r", True)):
            source = Path(f"{stem}_{kind}{index}.mp4")
            if not source.is_file():
                raise RuntimeError(f"Cena original ausente: {source}")
            png = root / f"asset_{asset['id']}_{kind}{index}_horizontal.png"
            scene = root / f"asset_{asset['id']}_{kind}{index}_horizontal.mp4"
            make_preview_horizontal(
                question["question"], options, asset["color"], png,
                asset["outfit_path"], question["correct_option"], reveal,
                question_labels(question),
            )
            _render_still(png, source, scene, media_duration(source))
            scenes.append(scene)
            if kind == "q":
                think = Path(f"{stem}_t{index}.mp4")
                if not think.is_file():
                    raise RuntimeError(f"Cena original ausente: {think}")
                think_scene = root / f"asset_{asset['id']}_t{index}_horizontal.mp4"
                _render_still(png, think, think_scene, media_duration(think))
                scenes.append(think_scene)
            _heartbeat(db_path, asset["id"])
    outro_source = Path(f"{stem}_outro.mp4")
    if not outro_source.is_file():
        raise RuntimeError(f"Cena original ausente: {outro_source}")
    outro_copy = "Conta pra gente nos comentários quantas você acertou!"
    srt = vertical.with_suffix(".srt")
    if srt.exists():
        blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
        if blocks and len(blocks[-1].splitlines()) >= 3:
            outro_copy = " ".join(blocks[-1].splitlines()[2:])
    png = root / f"asset_{asset['id']}_outro_horizontal.png"
    scene = root / f"asset_{asset['id']}_outro_horizontal.mp4"
    make_outro_horizontal(outro_copy, asset["color"], png, asset["outfit_path"])
    _render_still(png, outro_source, scene, media_duration(outro_source))
    scenes.append(scene)
    listing = root / f"asset_{asset['id']}_horizontal.concat"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in scenes), encoding="utf-8")
    horizontal = root / f"asset_{asset['id']}_horizontal.mp4"
    temporary = horizontal.with_suffix(".building.mp4")
    _run(with_ffmpeg_threads([
        "ffmpeg", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0",
        "-i", str(listing), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-ar", "48000", "-pix_fmt", "yuv420p",
        "-avoid_negative_ts", "make_zero", str(temporary),
    ]))
    temporary.replace(horizontal)
    duration = validate_pair(vertical, horizontal)
    return vertical, horizontal, duration


def prepare_asset(asset_id, db_path=DEFAULT_DB, live_dir="live"):
    """Build only missing phases, publishing the 16:9 master before proxies."""
    root = Path(live_dir)
    root.mkdir(parents=True, exist_ok=True)
    asset, questions = _load(asset_id, db_path)
    phase = "horizontal" if asset["horizontal_status"] != "ready" else "proxy"
    with connect(db_path) as conn:
        conn.execute(
            f"""UPDATE live_assets SET {phase}_status='building',status='building',
                   {phase}_error=NULL,error=NULL,worker_pid=?,heartbeat_at=CURRENT_TIMESTAMP,
                   attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (os.getpid(), asset_id),
        )
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg/ffprobe não encontrado.")
        if phase == "horizontal":
            vertical, horizontal, duration = _build_horizontal(asset, questions, root, db_path)
            with connect(db_path) as conn:
                conn.execute(
                    """UPDATE live_assets SET horizontal_status='ready',proxy_status='pending',
                       status='building',vertical_master_path=?,horizontal_master_path=?,
                       duration_seconds=?,horizontal_completed_at=CURRENT_TIMESTAMP,
                       horizontal_error=NULL,error=NULL,heartbeat_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (str(vertical), str(horizontal), duration, asset_id),
                )
            asset, _ = _load(asset_id, db_path)
            phase = "proxy"
        vertical = Path(asset["vertical_master_path"] or asset["output_path"])
        horizontal = Path(asset["horizontal_master_path"])
        vertical_proxy = root / f"asset_{asset_id}_vertical_720.mp4"
        horizontal_proxy = root / f"asset_{asset_id}_horizontal_720.mp4"
        _make_proxy(vertical, vertical_proxy, True)
        _heartbeat(db_path, asset_id)
        _make_proxy(horizontal, horizontal_proxy, False)
        duration = validate_pair(vertical, horizontal, vertical_proxy, horizontal_proxy)
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE live_assets SET status='ready',proxy_status='ready',
                   vertical_proxy_path=?,horizontal_proxy_path=?,duration_seconds=?,
                   proxy_completed_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP,
                   proxy_error=NULL,error=NULL,worker_pid=NULL,heartbeat_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (str(vertical_proxy), str(horizontal_proxy), duration, asset_id),
            )
        return True
    except Exception as exc:
        message = str(exc)[-2000:]
        with connect(db_path) as conn:
            conn.execute(
                f"""UPDATE live_assets SET status='error',{phase}_status='error',
                   {phase}_error=?,error=?,worker_pid=NULL,heartbeat_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (message, message, asset_id),
            )
        return False


def prepare_next(db_path=DEFAULT_DB, live_dir="live"):
    init_db(db_path)
    discover_completed(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT a.id FROM live_assets a JOIN video_render_jobs r ON r.id=a.render_job_id
               WHERE r.status='concluida' AND (a.horizontal_status='pending'
                  OR (a.horizontal_status='ready' AND a.proxy_status='pending'))
               ORDER BY a.id LIMIT 1"""
        ).fetchone()
    return None if not row else prepare_asset(row["id"], db_path, live_dir)


def retry_phase(asset_id, phase, db_path=DEFAULT_DB):
    if phase not in {"horizontal", "proxy"}:
        raise ValueError("Fase inválida.")
    with connect(db_path) as conn:
        if phase == "horizontal":
            conn.execute("""UPDATE live_assets SET status='pending',horizontal_status='pending',
              proxy_status='pending',horizontal_master_path=NULL,vertical_proxy_path=NULL,
              horizontal_proxy_path=NULL,horizontal_error=NULL,proxy_error=NULL,error=NULL,
              completed_at=NULL,worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (asset_id,))
        else:
            conn.execute("""UPDATE live_assets SET status='building',proxy_status='pending',
              vertical_proxy_path=NULL,horizontal_proxy_path=NULL,proxy_error=NULL,error=NULL,
              completed_at=NULL,worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND horizontal_status='ready'""", (asset_id,))
