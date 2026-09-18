from pathlib import Path
from publi.questions import validate_questions, QuestionValidationError
from publi.artwork import make_preview
from publi.database import (
    init_db, connect, delete_video, list_review_videos, list_video_jobs,
)
from publi.worker import process_next


def test_question_validation_rejects_bad_shapes():
    good = [{"question":"Qual é a capital?", "options":["A","B","C","D"], "correct_option":1}]
    assert validate_questions(good)[0]["correct_option"] == 1
    for bad in ([{"question":"x","options":["a","a","b","c"],"correct_option":0}], [{"question":"x","options":["a","b","c","d"],"correct_option":5}]):
        try: validate_questions(bad); assert False
        except QuestionValidationError: pass


def test_preview_is_vertical_and_colored(tmp_path):
    target = tmp_path / "preview.png"
    image = make_preview("Pergunta de teste", ["Uma", "Duas", "Três"], "#ff0000", target)
    assert image.size == (1080, 1920) and target.exists()
    assert any(red > 100 and green < 40 and blue < 40 for red, green, blue in image.getdata())  # corpo do SVG vermelho
    background = (16, 20, 38)
    assert image.getpixel((60, 80)) == background  # o topo agora possui uma margem segura
    assert image.getpixel((540, 160)) != background  # enunciado deslocado para baixo
    assert image.getpixel((80, 580)) == background  # alternativas afastadas das laterais
    assert image.getpixel((540, 580)) != background


def test_status_transitions_and_failure_does_not_block_queue(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"; init_db(db)
    with connect(db) as c:
        n = c.execute("INSERT INTO niches(name,color,voice) VALUES('Teste','#123456','voz')").lastrowid
        b = c.execute("INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'fácil')", (n,)).lastrowid
        q = c.execute("INSERT INTO questions(batch_id,question,option_a,option_b,option_c,option_d,correct_option,status) VALUES(?,?,?,?,?,?,?,'na_fila')", (b,'P?','A','B','C','D',0)).lastrowid
        c.execute("INSERT INTO render_jobs(question_id) VALUES(?)", (q,))
    monkeypatch.setattr('publi.worker.shutil.which', lambda _: None)
    assert process_next(db, tmp_path) is False
    assert rows_status(db) == 'erro'
    with connect(db) as c: c.execute("UPDATE render_jobs SET status='na_fila' WHERE question_id=?", (q,))
    assert process_next(db, tmp_path) is False


def rows_status(db):
    with connect(db) as c: return c.execute("SELECT status FROM render_jobs").fetchone()[0]


def test_svg_rasterization_keeps_black_details_and_recolors_body(tmp_path):
    from PIL import Image
    from publi.artwork import rasterize_publi
    output = tmp_path / "publi.png"
    rasterize_publi("#22aa66", output, width=430)
    pixels = list(Image.open(output).convert("RGBA").getdata())
    assert any(red < 8 and green < 8 and blue < 8 and alpha > 0 for red, green, blue, alpha in pixels)
    assert any(green > 100 and red < 80 and alpha > 0 for red, green, blue, alpha in pixels)


def test_srt_uses_the_audio_intervals(tmp_path):
    from publi.worker import _write_srt
    output = tmp_path / "video.srt"
    _write_srt([(0, 1.234, "Pergunta"), (1.234, 3.579, "Resposta")], output)
    assert output.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,234\nPergunta\n\n"
        "2\n00:00:01,234 --> 00:00:03,579\nResposta\n"
    )


def test_render_scene_uses_measured_duration_and_shortest(monkeypatch, tmp_path):
    from publi.worker import AUDIO_RATE, _render_scene
    calls = []
    monkeypatch.setattr("publi.worker.subprocess.run", lambda command, **kwargs: calls.append(command))
    _render_scene(tmp_path / "frame.png", tmp_path / "voice.mp3", tmp_path / "scene.mp4", 2.345)
    command = calls[0]
    assert command[command.index("-t") + 1] == "2.465"
    assert "-shortest" in command
    assert command[command.index("-ar") + 1] == str(AUDIO_RATE)
    assert "6" not in command


def test_think_scene_is_exactly_five_seconds_and_generates_percussive_clock_audio(monkeypatch, tmp_path):
    from publi.worker import AUDIO_RATE, THINK_TIME, _render_think_scene

    calls = []
    monkeypatch.setattr("publi.worker.subprocess.run", lambda command, **kwargs: calls.append(command))
    _render_think_scene(tmp_path / "frame.png", tmp_path / "think.mp4")

    command = calls[0]
    assert command[command.index("-t") + 1] == "5.000"
    clock = command[command.index("lavfi") + 2]
    assert f"d={THINK_TIME:.3f}" in clock
    assert "aevalsrc=" in clock
    assert "exp(-38*mod(t\\,1))" in clock
    assert "lt(mod(t\\,1)\\,0.16)" in clock
    assert "eq(mod(floor(t)\\,2)\\,0)" in clock
    assert "eq(mod(floor(t)\\,2)\\,1)" in clock
    assert "1450" in clock and "2900" in clock and "4350" in clock
    assert "980" in clock and "1960" in clock and "2940" in clock
    assert "770+110" not in clock
    assert "lt(mod(t\\,1)\\,0.12)" not in clock
    assert "1100" not in " ".join(command)
    assert "-shortest" in command
    assert command[command.index("-ar") + 1] == str(AUDIO_RATE)


def test_five_question_video_uses_variable_audio_intervals(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from publi.worker import AUDIO_RATE, EXIT_MARGIN, THINK_TIME, _render_video_job, _stamp

    db = tmp_path / "db.sqlite"
    init_db(db)
    with connect(db) as conn:
        niche_id = conn.execute("INSERT INTO niches(name,color,voice) VALUES('Teste','#22aa66','voz')").lastrowid
        batch_id = conn.execute("INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'fácil')", (niche_id,)).lastrowid
        video_id = conn.execute("INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo 1')", (batch_id,)).lastrowid
        for position in range(1, 6):
            question_id = conn.execute(
                "INSERT INTO questions(batch_id,question,option_a,option_b,option_c,option_d,correct_option,explanation,status) VALUES(?,?,?,?,?,?,?,?, 'na_fila')",
                (batch_id, f"Pergunta {position}?", "A", "B", "C", "D", position % 4, f"Explicação {position}."),
            ).lastrowid
            conn.execute("INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,?)", (video_id, question_id, position))
        conn.execute("INSERT INTO video_render_jobs(video_id) VALUES(?)", (video_id,))

    measured = iter([1.0, .5, 1.7, .8, 2.1, .6, 1.2, .9, 2.4, .7, 1.1])
    render_calls, think_calls, ffmpeg_calls = [], [], []

    def fake_speech(text, voice, path):
        Path(path).write_bytes(b"audio")
        return next(measured)

    def fake_render(preview, audio, scene, duration, ding=False, exit_margin=EXIT_MARGIN):
        Path(scene).write_bytes(b"scene")
        render_calls.append((duration, ding, exit_margin))

    def fake_think(preview, scene, duration=THINK_TIME):
        Path(scene).write_bytes(b"scene")
        think_calls.append((Path(preview).name, Path(scene).name, duration))

    def fake_preview(question, options, color, output, *args, **kwargs):
        Path(output).write_bytes(b"preview")

    def fake_outro(message, color, output, outfit_path=None):
        Path(output).write_bytes(b"outro")

    def fake_run(command, **kwargs):
        ffmpeg_calls.append(command)
        Path(command[-1]).write_bytes(b"video")
        return SimpleNamespace(stdout="", returncode=0)

    def fake_probe(path):
        name = Path(path).stem
        if name.endswith("rendering"):
            duration = expected
        else:
            label = name.rsplit("_", 1)[-1]
            if label == "outro":
                duration = 1.1 + EXIT_MARGIN
            else:
                index = int(label[1:]) - 1
                if label.startswith("q"):
                    duration = durations[index * 2]
                elif label.startswith("t"):
                    duration = THINK_TIME
                else:
                    duration = durations[index * 2 + 1] + EXIT_MARGIN
        return {
            "format": {"duration": str(duration)},
            "streams": [
                {"codec_type": "video", "duration": str(duration)},
                {"codec_type": "audio", "duration": str(duration), "sample_rate": str(AUDIO_RATE)},
            ],
        }

    durations = [1.0, .5, 1.7, .8, 2.1, .6, 1.2, .9, 2.4, .7]
    expected = sum(durations) + 6 * EXIT_MARGIN + 5 * THINK_TIME + 1.1
    monkeypatch.setattr("publi.worker.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("publi.worker._speech", fake_speech)
    monkeypatch.setattr("publi.worker._render_scene", fake_render)
    monkeypatch.setattr("publi.worker._render_think_scene", fake_think)
    monkeypatch.setattr("publi.worker.make_preview", fake_preview)
    monkeypatch.setattr("publi.worker.make_outro", fake_outro)
    monkeypatch.setattr("publi.worker.subprocess.run", fake_run)
    monkeypatch.setattr("publi.worker._probe_media", fake_probe)
    monkeypatch.setattr("publi.worker._duration", lambda path: expected)

    assert _render_video_job(db, tmp_path) is True
    assert [duration for duration, _, _ in render_calls] == durations + [1.1]
    assert [ding for _, ding, _ in render_calls] == [False, True] * 5 + [False]
    assert [margin for _, _, margin in render_calls] == [0.0, EXIT_MARGIN] * 5 + [EXIT_MARGIN]
    assert [duration for _, _, duration in think_calls] == [THINK_TIME] * 5
    assert sum(duration for _, _, duration in think_calls) == 25.0
    listing = (tmp_path / "video_1_v1_concat.txt").read_text(encoding="utf-8")
    assert [Path(line[6:-1]).name for line in listing.splitlines()] == [
        name for index in range(1, 6) for name in
        (f"video_1_v1_q{index}.mp4", f"video_1_v1_t{index}.mp4", f"video_1_v1_r{index}.mp4")
    ] + ["video_1_v1_outro.mp4"]
    assert "copy" not in ffmpeg_calls[0]
    assert "libx264" in ffmpeg_calls[0] and "+genpts" in ffmpeg_calls[0]
    srt = (tmp_path / "video_1_v1.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") == 11
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-ar") + 1] == str(AUDIO_RATE)
    first_reveal = _stamp(1.0 + THINK_TIME)
    assert f"00:00:00,000 --> {first_reveal}" in srt
    assert f"{first_reveal} --> {_stamp(1.0 + .5 + EXIT_MARGIN + THINK_TIME)}" in srt
    assert f"--> {_stamp(expected)}" in srt
    caption_lines = [line for line in srt.splitlines() if " --> " in line]
    cursor = 0.0
    for index in range(5):
        question_duration, answer_duration = durations[index * 2:index * 2 + 2]
        reveal = cursor + question_duration + THINK_TIME
        assert caption_lines[index * 2].endswith(_stamp(reveal))
        assert caption_lines[index * 2 + 1].startswith(_stamp(reveal))
        cursor = reveal + answer_duration + EXIT_MARGIN
    assert caption_lines[-1] == f"{_stamp(cursor)} --> {_stamp(expected)}"
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM video_render_jobs").fetchone()
        statuses = json.loads(job["scene_status"])
        assert job["status"] == "concluida"
        assert abs(job["duration_seconds"] - expected) < .001
        assert all(scene["concluído"] for scene in statuses.values())
        assert all(statuses[str(index)]["tempo para responder"] == "concluído" for index in range(1, 6))
        assert statuses["encerramento"]["encerramento"] == "concluído"
        assert statuses["encerramento"]["montagem"] == "concluído"


def test_provider_retries_503_then_succeeds(monkeypatch):
    from publi.questions import _post_with_retries

    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
        def raise_for_status(self):
            return None
        def json(self):
            return {"ok": True}

    responses = iter([Response(503), Response(503), Response(200)])
    calls, waits = [], []
    monkeypatch.setattr("publi.questions.requests.post", lambda *args, **kwargs: calls.append(1) or next(responses))
    monkeypatch.setattr("publi.questions.time.sleep", waits.append)
    assert _post_with_retries("https://example.test", {}, {}) == {"ok": True}
    assert len(calls) == 3
    assert waits == [1, 2]


def test_provider_reports_persistent_rate_limit(monkeypatch):
    import pytest
    from publi.questions import ProviderError, _post_with_retries

    class Response:
        status_code = 429
        headers = {"Retry-After": "0"}

    monkeypatch.setenv("PROVIDER_RETRY_ATTEMPTS", "3")
    monkeypatch.setattr("publi.questions.requests.post", lambda *args, **kwargs: Response())
    monkeypatch.setattr("publi.questions.time.sleep", lambda delay: None)
    with pytest.raises(ProviderError, match="após 3 tentativas"):
        _post_with_retries("https://example.test", {}, {})


def test_publi_is_filled_and_centered_at_the_bottom(tmp_path):
    image = make_preview("Pergunta", ["A", "B", "C", "D"], "#ff3322", tmp_path / "centered.png")
    body = [
        (x, y)
        for y in range(1100, 1750)
        for x in range(1080)
        if (lambda pixel: pixel[0] > 140 and pixel[1] < 90 and pixel[2] < 90)(image.getpixel((x, y)))
    ]
    assert len(body) > 25_000
    assert abs((min(x for x, _ in body) + max(x for x, _ in body)) / 2 - 540) < 12
    assert min(y for _, y in body) > 1100
    assert max(y for _, y in body) < 1650


def _create_approved_video(db):
    with connect(db) as conn:
        niche_id = conn.execute("INSERT INTO niches(name,color,voice) VALUES('Teste','#123456','voz')").lastrowid
        batch_id = conn.execute("INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'fácil')", (niche_id,)).lastrowid
        video_id = conn.execute("INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo 1')", (batch_id,)).lastrowid
        question_ids = []
        for position in range(1, 6):
            question_id = conn.execute(
                "INSERT INTO questions(batch_id,question,option_a,option_b,option_c,option_d,correct_option,status) VALUES(?,?,?,?,?,?,?,'aprovada')",
                (batch_id, f"Pergunta {position}?", "A", "B", "C", "D", 0),
            ).lastrowid
            question_ids.append(question_id)
            conn.execute("INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,?)", (video_id, question_id, position))
    return video_id, batch_id, question_ids


def test_review_lists_unqueued_videos_and_removes_them_after_queueing(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, question_ids = _create_approved_video(db)

    with connect(db) as conn:
        conn.execute(
            "UPDATE questions SET status='aguardando_revisao' WHERE id=?",
            (question_ids[0],),
        )
    assert [video["id"] for video in list_review_videos(db)] == [video_id]

    with connect(db) as conn:
        conn.execute("UPDATE questions SET status='aprovada' WHERE id=?", (question_ids[0],))
    assert [video["id"] for video in list_review_videos(db)] == [video_id]

    with connect(db) as conn:
        job_id = conn.execute(
            "INSERT INTO video_render_jobs(video_id) VALUES(?)", (video_id,)
        ).lastrowid

    assert list_review_videos(db) == []
    jobs = list_video_jobs(db)
    assert jobs[0]["id"] == job_id
    assert jobs[0]["live_asset_status"] is None


def test_video_jobs_include_matching_horizontal_asset_state(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, _ = _create_approved_video(db)
    with connect(db) as conn:
        job_id = conn.execute(
            """INSERT INTO video_render_jobs(video_id,status,output_path)
               VALUES(?,'concluida','vertical.mp4')""",
            (video_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO live_assets(
                   video_id,render_job_id,status,vertical_master_path,
                   horizontal_master_path,error)
               VALUES(?,?,'error','vertical.mp4',NULL,'falha horizontal')""",
            (video_id, job_id),
        )

    job = list_video_jobs(db)[0]
    assert job["output_path"] == "vertical.mp4"
    assert job["live_asset_status"] == "error"
    assert job["live_horizontal_path"] is None
    assert job["live_asset_error"] == "falha horizontal"


def test_delete_completed_video_removes_database_rows_and_artifacts(tmp_path):
    db = tmp_path / "db.sqlite"
    output = tmp_path / "output"
    output.mkdir()
    init_db(db)
    video_id, batch_id, question_ids = _create_approved_video(db)
    with connect(db) as conn:
        job_id = conn.execute(
            "INSERT INTO video_render_jobs(video_id,status,progress) VALUES(?,'concluida',100)",
            (video_id,),
        ).lastrowid
    artifacts = [
        output / f"video_{job_id}.mp4",
        output / f"video_{job_id}.srt",
        output / f"video_{job_id}_q1.png",
        output / f"preview_question_{question_ids[0]}.png",
    ]
    for artifact in artifacts:
        artifact.write_bytes(b"generated")
    with connect(db) as conn:
        conn.execute(
            "UPDATE video_render_jobs SET output_path=?,srt_path=?,thumbnail_path=? WHERE id=?",
            (str(artifacts[0]), str(artifacts[1]), str(artifacts[2]), job_id),
        )

    assert delete_video(video_id, db, output) is True
    assert not any(artifact.exists() for artifact in artifacts)
    with connect(db) as conn:
        assert conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM video_render_jobs WHERE video_id=?", (video_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM questions WHERE batch_id=?", (batch_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is None


def test_delete_video_is_blocked_while_rendering(tmp_path):
    import pytest

    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, _ = _create_approved_video(db)
    with connect(db) as conn:
        conn.execute("INSERT INTO video_render_jobs(video_id,status) VALUES(?,'renderizando')", (video_id,))

    with pytest.raises(RuntimeError, match="sendo renderizado"):
        delete_video(video_id, db, tmp_path / "output")
    with connect(db) as conn:
        assert conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone() is not None


def test_delete_video_abandoned_during_review(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, batch_id, _ = _create_approved_video(db)

    assert delete_video(video_id, db, tmp_path / "output") is True
    with connect(db) as conn:
        assert conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is None


def test_requeue_preserves_source_and_clears_published_render(tmp_path):
    from publi.database import requeue_video_job

    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, question_ids = _create_approved_video(db)
    with connect(db) as conn:
        job_id = conn.execute(
            """INSERT INTO video_render_jobs(
                   video_id,status,progress,output_path,srt_path,thumbnail_path,
                   error,scene_status,duration_seconds,render_version)
               VALUES(?,'concluida',100,'old.mp4','old.srt','old.png',
                      NULL,'{}',72.5,1)""",
            (video_id,),
        ).lastrowid
        before = [tuple(row) for row in conn.execute(
            """SELECT q.question,q.option_a,q.option_b,q.option_c,q.option_d,
                      q.correct_option,n.voice,n.color
               FROM video_questions vq JOIN questions q ON q.id=vq.question_id
               JOIN batches b ON b.id=q.batch_id JOIN niches n ON n.id=b.niche_id
               WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position""",
            (video_id,),
        )]

    assert requeue_video_job(job_id, db) is True
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM video_render_jobs WHERE id=?", (job_id,)).fetchone()
        after = [tuple(row) for row in conn.execute(
            """SELECT q.question,q.option_a,q.option_b,q.option_c,q.option_d,
                      q.correct_option,n.voice,n.color
               FROM video_questions vq JOIN questions q ON q.id=vq.question_id
               JOIN batches b ON b.id=q.batch_id JOIN niches n ON n.id=b.niche_id
               WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position""",
            (video_id,),
        )]
        statuses = [row[0] for row in conn.execute(
            "SELECT status FROM questions WHERE id IN (%s) ORDER BY id" %
            ",".join("?" * len(question_ids)), question_ids
        )]
    assert before == after
    assert job["status"] == "na_fila" and job["progress"] == 0
    assert job["render_version"] == 2
    assert all(job[key] is None for key in (
        "output_path", "srt_path", "thumbnail_path", "error",
        "scene_status", "duration_seconds", "worker_pid", "heartbeat_at",
    ))
    assert statuses == ["na_fila"] * 5


def test_automatic_worker_is_spawned_as_one_shot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import publi.worker_manager as manager

    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, _ = _create_approved_video(db)
    with connect(db) as conn:
        conn.execute("INSERT INTO video_render_jobs(video_id) VALUES(?)", (video_id,))
    calls = []
    monkeypatch.setattr(manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(manager, "recover_failed_workers", lambda path: [])
    monkeypatch.setattr(
        manager.subprocess, "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(pid=4321),
    )

    assert manager.start_worker_if_needed(db, tmp_path / "output") == 4321
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1].endswith("worker.py") and "--once" in command
    assert command[command.index("--db") + 1] == str(db.resolve())
    assert kwargs["start_new_session"] is True


def test_dead_worker_becomes_retryable_error(tmp_path):
    from publi.worker_manager import recover_failed_workers

    db = tmp_path / "db.sqlite"
    init_db(db)
    video_id, _, _ = _create_approved_video(db)
    with connect(db) as conn:
        job_id = conn.execute(
            """INSERT INTO video_render_jobs(
                   video_id,status,progress,worker_pid,heartbeat_at)
               VALUES(?,'renderizando',30,999999999,CURRENT_TIMESTAMP)""",
            (video_id,),
        ).lastrowid

    assert recover_failed_workers(db) == [job_id]
    with connect(db) as conn:
        job = conn.execute("SELECT status,error,worker_pid FROM video_render_jobs").fetchone()
    assert job["status"] == "erro" and "Renderizar novamente" in job["error"]
    assert job["worker_pid"] is None


def test_render_validation_rejects_bad_clock_and_desync(tmp_path, monkeypatch):
    import pytest
    from publi.worker import AUDIO_RATE, THINK_TIME, _validate_render

    scenes = [tmp_path / f"video_1_v1_{kind}{number}.mp4"
              for number in range(1, 6) for kind in ("q", "t", "r")]
    scenes.append(tmp_path / "video_1_v1_outro.mp4")
    captions = []
    cursor = 0.0
    for _ in range(5):
        captions.extend([(cursor, cursor + 11, "pergunta"),
                         (cursor + 11, cursor + 12, "resposta")])
        cursor += 12
    captions.append((cursor, cursor + 1, "encerramento"))
    final = tmp_path / "video_1_v1.rendering.mp4"

    def probe(path):
        label = Path(path).stem.rsplit("_", 1)[-1]
        duration = THINK_TIME if label.startswith("t") else 1.0
        if label == "t3":
            duration = 9.5
        return {
            "format": {"duration": str(duration)},
            "streams": [
                {"codec_type": "video", "duration": str(duration)},
                {"codec_type": "audio", "duration": str(duration),
                 "sample_rate": str(AUDIO_RATE)},
            ],
        }

    monkeypatch.setattr("publi.worker._probe_media", probe)
    with pytest.raises(RuntimeError, match="t3 mede"):
        _validate_render(scenes, captions, final)


def test_render_validation_accepts_four_question_video(tmp_path, monkeypatch):
    from publi.worker import AUDIO_RATE, THINK_TIME, _validate_render

    question_count = 4
    scenes = [
        tmp_path / f"video_1_v1_{kind}{number}.mp4"
        for number in range(1, question_count + 1)
        for kind in ("q", "t", "r")
    ]
    scenes.append(tmp_path / "video_1_v1_outro.mp4")
    captions = []
    cursor = 0.0
    for _ in range(question_count):
        captions.extend([
            (cursor, cursor + 1.0 + THINK_TIME, "pergunta"),
            (cursor + 1.0 + THINK_TIME, cursor + 2.0 + THINK_TIME, "resposta"),
        ])
        cursor += 2.0 + THINK_TIME
    captions.append((cursor, cursor + 1.0, "encerramento"))
    final = tmp_path / "video_1_v1.rendering.mp4"
    final_duration = cursor + 1.0

    def probe(path):
        label = Path(path).stem.rsplit("_", 1)[-1]
        duration = final_duration if Path(path) == final else (THINK_TIME if label.startswith("t") else 1.0)
        return {
            "format": {"duration": str(duration)},
            "streams": [
                {"codec_type": "video", "duration": str(duration)},
                {"codec_type": "audio", "duration": str(duration), "sample_rate": str(AUDIO_RATE)},
            ],
        }

    monkeypatch.setattr("publi.worker._probe_media", probe)
    assert _validate_render(scenes, captions, final) == final_duration


def test_outro_is_vertical_colored_and_points_to_comments(tmp_path):
    from publi.artwork import make_outro

    target = tmp_path / "outro.png"
    image = make_outro("Conta pro Publi quantas você acertou!", "#ef3b2d", target)

    assert image.size == (1080, 1920)
    assert target.exists()
    assert image.getpixel((930, 1370))[0] > 180  # ponta colorida da seta à direita
    colored = 0
    for y in range(1050, 1710, 4):
        for x in range(80, 600, 4):
            red, green, blue = image.getpixel((x, y))
            colored += red > 150 and green < 100 and blue < 100
    assert colored > 1_000  # corpo colorido do Publi na região inferior segura


def test_outro_text_is_contextual_stable_varied_and_has_fallback():
    from publi.worker import _outro_text

    first = _outro_text("Sistema Solar", "Ciência", "fácil", 7, 2)
    assert first == _outro_text("Sistema Solar", "Ciência", "fácil", 7, 2)
    assert "Sistema Solar" in first and "Ciência" not in first
    assert "Ciência" in _outro_text(None, "Ciência", "média", 7, 2)
    variants = {_outro_text("Sistema Solar", "Ciência", "difícil", video_id, video_id)
                for video_id in range(1, 20)}
    assert len(variants) > 1
    long = _outro_text("Um assunto extremamente longo que não deve ultrapassar o espaço visual disponível", "Nicho", "fácil", 1, 1)
    assert "…" in long


def test_render_validation_rejects_missing_reordered_or_silent_outro(tmp_path, monkeypatch):
    import pytest
    from publi.worker import AUDIO_RATE, THINK_TIME, _validate_render

    scenes = [tmp_path / f"video_1_v1_{kind}{number}.mp4"
              for number in range(1, 6) for kind in ("q", "t", "r")]
    captions = []
    cursor = 0.0
    for _ in range(5):
        captions.extend([(cursor, cursor + 11, "pergunta"),
                         (cursor + 11, cursor + 12, "resposta")])
        cursor += 12
    captions.append((cursor, cursor + 1, "encerramento"))
    final = tmp_path / "video_1_v1.rendering.mp4"

    with pytest.raises(RuntimeError, match="r5,outro"):
        _validate_render(scenes, captions, final)
    scenes.append(tmp_path / "video_1_v1_outro.mp4")
    reordered = scenes[:-2] + [scenes[-1], scenes[-2]]
    with pytest.raises(RuntimeError, match="r5,outro"):
        _validate_render(reordered, captions, final)

    def probe(path):
        label = Path(path).stem.rsplit("_", 1)[-1]
        duration = 61.0 if label == "1.rendering" else (THINK_TIME if label.startswith("t") else 1.0)
        streams = [{"codec_type": "video", "duration": str(duration)}]
        if label != "outro":
            streams.append({"codec_type": "audio", "duration": str(duration),
                            "sample_rate": str(AUDIO_RATE)})
        return {"format": {"duration": str(duration)}, "streams": streams}

    monkeypatch.setattr("publi.worker._probe_media", probe)
    with pytest.raises(RuntimeError, match="cena outro sem áudio"):
        _validate_render(scenes, captions, final)
