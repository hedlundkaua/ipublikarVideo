from publi.alternatives import (
    OPTION_COLUMNS, VIDEO_QUESTION_COUNT,
    balanced_correct_options,
    option_labels,
    shuffle_question_options,
)
from publi.database import connect, init_db
from publi.worker import _answer_text, _question_text


def _video_with_questions(db, status="aguardando_revisao"):
    with connect(db) as conn:
        niche_id = conn.execute(
            "INSERT INTO niches(name,color,voice) VALUES('Teste','#123456','voz')"
        ).lastrowid
        batch_id = conn.execute(
            "INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'fácil')",
            (niche_id,),
        ).lastrowid
        video_id = conn.execute(
            "INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo 1')",
            (batch_id,),
        ).lastrowid
        question_ids = []
        for position in range(1, 6):
            question_id = conn.execute(
                """INSERT INTO questions(
                       batch_id,question,option_a,option_b,option_c,option_d,
                       correct_option,status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (batch_id, f"Pergunta {position}", f"Certa {position}",
                 f"Errada {position}.1", f"Errada {position}.2",
                 f"Errada {position}.3", 0, status),
            ).lastrowid
            question_ids.append(question_id)
            conn.execute(
                "INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,?)",
                (video_id, question_id, position),
            )
    return video_id, question_ids


def _question_state(conn, question_id):
    row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    return tuple(row[column] for column in OPTION_COLUMNS), row["correct_option"], row["options_shuffled"]


def test_balanced_correct_positions_are_stable_complete_and_nonconsecutive():
    for video_id in range(1, 30):
        positions = balanced_correct_options(video_id)
        assert positions == balanced_correct_options(video_id)
        assert len(positions) == 5
        assert set(positions) == {0, 1, 2, 3}
        assert len(positions[:VIDEO_QUESTION_COUNT]) == VIDEO_QUESTION_COUNT
        assert set(positions[:VIDEO_QUESTION_COUNT]) == {0, 1, 2, 3}
        assert all(left != right for left, right in zip(positions, positions[1:]))


def test_shuffle_preserves_correct_answers_and_runs_only_once(tmp_path):
    db = tmp_path / "quiz.sqlite"
    init_db(db)
    video_id, question_ids = _video_with_questions(db)

    with connect(db) as conn:
        for position, question_id in enumerate(question_ids, 1):
            assert shuffle_question_options(conn, video_id, question_id, position)
        first = [_question_state(conn, question_id) for question_id in question_ids]
        for position, question_id in enumerate(question_ids, 1):
            assert not shuffle_question_options(conn, video_id, question_id, position)
        second = [_question_state(conn, question_id) for question_id in question_ids]

    assert first == second
    assert {state[1] for state in first} == {0, 1, 2, 3}
    assert all(first[index][1] != first[index + 1][1] for index in range(4))
    for position, (options, correct, shuffled) in enumerate(first, 1):
        assert options[correct] == f"Certa {position}"
        assert set(options) == {
            f"Certa {position}", f"Errada {position}.1",
            f"Errada {position}.2", f"Errada {position}.3",
        }
        assert shuffled == 1


def test_migration_shuffles_review_but_preserves_video_with_render_history(tmp_path):
    db = tmp_path / "quiz.sqlite"
    init_db(db)
    with connect(db) as conn:
        conn.execute("ALTER TABLE questions DROP COLUMN labels_dynamic")
    review_video, review_ids = _video_with_questions(db)
    with connect(db) as conn:
        niche_id = conn.execute(
            "INSERT INTO niches(name,color,voice) VALUES('Antigo','#654321','voz')"
        ).lastrowid
        batch_id = conn.execute(
            "INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'fácil')",
            (niche_id,),
        ).lastrowid
        old_video = conn.execute(
            "INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Antigo')",
            (batch_id,),
        ).lastrowid
        old_id = conn.execute(
            """INSERT INTO questions(
                   batch_id,question,option_a,option_b,option_c,option_d,
                   correct_option,status)
               VALUES(?,?,?,?,?,?,?,'concluida')""",
            (batch_id, "Antiga", "Certa", "B", "C", "D", 0),
        ).lastrowid
        conn.execute(
            "INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,1)",
            (old_video, old_id),
        )
        conn.execute(
            "INSERT INTO video_render_jobs(video_id,status) VALUES(?,'concluida')",
            (old_video,),
        )
        old_before = _question_state(conn, old_id)

    init_db(db)

    with connect(db) as conn:
        review = [_question_state(conn, question_id) for question_id in review_ids]
        old_after = _question_state(conn, old_id)
        review_labels = [conn.execute("SELECT labels_dynamic FROM questions WHERE id=?", (question_id,)).fetchone()[0] for question_id in review_ids]
        old_labels = conn.execute("SELECT labels_dynamic FROM questions WHERE id=?", (old_id,)).fetchone()[0]
    assert [state[1] for state in review] == list(balanced_correct_options(review_video))
    assert all(state[2] == 1 for state in review)
    assert old_after[:2] == old_before[:2]
    assert old_after[2] == 1
    assert review_labels == [1] * 5
    assert old_labels == 0


def test_dynamic_labels_cover_letters_numbers_text_and_mixed_content():
    assert option_labels(["A", "b", "Ç", "x"]) == ("1", "2", "3", "4")
    assert option_labels(["1", "2", "3", "4"]) == ("A", "B", "C", "D")
    assert option_labels(["Azul", "Verde", "Rosa", "Roxo"]) == ("A", "B", "C", "D")
    assert option_labels(["A", "2", "C", "texto"]) == ("A", "B", "C", "D")


def test_narration_answer_and_caption_text_share_dynamic_labels():
    letter_question = {
        "question": "Qual letra?", "option_a": "X", "option_b": "Y",
        "option_c": "Z", "option_d": "W", "correct_option": 0,
        "explanation": "",
    }
    assert "Opção 1: X" in _question_text(letter_question)
    assert _answer_text(letter_question) == "A resposta correta é a opção 1: X."

    numeric_question = dict(letter_question, option_a="10", option_b="20",
                            option_c="30", option_d="40", correct_option=1)
    assert "Opção B: 20" in _question_text(numeric_question)
    assert _answer_text(numeric_question) == "A resposta correta é a opção B: 20."

    legacy_question = dict(letter_question, labels_dynamic=0)
    assert "Alternativa A: X" in _question_text(legacy_question)
    assert "Opção" not in _question_text(legacy_question)
    assert _answer_text(legacy_question) == (
        "A resposta correta é a alternativa A: X."
    )


def test_artwork_uses_the_same_dynamic_and_legacy_labels(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    import publi.artwork as artwork
    captured = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, position, text, *args, **kwargs):
        captured.append(text)
        return original_text(draw, position, text, *args, **kwargs)

    def fake_mascot(color, output, width=430):
        Image.new("RGBA", (10, 10)).save(output)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(artwork, "rasterize_publi", fake_mascot)
    artwork.make_preview("Pergunta", ["A", "B", "C", "D"], "#123456", tmp_path / "dynamic.png")
    assert [text for text in captured if text.startswith(("1.", "2.", "3.", "4."))] == ["1. A", "2. B", "3. C", "4. D"]

    captured.clear()
    artwork.make_preview("Pergunta", ["A", "B", "C", "D"], "#123456", tmp_path / "legacy.png", labels=("A", "B", "C", "D"))
    assert [text for text in captured if text.startswith(("A.", "B.", "C.", "D."))] == ["A. A", "B. B", "C. C", "D. D"]
