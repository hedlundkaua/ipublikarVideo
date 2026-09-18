import pytest

from publi.database import (
    connect, init_db, requeue_shorts_copy, requeue_video_job, save_shorts_copy,
)
from publi.questions import ProviderError
from publi.shorts import (
    ShortsCopyValidationError, generate_shorts_copy_for_job,
    request_shorts_copy, validate_shorts_copy,
)


VALID_COPY = {
    "title": "Você consegue acertar este quiz de ciência?",
    "description": (
        "Teste seus conhecimentos e conte quantas você acertou!\n"
        "#quiz #ciencia #desafio #shorts #conhecimento"
    ),
}


def _completed_job(db):
    init_db(db)
    with connect(db) as conn:
        niche_id = conn.execute(
            "INSERT INTO niches(name,color,voice) VALUES('Ciência','#123456','voz')"
        ).lastrowid
        batch_id = conn.execute(
            "INSERT INTO batches(niche_id,quantity,difficulty,theme) VALUES(?,1,'média','Espaço')",
            (niche_id,),
        ).lastrowid
        video_id = conn.execute(
            "INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo 1')", (batch_id,)
        ).lastrowid
        for position in range(1, 6):
            question_id = conn.execute(
                """INSERT INTO questions(
                    batch_id,question,option_a,option_b,option_c,option_d,
                    correct_option,status) VALUES(?,?,?,?,?,?,0,'concluida')""",
                (batch_id, f"Pergunta {position}?", "A", "B", "C", "D"),
            ).lastrowid
            conn.execute(
                "INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,?)",
                (video_id, question_id, position),
            )
        job_id = conn.execute(
            """INSERT INTO video_render_jobs(video_id,status,progress,output_path,shorts_copy_status)
               VALUES(?,'concluida',100,'video.mp4','na_fila')""", (video_id,)
        ).lastrowid
    return job_id


def test_validate_shorts_copy_requires_short_title_and_final_hashtags():
    assert validate_shorts_copy(VALID_COPY) == VALID_COPY
    with pytest.raises(ShortsCopyValidationError, match="100"):
        validate_shorts_copy({**VALID_COPY, "title": "x" * 101})
    with pytest.raises(ShortsCopyValidationError, match="5 a 8"):
        validate_shorts_copy({**VALID_COPY, "description": "Participe!\n#quiz #shorts"})
    with pytest.raises(ShortsCopyValidationError, match="final"):
        validate_shorts_copy({**VALID_COPY, "description": "#quiz no começo\n#um #dois #tres #quatro #cinco"})


def test_copy_provider_prefers_google_and_falls_back_to_openrouter(monkeypatch):
    calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "google")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router")
    monkeypatch.setattr(
        "publi.shorts._google_copy",
        lambda prompt: calls.append(("google", prompt)) or (_ for _ in ()).throw(ProviderError("fora")),
    )
    monkeypatch.setattr(
        "publi.shorts._openrouter_copy",
        lambda prompt: calls.append(("openrouter", prompt)) or VALID_COPY,
    )
    result = request_shorts_copy("Ciência", "Espaço", "média", [f"P{i}" for i in range(4)])
    assert result == VALID_COPY
    assert [provider for provider, _ in calls] == ["google", "openrouter"]
    assert all(f"P{i}" in calls[0][1] for i in range(4))


def test_generation_persists_copy_and_operator_edits(monkeypatch, tmp_path):
    db = tmp_path / "db.sqlite"
    job_id = _completed_job(db)
    monkeypatch.setattr("publi.shorts.request_shorts_copy", lambda *args: VALID_COPY)
    assert generate_shorts_copy_for_job(job_id, db) is True
    save_shorts_copy(job_id, "Título editado", "Descrição editada", db)
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM video_render_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["shorts_copy_status"] == "concluida"
        assert job["shorts_title"] == "Título editado"
        assert job["shorts_description"] == "Descrição editada"


def test_copy_failure_keeps_mp4_completed_and_can_be_requeued(monkeypatch, tmp_path):
    db = tmp_path / "db.sqlite"
    job_id = _completed_job(db)
    monkeypatch.setattr(
        "publi.shorts.request_shorts_copy", lambda *args: (_ for _ in ()).throw(ProviderError("sem cota"))
    )
    assert generate_shorts_copy_for_job(job_id, db) is False
    with connect(db) as conn:
        failed = conn.execute("SELECT * FROM video_render_jobs WHERE id=?", (job_id,)).fetchone()
        assert failed["status"] == "concluida"
        assert failed["output_path"] == "video.mp4"
        assert failed["shorts_copy_status"] == "erro"
        assert "sem cota" in failed["shorts_copy_error"]
    assert requeue_shorts_copy(job_id, db)
    with connect(db) as conn:
        assert conn.execute(
            "SELECT shorts_copy_status FROM video_render_jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == "na_fila"


def test_rerender_preserves_approved_copy(tmp_path):
    db = tmp_path / "db.sqlite"
    job_id = _completed_job(db)
    save_shorts_copy(job_id, VALID_COPY["title"], VALID_COPY["description"], db)
    with connect(db) as conn:
        conn.execute(
            "UPDATE video_render_jobs SET shorts_copy_status='concluida' WHERE id=?", (job_id,)
        )
    assert requeue_video_job(job_id, db)
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM video_render_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["shorts_title"] == VALID_COPY["title"]
        assert job["shorts_description"] == VALID_COPY["description"]
        assert job["shorts_copy_status"] == "concluida"


def test_migration_queues_existing_completed_video_without_data_loss(tmp_path):
    db = tmp_path / "old.sqlite"
    job_id = _completed_job(db)
    with connect(db) as conn:
        for column in ("shorts_title", "shorts_description", "shorts_copy_status", "shorts_copy_error"):
            conn.execute(f"ALTER TABLE video_render_jobs DROP COLUMN {column}")
    init_db(db)
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM video_render_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "concluida"
        assert job["output_path"] == "video.mp4"
        assert job["shorts_copy_status"] == "na_fila"


def test_completed_video_layout_has_responsive_pair_and_three_actions():
    source = open("app.py", encoding="utf-8").read()
    assert 'vertical_col, horizontal_col = st.columns(2, gap="large")' in source
    assert "Vertical (9:16)" in source
    assert "Horizontal (16:9)" in source
    assert "@media (max-width: 800px)" in source
    assert "flex-direction: column" in source
    assert 'download_col, delete_col, rerender_col = st.columns(3)' in source
    assert '"Baixar MP4"' in source
    assert "thumbnail_path" not in source[source.index("with tab_videos:"):]
