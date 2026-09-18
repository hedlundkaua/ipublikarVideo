import threading
import time
from pathlib import Path

from publi.database import connect, init_db


def _asset(db, root):
    vertical = root / "vertical.mp4"
    horizontal = root / "horizontal.mp4"
    vertical.write_bytes(b"vertical")
    horizontal.write_bytes(b"horizontal")
    with connect(db) as conn:
        niche = conn.execute("INSERT INTO niches(name,color,voice) VALUES('N','#123456','v')").lastrowid
        batch = conn.execute("INSERT INTO batches(niche_id,quantity,difficulty) VALUES(?,1,'media')", (niche,)).lastrowid
        video = conn.execute("INSERT INTO videos(batch_id,position,title) VALUES(?,1,'V')", (batch,)).lastrowid
        job = conn.execute(
            "INSERT INTO video_render_jobs(video_id,status,output_path) VALUES(?,'concluida',?)",
            (video, str(vertical)),
        ).lastrowid
        asset = conn.execute(
            "INSERT INTO live_assets(video_id,render_job_id,vertical_master_path) VALUES(?,?,?)",
            (video, job, str(vertical)),
        ).lastrowid
    return asset, vertical, horizontal


def test_tts_batch_limits_concurrency_and_promotes_atomically(tmp_path, monkeypatch):
    import publi.worker as worker

    active = maximum = 0
    lock = threading.Lock()

    def speech(text, voice, path):
        nonlocal active, maximum
        assert ".tmp.mp3" in str(path)
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        Path(path).write_bytes(text.encode())
        with lock:
            active -= 1
        return 1.0

    monkeypatch.setenv("PUBLI_TTS_CONCURRENCY", "3")
    monkeypatch.setattr(worker, "_speech", speech)
    specs = [(str(i), f"fala {i}", tmp_path / f"{i}.mp3", f"Etapa {i}") for i in range(9)]
    results = worker._generate_speeches(specs, "voice")
    assert maximum == 3
    assert results == {str(i): 1.0 for i in range(9)}
    assert all((tmp_path / f"{i}.mp3").read_bytes() == f"fala {i}".encode() for i in range(9))
    assert not list(tmp_path.glob("*.tmp.mp3"))


def test_horizontal_is_ready_before_proxy_phase(tmp_path, monkeypatch):
    import publi.horizontal_assets as assets

    db = tmp_path / "db.sqlite"
    init_db(db)
    asset_id, vertical, horizontal = _asset(db, tmp_path)
    observed = []

    monkeypatch.setattr(assets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(assets, "_build_horizontal", lambda *args: (vertical, horizontal, 12.0))

    def proxy(source, destination, vertical_orientation):
        with connect(db) as conn:
            row = conn.execute("SELECT horizontal_status,proxy_status FROM live_assets WHERE id=?", (asset_id,)).fetchone()
        observed.append(tuple(row))
        Path(destination).write_bytes(b"proxy")

    monkeypatch.setattr(assets, "_make_proxy", proxy)
    monkeypatch.setattr(assets, "validate_pair", lambda *args: 12.0)
    assert assets.prepare_asset(asset_id, db, tmp_path / "live") is True
    assert observed[0] == ("ready", "pending")
    with connect(db) as conn:
        row = conn.execute("SELECT status,horizontal_status,proxy_status FROM live_assets WHERE id=?", (asset_id,)).fetchone()
    assert tuple(row) == ("ready", "ready", "ready")


def test_proxy_failure_keeps_valid_horizontal_retryable(tmp_path, monkeypatch):
    import publi.horizontal_assets as assets

    db = tmp_path / "db.sqlite"
    init_db(db)
    asset_id, vertical, horizontal = _asset(db, tmp_path)
    monkeypatch.setattr(assets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(assets, "_build_horizontal", lambda *args: (vertical, horizontal, 12.0))
    monkeypatch.setattr(assets, "_make_proxy", lambda *args: (_ for _ in ()).throw(RuntimeError("proxy falhou")))
    assert assets.prepare_asset(asset_id, db, tmp_path / "live") is False
    with connect(db) as conn:
        row = conn.execute("SELECT status,horizontal_status,proxy_status,horizontal_master_path FROM live_assets WHERE id=?", (asset_id,)).fetchone()
    assert tuple(row) == ("error", "ready", "error", str(horizontal))
    assets.retry_phase(asset_id, "proxy", db)
    with connect(db) as conn:
        row = conn.execute("SELECT horizontal_status,proxy_status FROM live_assets WHERE id=?", (asset_id,)).fetchone()
    assert tuple(row) == ("ready", "pending")
