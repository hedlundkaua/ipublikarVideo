from pathlib import Path

import pytest

from publi.database import connect, init_db
from publi.youtube import _update, process_next_publication, queue_publication


def _publication(db, root):
    vertical = root / "vertical.mp4"
    horizontal = root / "horizontal.mp4"
    thumbnail = root / "thumbnail.png"
    for target in (vertical, horizontal, thumbnail):
        target.write_bytes(b"data")
    with connect(db) as conn:
        niche = conn.execute(
            "INSERT INTO niches(name,color,voice) VALUES('Teste','#123456','voz')"
        ).lastrowid
        batch = conn.execute(
            "INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'média')", (niche,)
        ).lastrowid
        video = conn.execute(
            "INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo')", (batch,)
        ).lastrowid
        job = conn.execute(
            """INSERT INTO video_render_jobs(video_id,status,output_path,shorts_title,
                 shorts_description,shorts_copy_status)
               VALUES(?,'concluida',?,'Mesmo título','Mesma descrição','concluida')""",
            (video, str(vertical)),
        ).lastrowid
        conn.execute(
            """INSERT INTO live_assets(video_id,render_job_id,status,vertical_master_path,
                 horizontal_master_path) VALUES(?,?,'ready',?,?)""",
            (video, job, str(vertical), str(horizontal)),
        )
    return queue_publication(job, thumbnail, db)


@pytest.mark.parametrize(
    "failed_kind,error_text",
    [("vertical", "token expirado"), ("horizontal", "limite da API excedido")],
)
def test_one_video_failure_is_partial_and_keeps_the_other_id(
    tmp_path, monkeypatch, failed_kind, error_text
):
    db = tmp_path / "db.sqlite"
    init_db(db)
    publication_id = _publication(db, tmp_path)

    def upload(publication, kind, media_path, db_path, service_factory):
        if kind == failed_kind:
            _update(db_path, publication["id"], **{
                f"{kind}_status": "erro", f"{kind}_error": error_text,
            })
            return None
        video_id = f"{kind}-id"
        _update(db_path, publication["id"], **{
            f"{kind}_status": "concluido", f"{kind}_progress": 100,
            f"{kind}_youtube_id": video_id, f"{kind}_url": f"https://youtu.be/{video_id}",
        })
        return video_id

    def thumbnail(publication, video_id, db_path, service_factory):
        _update(db_path, publication["id"], thumbnail_status="concluida")
        return True

    monkeypatch.setattr("publi.youtube._upload_video", upload)
    monkeypatch.setattr("publi.youtube._set_thumbnail", thumbnail)
    assert process_next_publication(db, service_factory=lambda: None) == "parcial"
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
    assert row[f"{failed_kind}_status"] == "erro"
    assert error_text in row[f"{failed_kind}_error"]
    successful = "horizontal" if failed_kind == "vertical" else "vertical"
    assert row[f"{successful}_youtube_id"] == f"{successful}-id"
    if failed_kind == "horizontal":
        assert row["thumbnail_status"] == "aguardando_horizontal"


def test_thumbnail_failure_does_not_lose_either_video(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    init_db(db)
    publication_id = _publication(db, tmp_path)

    def upload(publication, kind, media_path, db_path, service_factory):
        video_id = f"{kind}-id"
        _update(db_path, publication["id"], **{
            f"{kind}_status": "concluido", f"{kind}_progress": 100,
            f"{kind}_youtube_id": video_id,
        })
        return video_id

    def thumbnail(publication, video_id, db_path, service_factory):
        assert video_id == "horizontal-id"
        _update(db_path, publication["id"], thumbnail_status="erro", thumbnail_error="quota")
        return False

    monkeypatch.setattr("publi.youtube._upload_video", upload)
    monkeypatch.setattr("publi.youtube._set_thumbnail", thumbnail)
    assert process_next_publication(db, service_factory=lambda: None) == "parcial"
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
    assert row["vertical_youtube_id"] == "vertical-id"
    assert row["horizontal_youtube_id"] == "horizontal-id"
    assert row["thumbnail_status"] == "erro"
