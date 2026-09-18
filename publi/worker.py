import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

from .database import connect, DEFAULT_DB
from .alternatives import OPTION_COLUMNS, SUPPORTED_VIDEO_QUESTION_COUNTS, question_labels
from .artwork import make_outro, make_preview
from .shorts import generate_shorts_copy_for_job, process_next_shorts_copy
from .media import tts_concurrency, with_ffmpeg_threads

EXIT_MARGIN = 0.12
THINK_TIME = 5.0
AUDIO_RATE = 48_000

OUTRO_TEMPLATES = {
    "facil": (
        "Mandou bem em {subject}! Agora conta pro Publi: quantas você acertou?",
        "Você arrasou em {subject}! Diz nos comentários quantas acertou.",
        "Curtiu o quiz de {subject}? Conta pro Publi quantas você acertou!",
    ),
    "media": (
        "Seu placar em {subject} merece aparecer. Deixa nos comentários quantas acertou!",
        "Como foi seu desafio de {subject}? Conta nos comentários quantas você acertou.",
        "Quero ver seu resultado em {subject}! Comenta quantas respostas acertou.",
    ),
    "dificil": (
        "Sobreviveu ao desafio de {subject}? Prova nos comentários: quantas você acertou?",
        "Foi longe no desafio de {subject}? Deixa seu placar nos comentários!",
        "Só quem domina {subject} chegou até aqui. Comenta quantas você acertou!",
    ),
}


def _stamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def _probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _stream_duration(stream):
    value = stream.get("duration")
    return float(value) if value not in (None, "N/A") else None


def _validate_render(scenes, captions, final_video):
    question_count = next((
        count for count in SUPPORTED_VIDEO_QUESTION_COUNTS
        if len(scenes) in (count * 3, count * 3 + 1)
    ), 0)
    if not question_count:
        raise RuntimeError("Validação falhou: o vídeo deve conter quatro perguntas; vídeos antigos com cinco continuam compatíveis.")

    expected = [
        f"{kind}{number}"
        for number in range(1, question_count + 1)
        for kind in ("q", "t", "r")
    ] + ["outro"]
    actual = [Path(scene).stem.rsplit("_", 1)[-1] for scene in scenes]
    if actual != expected:
        last = question_count
        raise RuntimeError(
            f"Validação falhou: a sequência deve conter q1,t1,r1 … "
            f"q{last},t{last},r{last},outro."
        )
    expected_captions = question_count * 2 + 1
    if len(captions) != expected_captions:
        raise RuntimeError(
            f"Validação falhou: o vídeo deve conter {question_count * 2} falas "
            "do quiz e uma de encerramento."
        )

    durations = []
    for scene, label in zip(scenes, expected):
        probe = _probe_media(scene)
        streams = probe.get("streams", [])
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if not audio or not video:
            raise RuntimeError(f"Validação falhou: cena {label} sem áudio ou vídeo.")
        if int(audio.get("sample_rate", 0)) != AUDIO_RATE:
            raise RuntimeError(f"Validação falhou: áudio da cena {label} não está em 48 kHz.")
        duration = float(probe.get("format", {}).get("duration") or 0)
        durations.append(duration)
        if label.startswith("t") and abs(duration - THINK_TIME) > 0.08:
            raise RuntimeError(f"Validação falhou: cena {label} mede {duration:.3f} s, não {THINK_TIME:.2f} s.")

    final_probe = _probe_media(final_video)
    final_streams = final_probe.get("streams", [])
    audio = next((stream for stream in final_streams if stream.get("codec_type") == "audio"), None)
    video = next((stream for stream in final_streams if stream.get("codec_type") == "video"), None)
    if not audio or not video:
        raise RuntimeError("Validação falhou: arquivo final sem faixa de áudio ou vídeo.")
    audio_duration, video_duration = _stream_duration(audio), _stream_duration(video)
    if audio_duration is None or video_duration is None:
        raise RuntimeError("Validação falhou: duração das faixas finais indisponível.")
    if abs(audio_duration - video_duration) > 0.25:
        raise RuntimeError(
            f"Validação falhou: áudio e vídeo dessincronizados ({audio_duration:.3f} s; {video_duration:.3f} s)."
        )
    if int(audio.get("sample_rate", 0)) != AUDIO_RATE:
        raise RuntimeError("Validação falhou: áudio final não está em 48 kHz.")

    for index, (start, end, _text) in enumerate(captions, 1):
        if start < 0 or end <= start:
            raise RuntimeError(f"Validação falhou: legenda {index} possui intervalo inválido.")
        if index > 1 and abs(start - captions[index - 2][1]) > 0.20:
            raise RuntimeError(f"Validação falhou: legenda {index} não está alinhada à anterior.")

    cursor = 0.0
    for index in range(question_count):
        question_duration = durations[index * 3]
        think_duration = durations[index * 3 + 1]
        answer_start = captions[index * 2 + 1][0]
        earliest = cursor + question_duration + think_duration - 0.08
        if answer_start < earliest:
            raise RuntimeError(f"Validação falhou: legenda da resposta {index + 1} começa antes da pausa.")
        cursor += sum(durations[index * 3:index * 3 + 3])
    outro_start, outro_end, _ = captions[-1]
    if abs(outro_start - cursor) > 0.20 or abs(outro_end - (cursor + durations[-1])) > 0.20:
        raise RuntimeError("Validação falhou: legenda do encerramento não está alinhada à cena final.")

    final_duration = float(final_probe.get("format", {}).get("duration") or max(audio_duration, video_duration))
    if abs(final_duration - sum(durations)) > 0.35 or abs(final_duration - outro_end) > 0.35:
        raise RuntimeError("Validação falhou: duração final não corresponde às cenas e legendas.")
    return final_duration


def _write_srt(blocks, path):
    lines = []
    for number, (start, end, text) in enumerate(blocks, 1):
        lines.extend([str(number), f"{_stamp(start)} --> {_stamp(end)}", text, ""])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _speech(text, voice, path):
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice=voice).save(str(path)))
    return _duration(path)


async def _speech_batch(specs, voice, completed=None):
    """Generate required narration atomically with bounded concurrency."""
    semaphore = asyncio.Semaphore(tts_concurrency())

    async def generate(key, text, destination, label):
        temporary = destination.with_suffix(".tmp" + destination.suffix)
        temporary.unlink(missing_ok=True)
        try:
            async with semaphore:
                duration = await asyncio.to_thread(_speech, text, voice, temporary)
            os.replace(temporary, destination)
            if completed:
                completed(key)
            return key, duration
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"{label}: {_error_message(exc)}") from exc

    tasks = []
    for spec in specs:
        tasks.append(asyncio.create_task(generate(*spec)))
        # Let each request enter the client in stable source order; requests still overlap.
        await asyncio.sleep(0.001)
    results = await asyncio.gather(*tasks)
    return dict(results)


def _generate_speeches(specs, voice, completed=None):
    return asyncio.run(_speech_batch(specs, voice, completed))


def _question_text(question):
    options = [question[key] for key in OPTION_COLUMNS]
    labels = question_labels(question)
    dynamic = question["labels_dynamic"] if "labels_dynamic" in question.keys() else True
    noun = "Opção" if dynamic else "Alternativa"
    return question["question"] + ". " + ". ".join(f"{noun} {labels[i]}: {option}" for i, option in enumerate(options))


def _answer_text(question):
    options = [question[key] for key in OPTION_COLUMNS]
    label = question_labels(question)[question["correct_option"]]
    answer = options[question["correct_option"]]
    explanation = (question["explanation"] or "").strip() if "explanation" in question.keys() else ""
    suffix = f" {explanation}" if explanation else ""
    dynamic = question["labels_dynamic"] if "labels_dynamic" in question.keys() else True
    noun = "opção" if dynamic else "alternativa"
    return f"A resposta correta é a {noun} {label}: {answer}.{suffix}"


def _outro_text(theme, niche, difficulty, video_id, video_position):
    """Return a stable contextual CTA, varying its template between videos."""
    subject = " ".join((theme or niche or "este tema").split())
    if len(subject) > 48:
        subject = subject[:45].rstrip(" ,.;:-") + "…"
    normalized = unicodedata.normalize("NFKD", difficulty or "")
    level = "".join(char for char in normalized if not unicodedata.combining(char)).lower()
    templates = OUTRO_TEMPLATES.get(level, OUTRO_TEMPLATES["media"])
    key = f"{video_id}:{video_position}:{level}:{subject}".encode("utf-8")
    choice = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % len(templates)
    return templates[choice].format(subject=subject)


def _error_message(exc):
    detail = (getattr(exc, "stderr", None) or "").strip()
    if detail:
        return detail[-2000:]
    return str(exc) or exc.__class__.__name__


def _set_scene_status(db_path, job_id, statuses, progress, warning=None):
    with connect(db_path) as conn:
        conn.execute("UPDATE video_render_jobs SET scene_status=?,progress=?,error=?,heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='renderizando'", (json.dumps(statuses, ensure_ascii=False), progress, warning, job_id))


def _render_scene(preview, audio, scene, duration, ding=False, exit_margin=EXIT_MARGIN):
    command = ["ffmpeg", "-y", "-loop", "1", "-i", str(preview), "-i", str(audio)]
    if ding:
        command += ["-f", "lavfi", "-i", "sine=frequency=1100:duration=0.18", "-filter_complex", f"[1:a][2:a]amix=inputs=2:duration=longest,apad=pad_dur={exit_margin}[a]", "-map", "0:v:0", "-map", "[a]"]
    else:
        command += ["-af", f"apad=pad_dur={exit_margin}", "-map", "0:v:0", "-map", "1:a:0"]
    command += ["-t", f"{duration + exit_margin:.3f}", "-r", "24", "-vf", "scale=1080:1920", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-crf", "27", "-c:a", "aac", "-ar", str(AUDIO_RATE), "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(scene)]
    subprocess.run(with_ffmpeg_threads(command), check=True, capture_output=True, text=True)


def _render_think_scene(preview, scene, duration=THINK_TIME):
    """Render a fixed-length clock effect while the unrevealed card stays visible."""
    # Use the time within each second as the oscillator phase and envelope time.
    # The two spectra alternate once per second, producing five tick-tock pairs
    # in the standard ten-second pause. A steep exponential decay makes each
    # event a mechanical strike instead of a gated electronic beep.
    clock = (
        "aevalsrc="
        "0.22*exp(-38*mod(t\\,1))*lt(mod(t\\,1)\\,0.16)*"
        "(eq(mod(floor(t)\\,2)\\,0)*"
        "(sin(2*PI*1450*mod(t\\,1))+0.52*sin(2*PI*2900*mod(t\\,1))"
        "+0.20*sin(2*PI*4350*mod(t\\,1)))+"
        "eq(mod(floor(t)\\,2)\\,1)*"
        "(0.90*sin(2*PI*980*mod(t\\,1))+0.46*sin(2*PI*1960*mod(t\\,1))"
        "+0.16*sin(2*PI*2940*mod(t\\,1))))"
        f":s={AUDIO_RATE}:d={duration:.3f}"
    )
    command = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(preview),
        "-f", "lavfi", "-i", clock,
        "-t", f"{duration:.3f}", "-r", "24", "-vf", "scale=1080:1920",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-crf", "27", "-c:a", "aac", "-ar", str(AUDIO_RATE), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-shortest", str(scene),
    ]
    subprocess.run(with_ffmpeg_threads(command), check=True, capture_output=True, text=True)


def _process_legacy_next(db_path=DEFAULT_DB, output_dir="output"):
    """Execute one legacy question job with audio-driven duration."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        job = conn.execute("""SELECT r.*, q.question, q.option_a, q.option_b, q.option_c, q.option_d, q.labels_dynamic, n.color, n.voice, n.outfit_path FROM render_jobs r JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id JOIN niches n ON n.id=b.niche_id WHERE r.status='na_fila' ORDER BY r.id LIMIT 1""").fetchone()
        if not job:
            return False
        conn.execute("UPDATE render_jobs SET status='renderizando',progress=10,attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg/ffprobe não encontrado no sistema.")
        stem = f"video_{job['id']}"; output = Path(output_dir)
        preview, audio, video, subtitles = output / f"{stem}.png", output / f"{stem}.mp3", output / f"{stem}.mp4", output / f"{stem}.srt"
        make_preview(job["question"], [job["option_a"], job["option_b"], job["option_c"], job["option_d"]], job["color"], preview, job["outfit_path"], labels=question_labels(job))
        duration = _speech(_question_text(job), job["voice"], audio)
        _render_scene(preview, audio, video, duration)
        _write_srt([(0, duration + EXIT_MARGIN, _question_text(job))], subtitles)
        with connect(db_path) as conn:
            conn.execute("UPDATE render_jobs SET status='concluida',progress=100,output_path=?,srt_path=?,thumbnail_path=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(video), str(subtitles), str(preview), job["id"]))
            conn.execute("UPDATE questions SET status='concluida' WHERE id=?", (job["question_id"],))
        return True
    except Exception as exc:
        with connect(db_path) as conn:
            conn.execute("UPDATE render_jobs SET status='erro',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (_error_message(exc), job["id"]))
        return False


def _render_video_job(db_path, output_dir):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("""SELECT r.*,v.title,v.id video_id,v.position video_position,
                                     b.theme,b.difficulty,n.name niche,n.color,n.voice,n.outfit_path
                              FROM video_render_jobs r JOIN videos v ON v.id=r.video_id
                              JOIN batches b ON b.id=v.batch_id JOIN niches n ON n.id=b.niche_id
                              WHERE r.status='na_fila' ORDER BY r.id LIMIT 1""").fetchone()
        if not job: return None
        questions = conn.execute("SELECT q.* FROM video_questions vq JOIN questions q ON q.id=vq.question_id WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position", (job["video_id"],)).fetchall()
        if len(questions) not in SUPPORTED_VIDEO_QUESTION_COUNTS:
            conn.execute("UPDATE video_render_jobs SET status='erro',error='O vídeo precisa de quatro questões ativas. Vídeos antigos com cinco continuam compatíveis.',updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],)); return False
        conn.execute("UPDATE video_render_jobs SET status='renderizando',progress=3,attempts=attempts+1,scene_status='{}',worker_pid=?,heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='na_fila'", (os.getpid(), job["id"]))
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg/ffprobe não encontrado no sistema.")
        output, stem = Path(output_dir), f"video_{job['id']}_v{job['render_version']}"
        scenes, captions, statuses, cursor = [], [], {}, 0.0
        outro_copy = _outro_text(job["theme"], job["niche"], job["difficulty"], job["video_id"], job["video_position"])
        specs = []
        for index, question in enumerate(questions, 1):
            statuses[str(index)] = {"voz da pergunta": "renderizando", "tempo para responder": "aguardando", "revelação": "renderizando", "montagem": "aguardando", "concluído": False}
            specs.extend([
                (f"q{index}", _question_text(question), output / f"{stem}_q{index}.mp3", f"Questão {index}, voz da pergunta"),
                (f"r{index}", _answer_text(question), output / f"{stem}_r{index}.mp3", f"Questão {index}, revelação"),
            ])
        statuses["encerramento"] = {"encerramento": "renderizando", "montagem": "aguardando", "concluído": False}
        specs.append(("outro", outro_copy, output / f"{stem}_outro.mp3", "Encerramento"))
        completed_count = 0
        def speech_completed(key):
            nonlocal completed_count
            completed_count += 1
            if key == "outro":
                statuses["encerramento"]["encerramento"] = "concluído"
            else:
                field = "voz da pergunta" if key.startswith("q") else "revelação"
                statuses[key[1:]][field] = "concluído"
            _set_scene_status(db_path, job["id"], statuses, 4 + int(20 * completed_count / len(specs)))
        durations_by_key = _generate_speeches(specs, job["voice"], speech_completed)
        for index, question in enumerate(questions, 1):
            options = [question[k] for k in ("option_a", "option_b", "option_c", "option_d")]
            statuses.setdefault(str(index), {"voz da pergunta": "concluído", "tempo para responder": "aguardando", "revelação": "concluído", "montagem": "aguardando", "concluído": False})
            _set_scene_status(db_path, job["id"], statuses, 4 + (index - 1) * 17)
            question_png, answer_png = output / f"{stem}_q{index}.png", output / f"{stem}_r{index}.png"
            question_audio, answer_audio = output / f"{stem}_q{index}.mp3", output / f"{stem}_r{index}.mp3"
            question_scene = output / f"{stem}_q{index}.mp4"
            think_scene = output / f"{stem}_t{index}.mp4"
            answer_scene = output / f"{stem}_r{index}.mp4"
            try:
                make_preview(question["question"], options, job["color"], question_png, job["outfit_path"], labels=question_labels(question))
                make_preview(question["question"], options, job["color"], answer_png, job["outfit_path"], question["correct_option"], reveal=True, labels=question_labels(question))
            except Exception as exc:
                message = _error_message(exc)
                statuses[str(index)]["montagem"] = f"erro: {message}"
                _set_scene_status(db_path, job["id"], statuses, 4 + (index - 1) * 17, message)
                raise RuntimeError(f"Questão {index}, rasterização: {message}") from exc
            try:
                question_duration = durations_by_key[f"q{index}"]
                # Start the clock immediately after the final option narration.
                # The answer keeps a short tail to avoid clipping, but adding it
                # here would make the reflection interval longer than 5 seconds.
                _render_scene(question_png, question_audio, question_scene, question_duration, exit_margin=0.0)
            except Exception as exc:
                message = _error_message(exc)
                statuses[str(index)]["voz da pergunta"] = f"erro: {message}"
                _set_scene_status(db_path, job["id"], statuses, 4 + (index - 1) * 17, message)
                raise RuntimeError(f"Questão {index}, voz da pergunta: {message}") from exc
            statuses[str(index)]["voz da pergunta"] = "concluído"
            statuses[str(index)]["tempo para responder"] = "renderizando"
            _set_scene_status(db_path, job["id"], statuses, 9 + (index - 1) * 17)
            try:
                _render_think_scene(question_png, think_scene)
            except Exception as exc:
                message = _error_message(exc)
                statuses[str(index)]["tempo para responder"] = f"erro: {message}"
                _set_scene_status(db_path, job["id"], statuses, 9 + (index - 1) * 17, message)
                raise RuntimeError(f"Questão {index}, tempo para responder: {message}") from exc
            statuses[str(index)]["tempo para responder"] = "concluído"
            statuses[str(index)]["revelação"] = "renderizando"
            _set_scene_status(db_path, job["id"], statuses, 12 + (index - 1) * 17)
            try:
                answer_duration = durations_by_key[f"r{index}"]
                _render_scene(answer_png, answer_audio, answer_scene, answer_duration, ding=True)
            except Exception as exc:
                message = _error_message(exc)
                statuses[str(index)]["revelação"] = f"erro: {message}"
                _set_scene_status(db_path, job["id"], statuses, 12 + (index - 1) * 17, message)
                raise RuntimeError(f"Questão {index}, revelação: {message}") from exc
            question_end = cursor + question_duration + THINK_TIME
            answer_end = question_end + answer_duration + EXIT_MARGIN
            captions.extend([(cursor, question_end, _question_text(question)), (question_end, answer_end, _answer_text(question))])
            cursor = answer_end
            statuses[str(index)]["montagem"] = "renderizando"
            scenes.extend([question_scene, think_scene, answer_scene])
            statuses[str(index)]["revelação"] = "concluído"; statuses[str(index)]["montagem"] = "concluído"; statuses[str(index)]["concluído"] = True
            _set_scene_status(db_path, job["id"], statuses, 4 + index * 17)

        statuses["encerramento"] = {"encerramento": "renderizando", "montagem": "aguardando", "concluído": False}
        _set_scene_status(db_path, job["id"], statuses, 90)
        outro_png = output / f"{stem}_outro.png"
        outro_audio = output / f"{stem}_outro.mp3"
        outro_scene = output / f"{stem}_outro.mp4"
        outro_copy = _outro_text(
            job["theme"], job["niche"], job["difficulty"], job["video_id"], job["video_position"]
        )
        try:
            make_outro(outro_copy, job["color"], outro_png, job["outfit_path"])
            outro_duration = durations_by_key["outro"]
            _render_scene(outro_png, outro_audio, outro_scene, outro_duration)
        except Exception as exc:
            message = _error_message(exc)
            statuses["encerramento"]["encerramento"] = f"erro: {message}"
            _set_scene_status(db_path, job["id"], statuses, 90, message)
            raise RuntimeError(f"Encerramento: {message}") from exc
        outro_end = cursor + outro_duration + EXIT_MARGIN
        captions.append((cursor, outro_end, outro_copy))
        cursor = outro_end
        scenes.append(outro_scene)
        statuses["encerramento"]["encerramento"] = "concluído"
        statuses["encerramento"]["montagem"] = "renderizando"
        _set_scene_status(db_path, job["id"], statuses, 94)
        listing = output / f"{stem}_concat.txt"
        listing.write_text("".join(f"file '{scene.resolve()}'\n" for scene in scenes), encoding="utf-8")
        final_video = output / f"{stem}.mp4"
        temporary_video = output / f"{stem}.rendering.mp4"
        subtitles = output / f"{stem}.srt"
        _set_scene_status(db_path, job["id"], statuses, 96)
        subprocess.run(with_ffmpeg_threads(["ffmpeg", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-c:a", "aac", "-ar", str(AUDIO_RATE), "-avoid_negative_ts", "make_zero", str(temporary_video)]), check=True, capture_output=True, text=True)
        _write_srt(captions, subtitles)
        final_duration = _validate_render(scenes, captions, temporary_video)
        temporary_video.replace(final_video)
        statuses["encerramento"]["montagem"] = "concluído"
        statuses["encerramento"]["concluído"] = True
        with connect(db_path) as conn:
            changed = conn.execute("""UPDATE video_render_jobs
                SET status='concluida',progress=100,scene_status=?,output_path=?,srt_path=?,
                    thumbnail_path=?,duration_seconds=?,error=NULL,worker_pid=NULL,heartbeat_at=NULL,
                    shorts_copy_status=CASE
                        WHEN shorts_title IS NULL OR trim(shorts_title)=''
                          OR shorts_description IS NULL OR trim(shorts_description)=''
                        THEN 'na_fila' ELSE shorts_copy_status END,
                    shorts_copy_error=CASE
                        WHEN shorts_title IS NULL OR trim(shorts_title)=''
                          OR shorts_description IS NULL OR trim(shorts_description)=''
                        THEN NULL ELSE shorts_copy_error END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='renderizando' AND worker_pid=?""",
                (json.dumps(statuses, ensure_ascii=False), str(final_video), str(subtitles),
                 str(output / f"{stem}_q1.png"), final_duration, job["id"], os.getpid())).rowcount
            if changed:
                conn.execute("UPDATE questions SET status='concluida' WHERE id IN (SELECT question_id FROM video_questions WHERE video_id=? AND active=1)", (job["video_id"],))
            else:
                raise RuntimeError("A posse deste trabalho expirou durante a renderização.")
        # Publish the horizontal job before copy generation so both can advance.
        try:
            from .live_assets import discover_completed
            from .horizontal_manager import start_horizontal_worker_if_needed
            discover_completed(db_path)
            start_horizontal_worker_if_needed(db_path)
        except Exception:
            pass
        # Copy generation is isolated: failure never rolls a valid MP4 back.
        try:
            generate_shorts_copy_for_job(job["id"], db_path)
        except Exception:
            # Bookkeeping failures must not change a validated MP4's status.
            pass
        # The independent live service performs the expensive 16:9/proxy build.
        # Registering here makes a new render visible to its FIFO immediately.
        try:
            from .live_assets import discover_completed
            discover_completed(db_path)
        except Exception:
            pass
        return True
    except Exception as exc:
        with connect(db_path) as conn:
            conn.execute("UPDATE video_render_jobs SET status='erro',error=?,worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='renderizando' AND worker_pid=?", (_error_message(exc), job["id"], os.getpid()))
        return False


def process_next(db_path=DEFAULT_DB, output_dir="output", include_legacy=True):
    result = _render_video_job(db_path, output_dir)
    if result is None:
        copy_result = process_next_shorts_copy(db_path)
        if copy_result is not None:
            return copy_result
    if result is None and include_legacy:
        return _process_legacy_next(db_path, output_dir)
    return result
