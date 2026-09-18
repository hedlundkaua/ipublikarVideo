from datetime import datetime

from publi.database import connect, init_db
from publi.live_service import LiveService, ZONE, atomic_write, broadcast_window, enqueue_command


def _ready_asset(db_path, duration=60):
    with connect(db_path) as conn:
        niche = conn.execute("INSERT INTO niches(name,color,voice) VALUES('Teste','#123456','voz')").lastrowid
        batch = conn.execute("INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'média')", (niche,)).lastrowid
        video = conn.execute("INSERT INTO videos(batch_id,position,title) VALUES(?,1,'Vídeo')", (batch,)).lastrowid
        job = conn.execute("INSERT INTO video_render_jobs(video_id,status,output_path) VALUES(?,'concluida','vertical.mp4')", (video,)).lastrowid
        return conn.execute(
            """INSERT INTO live_assets(video_id,render_job_id,status,vertical_master_path,
               horizontal_master_path,vertical_proxy_path,horizontal_proxy_path,duration_seconds,completed_at)
               VALUES(?,?,'ready','vertical.mp4','horizontal.mp4','v720.mp4','h720.mp4',?,CURRENT_TIMESTAMP)""",
            (video, job, duration),
        ).lastrowid


def test_broadcast_window_uses_sao_paulo():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ZONE)
    start, end = broadcast_window(now)
    assert (start.hour, end.hour) == (9, 17)
    assert (end - start).total_seconds() == 8 * 3600


def test_queue_rotates_and_does_not_cross_close(tmp_path):
    db = tmp_path / "live.db"
    init_db(db)
    asset = _ready_asset(db, 50)
    service = LiveService(db, tmp_path / "live")
    now = datetime(2026, 9, 17, 16, 58, 30, tzinfo=ZONE)
    session = service._session(now)
    service._queue_ahead(session["id"], now)
    with connect(db) as conn:
        queued = conn.execute("SELECT * FROM live_playback ORDER BY sequence").fetchall()
    assert len(queued) == 1
    assert queued[0]["asset_id"] == asset
    assert queued[0]["cycle"] == 1


def test_queue_keeps_three_items_and_cycles_fifo(tmp_path):
    db = tmp_path / "live.db"
    init_db(db)
    asset = _ready_asset(db, 30)
    service = LiveService(db, tmp_path / "live")
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ZONE)
    session = service._session(now)
    service._queue_ahead(session["id"], now)
    with connect(db) as conn:
        queued = conn.execute("SELECT * FROM live_playback ORDER BY sequence").fetchall()
    assert len(queued) == 3
    assert {row["asset_id"] for row in queued} == {asset}
    assert [row["cycle"] for row in queued] == [1, 2, 3]


def test_commands_are_persistent(tmp_path):
    db = tmp_path / "live.db"
    init_db(db)
    enqueue_command("skip", db)
    with connect(db) as conn:
        row = conn.execute("SELECT command,status FROM live_commands").fetchone()
    assert tuple(row) == ("skip", "pending")


def test_atomic_write_replaces_contents(tmp_path):
    target = tmp_path / "event.m3u8"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text() == "second"
    assert not (tmp_path / "event.m3u8.tmp").exists()
