from pathlib import Path
import threading

from publi.artwork import make_youtube_thumbnail
from publi.database import connect, init_db
from publi.youtube import (
    list_publication_candidates, process_next_publication, queue_publication,
    retry_publication,
)


def _ready_pair(db, root, title="Título idêntico", description="Descrição idêntica"):
    vertical, horizontal = root / "vertical.mp4", root / "horizontal.mp4"
    vertical.write_bytes(b"vertical")
    horizontal.write_bytes(b"horizontal")
    with connect(db) as conn:
        niche = conn.execute(
            "INSERT INTO niches(name,color,voice) VALUES('Ciência','#22aa66','voz')"
        ).lastrowid
        batch = conn.execute(
            "INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'média')", (niche,)
        ).lastrowid
        video = conn.execute(
            "INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo')", (batch,)
        ).lastrowid
        job = conn.execute(
            """INSERT INTO video_render_jobs(
                 video_id,status,progress,output_path,shorts_title,shorts_description,shorts_copy_status)
               VALUES(?,'concluida',100,?,?,?,'concluida')""",
            (video, str(vertical), title, description),
        ).lastrowid
        conn.execute(
            """INSERT INTO live_assets(video_id,render_job_id,status,vertical_master_path,
                 horizontal_master_path,completed_at)
               VALUES(?,?,'ready',?,?,CURRENT_TIMESTAMP)""",
            (video, job, str(vertical), str(horizontal)),
        )
    return job, vertical, horizontal


def test_thumbnail_is_1280x720_and_uses_niche_colour(tmp_path):
    target = tmp_path / "thumb.png"
    image = make_youtube_thumbnail(
        "História da ciência e das invenções extraordinárias", "#22aa66", target
    )
    assert image.size == (1280, 720)
    assert target.is_file()
    assert image.getpixel((28, 100)) == (34, 170, 102)
    # This bright niche colour gets a dark contrasting background.
    assert image.getpixel((0, 0)) == (16, 20, 38)


def test_candidates_require_both_existing_files_and_copy(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    job, _, horizontal = _ready_pair(db, tmp_path)
    assert [row["render_job_id"] for row in list_publication_candidates(db)] == [job]
    horizontal.unlink()
    assert list_publication_candidates(db) == []


def test_confirmation_freezes_copy_and_uploads_pair_concurrently(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    init_db(db)
    job, vertical, horizontal = _ready_pair(db, tmp_path)
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"png")
    publication_id = queue_publication(job, thumbnail, db)
    with connect(db) as conn:
        conn.execute(
            "UPDATE video_render_jobs SET shorts_title='Editado depois',shorts_description='Outra' WHERE id=?",
            (job,),
        )

    barrier = threading.Barrier(2)
    uploads = []

    def fake_upload(publication, kind, media_path, db_path, service_factory):
        uploads.append((kind, Path(media_path), publication["title"], publication["description"]))
        barrier.wait(timeout=2)
        video_id = "short-id" if kind == "vertical" else "wide-id"
        from publi.youtube import _update
        _update(db_path, publication["id"], **{
            f"{kind}_status": "concluido", f"{kind}_progress": 100,
            f"{kind}_youtube_id": video_id, f"{kind}_url": f"https://youtu.be/{video_id}",
        })
        return video_id

    thumbnails = []

    def fake_thumbnail(publication, video_id, db_path, service_factory):
        thumbnails.append(video_id)
        from publi.youtube import _update
        _update(db_path, publication["id"], thumbnail_status="concluida")
        return True

    monkeypatch.setattr("publi.youtube._upload_video", fake_upload)
    monkeypatch.setattr("publi.youtube._set_thumbnail", fake_thumbnail)
    assert process_next_publication(db, service_factory=lambda: None) == "concluida"
    assert {item[:2] for item in uploads} == {("vertical", vertical), ("horizontal", horizontal)}
    assert {(item[2], item[3]) for item in uploads} == {("Título idêntico", "Descrição idêntica")}
    assert thumbnails == ["wide-id"]
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
        assert row["status"] == "concluida"
        assert row["title"] == "Título idêntico"
        assert row["vertical_youtube_id"] == "short-id"
        assert row["horizontal_youtube_id"] == "wide-id"


def test_retry_requeues_only_failed_part_and_preserves_ids(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db)
    job, _, _ = _ready_pair(db, tmp_path)
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"png")
    publication_id = queue_publication(job, thumbnail, db)
    with connect(db) as conn:
        conn.execute(
            """UPDATE youtube_publications SET status='parcial',vertical_status='concluido',
                 vertical_youtube_id='kept-id',vertical_url='https://youtu.be/kept-id',
                 horizontal_status='erro',horizontal_error='quota' WHERE id=?""", (publication_id,)
        )
    retry_publication(publication_id, db)
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
    assert row["status"] == "na_fila"
    assert row["vertical_status"] == "concluido"
    assert row["vertical_youtube_id"] == "kept-id"
    assert row["horizontal_status"] == "na_fila"
    assert row["thumbnail_status"] == "aguardando_horizontal"
