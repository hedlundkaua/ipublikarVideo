"""Build and validate synchronized vertical/horizontal assets for the live service."""
import json
import shutil
import subprocess
from pathlib import Path

from .alternatives import OPTION_COLUMNS, question_labels
from .artwork import make_outro_horizontal, make_preview_horizontal
from .database import DEFAULT_DB, connect, init_db
from .media import with_ffmpeg_threads


FPS = 30
VIDEO_RATE = 4_000_000
AUDIO_RATE = 128_000
DURATION_TOLERANCE = 0.35


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def media_duration(path):
    return float(probe(path)["format"]["duration"])


def _rate(value):
    numerator, denominator = str(value or "0/1").split("/", 1)
    return float(numerator) / float(denominator)


def _validate_gop(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    timestamps = [float(line.strip().split(",")[0]) for line in result.stdout.splitlines() if line.strip()]
    if len(timestamps) > 1 and any(not 1.85 <= b - a <= 2.15 for a, b in zip(timestamps, timestamps[1:])):
        raise RuntimeError("Proxy não mantém GOP de 2 segundos.")


def _run(command):
    subprocess.run(with_ffmpeg_threads(command), check=True, capture_output=True, text=True)


def _render_still(image, audio_source, output, duration):
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-i", str(audio_source),
        "-t", f"{duration:.6f}", "-r", str(FPS), "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1920:1080", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-c:a", "aac", "-ar", "48000", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ])


def _make_proxy(source, output, vertical):
    scale = "scale=720:1280" if vertical else "scale=1280:720"
    temporary = output.with_suffix(".building.mp4")
    _run([
        "ffmpeg", "-y", "-i", str(source), "-vf", f"{scale},fps={FPS}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", "4000k", "-minrate", "4000k", "-maxrate", "4000k",
        "-bufsize", "8000k", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-x264-params", "nal-hrd=cbr:force-cfr=1", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(temporary),
    ])
    temporary.replace(output)


def validate_pair(vertical, horizontal, vertical_proxy=None, horizontal_proxy=None):
    """Validate dimensions, codecs, GOP-facing settings and synchronized duration."""
    v_probe, h_probe = probe(vertical), probe(horizontal)
    results = []
    for name, data, size in (("vertical", v_probe, (1080, 1920)), ("horizontal", h_probe, (1920, 1080))):
        video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
        audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
        if not video or not audio or (video.get("width"), video.get("height")) != size:
            raise RuntimeError(f"Master {name} inválido ou com resolução incorreta.")
        results.append(float(data["format"]["duration"]))
    if abs(results[0] - results[1]) > DURATION_TOLERANCE:
        raise RuntimeError("Masters vertical e horizontal têm durações diferentes.")

    for name, path, size in (
        ("vertical", vertical_proxy, (720, 1280)),
        ("horizontal", horizontal_proxy, (1280, 720)),
    ):
        if not path:
            continue
        data = probe(path)
        video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
        audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
        if not video or not audio:
            raise RuntimeError(f"Proxy {name} não contém áudio e vídeo.")
        if (video.get("width"), video.get("height")) != size or video.get("codec_name") != "h264":
            raise RuntimeError(f"Proxy {name} não é H.264 720p.")
        if video.get("pix_fmt") != "yuv420p" or audio.get("codec_name") != "aac":
            raise RuntimeError(f"Proxy {name} usa codecs incompatíveis.")
        if abs(_rate(video.get("avg_frame_rate")) - FPS) > 0.01:
            raise RuntimeError(f"Proxy {name} não está em 30 fps.")
        bit_rate = int(video.get("bit_rate") or 0)
        if not 3_800_000 <= bit_rate <= 4_200_000:
            raise RuntimeError(f"Proxy {name} não mantém CBR próximo de 4 Mbps.")
        if int(audio.get("sample_rate") or 0) != 48_000 or int(audio.get("channels") or 0) != 2:
            raise RuntimeError(f"Proxy {name} não usa AAC estéreo em 48 kHz.")
        if abs(float(data["format"]["duration"]) - results[0]) > DURATION_TOLERANCE:
            raise RuntimeError(f"Proxy {name} está fora de sincronismo.")
        _validate_gop(path)
    return results[0]


def discover_completed(db_path=DEFAULT_DB):
    """Add completed renders to the preparation queue in FIFO completion order."""
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE live_assets SET status='pending',horizontal_status='pending',proxy_status='pending',horizontal_master_path=NULL,
                   vertical_proxy_path=NULL,horizontal_proxy_path=NULL,duration_seconds=NULL,
                   completed_at=NULL,horizontal_completed_at=NULL,proxy_completed_at=NULL,horizontal_error=NULL,proxy_error=NULL,error=NULL,worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE EXISTS (SELECT 1 FROM video_render_jobs r
                 WHERE r.id=live_assets.render_job_id AND r.status='concluida'
                   AND r.output_path IS NOT live_assets.vertical_master_path)"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO live_assets(video_id,render_job_id,vertical_master_path)
               SELECT video_id,id,output_path FROM video_render_jobs
               WHERE status='concluida' AND output_path IS NOT NULL ORDER BY updated_at,id"""
        )
        conn.execute(
            """UPDATE live_assets SET vertical_master_path=(
                   SELECT output_path FROM video_render_jobs WHERE id=live_assets.render_job_id)
               WHERE status!='ready'"""
        )


def prepare_asset(asset_id, db_path=DEFAULT_DB, live_dir="live"):
    """Rebuild 16:9 from stored scene audio, then create the two live proxies."""
    root = Path(live_dir)
    root.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        asset = conn.execute(
            """SELECT a.*,r.render_version,r.output_path,v.position,b.theme,b.difficulty,
                      n.name niche,n.color,n.outfit_path
               FROM live_assets a JOIN video_render_jobs r ON r.id=a.render_job_id
               JOIN videos v ON v.id=a.video_id JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id WHERE a.id=?""", (asset_id,),
        ).fetchone()
        if not asset:
            raise RuntimeError("Ativo de live não encontrado.")
        questions = conn.execute(
            """SELECT q.* FROM video_questions vq JOIN questions q ON q.id=vq.question_id
               WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position""", (asset["video_id"],),
        ).fetchall()
        conn.execute("UPDATE live_assets SET status='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (asset_id,))
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg/ffprobe não encontrado.")
        vertical = Path(asset["output_path"])
        if not vertical.exists():
            raise RuntimeError(f"Master vertical não encontrado: {vertical}")
        stem = vertical.with_suffix("")
        scene_paths = []
        for index, question in enumerate(questions, 1):
            options = [question[column] for column in OPTION_COLUMNS]
            for kind, reveal in (("q", False), ("r", True)):
                source_scene = Path(f"{stem}_{kind}{index}.mp4")
                if not source_scene.exists():
                    raise RuntimeError(f"Cena original ausente: {source_scene}")
                png = root / f"asset_{asset_id}_{kind}{index}_horizontal.png"
                scene = root / f"asset_{asset_id}_{kind}{index}_horizontal.mp4"
                make_preview_horizontal(question["question"], options, asset["color"], png,
                                        asset["outfit_path"], question["correct_option"], reveal,
                                        question_labels(question))
                _render_still(png, source_scene, scene, media_duration(source_scene))
                if kind == "q":
                    scene_paths.append(scene)
                    think_source = Path(f"{stem}_t{index}.mp4")
                    if not think_source.exists():
                        raise RuntimeError(f"Cena original ausente: {think_source}")
                    think_scene = root / f"asset_{asset_id}_t{index}_horizontal.mp4"
                    _render_still(png, think_source, think_scene, media_duration(think_source))
                    scene_paths.append(think_scene)
                else:
                    scene_paths.append(scene)
        outro_source = Path(f"{stem}_outro.mp4")
        if not outro_source.exists():
            raise RuntimeError(f"Cena original ausente: {outro_source}")
        # The spoken copy is available in the final SRT's last block.
        srt_path = vertical.with_suffix(".srt")
        outro_copy = "Conta pra gente nos comentários quantas você acertou!"
        if srt_path.exists():
            blocks = srt_path.read_text(encoding="utf-8").strip().split("\n\n")
            if blocks and len(blocks[-1].splitlines()) >= 3:
                outro_copy = " ".join(blocks[-1].splitlines()[2:])
        outro_png = root / f"asset_{asset_id}_outro_horizontal.png"
        outro_scene = root / f"asset_{asset_id}_outro_horizontal.mp4"
        make_outro_horizontal(outro_copy, asset["color"], outro_png, asset["outfit_path"])
        _render_still(outro_png, outro_source, outro_scene, media_duration(outro_source))
        scene_paths.append(outro_scene)

        listing = root / f"asset_{asset_id}_horizontal.concat"
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in scene_paths), encoding="utf-8")
        horizontal = root / f"asset_{asset_id}_horizontal.mp4"
        temporary = horizontal.with_suffix(".building.mp4")
        _run(["ffmpeg", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-ar", "48000",
              "-pix_fmt", "yuv420p", "-avoid_negative_ts", "make_zero", str(temporary)])
        temporary.replace(horizontal)
        vertical_proxy = root / f"asset_{asset_id}_vertical_720.mp4"
        horizontal_proxy = root / f"asset_{asset_id}_horizontal_720.mp4"
        _make_proxy(vertical, vertical_proxy, True)
        _make_proxy(horizontal, horizontal_proxy, False)
        duration = validate_pair(vertical, horizontal, vertical_proxy, horizontal_proxy)
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE live_assets SET status='ready',vertical_master_path=?,horizontal_master_path=?,
                   vertical_proxy_path=?,horizontal_proxy_path=?,duration_seconds=?,completed_at=CURRENT_TIMESTAMP,
                   error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (str(vertical), str(horizontal), str(vertical_proxy), str(horizontal_proxy), duration, asset_id),
            )
        return True
    except Exception as exc:
        with connect(db_path) as conn:
            conn.execute("UPDATE live_assets SET status='error',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc)[-2000:], asset_id))
        return False


def prepare_next(db_path=DEFAULT_DB, live_dir="live"):
    discover_completed(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT id FROM live_assets WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    return None if not row else prepare_asset(row["id"], db_path, live_dir)
