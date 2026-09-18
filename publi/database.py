import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path("publi.db")


def init_db(path=DEFAULT_DB):
    with connect(path) as conn:
        conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS niches (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL,
          voice TEXT NOT NULL, outfit_path TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS batches (
          id INTEGER PRIMARY KEY, niche_id INTEGER NOT NULL REFERENCES niches(id),
          quantity INTEGER NOT NULL, difficulty TEXT NOT NULL, theme TEXT,
          status TEXT NOT NULL DEFAULT 'aguardando_revisao', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions (
          id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
          question TEXT NOT NULL, option_a TEXT NOT NULL, option_b TEXT NOT NULL, option_c TEXT NOT NULL, option_d TEXT NOT NULL DEFAULT '',
          correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3), explanation TEXT NOT NULL DEFAULT '',
          options_shuffled INTEGER NOT NULL DEFAULT 0 CHECK(options_shuffled IN (0, 1)),
          labels_dynamic INTEGER NOT NULL DEFAULT 1 CHECK(labels_dynamic IN (0, 1)),
          status TEXT NOT NULL DEFAULT 'aguardando_revisao', rejection_reason TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS render_jobs (
          id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id),
          status TEXT NOT NULL DEFAULT 'na_fila', progress INTEGER NOT NULL DEFAULT 0,
          output_path TEXT, srt_path TEXT, thumbnail_path TEXT, error TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- Legacy render_jobs are kept for question-by-question videos. New
        -- batches compose five questions into one video render job.
        CREATE TABLE IF NOT EXISTS videos (
          id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
          position INTEGER NOT NULL CHECK(position >= 1), title TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(batch_id, position)
        );
        CREATE TABLE IF NOT EXISTS video_questions (
          id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id),
          question_id INTEGER NOT NULL REFERENCES questions(id),
          position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 5),
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
          replaced_question_id INTEGER REFERENCES questions(id),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(video_id, question_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_video_questions_active_position
          ON video_questions(video_id, position) WHERE active = 1;
        CREATE TABLE IF NOT EXISTS video_render_jobs (
          id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL UNIQUE REFERENCES videos(id),
          status TEXT NOT NULL DEFAULT 'na_fila', progress INTEGER NOT NULL DEFAULT 0,
          output_path TEXT, srt_path TEXT, thumbnail_path TEXT, error TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_niches_name_nocase ON niches(name COLLATE NOCASE);
        DROP INDEX IF EXISTS idx_niches_color;
        CREATE UNIQUE INDEX idx_niches_color ON niches(color COLLATE NOCASE);
        """)
        _upgrade_questions(conn)
        _upgrade_question_explanation(conn)
        _upgrade_option_shuffle(conn)
        _upgrade_video_jobs(conn)
        _upgrade_shorts_copy(conn)
        _upgrade_live(conn)
        _upgrade_youtube_publications(conn)


def _upgrade_youtube_publications(conn):
    """Create the durable, independently retryable YouTube upload queue."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS youtube_publications (
        id INTEGER PRIMARY KEY,
        render_job_id INTEGER NOT NULL UNIQUE REFERENCES video_render_jobs(id) ON DELETE CASCADE,
        title TEXT NOT NULL, description TEXT NOT NULL, thumbnail_path TEXT NOT NULL,
        privacy_status TEXT NOT NULL DEFAULT 'unlisted', category_id TEXT NOT NULL DEFAULT '27',
        language TEXT NOT NULL DEFAULT 'pt-BR', made_for_kids INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'na_fila', vertical_status TEXT NOT NULL DEFAULT 'na_fila',
        horizontal_status TEXT NOT NULL DEFAULT 'na_fila', thumbnail_status TEXT NOT NULL DEFAULT 'aguardando_horizontal',
        vertical_progress INTEGER NOT NULL DEFAULT 0, horizontal_progress INTEGER NOT NULL DEFAULT 0,
        vertical_youtube_id TEXT, horizontal_youtube_id TEXT, vertical_url TEXT, horizontal_url TEXT,
        vertical_error TEXT, horizontal_error TEXT, thumbnail_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER, heartbeat_at TEXT,
        started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE INDEX IF NOT EXISTS idx_youtube_publications_queue ON youtube_publications(status, id);
    """)


def _upgrade_live(conn):
    """Create the durable state used by the independent live service."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS live_assets (
        id INTEGER PRIMARY KEY,
        video_id INTEGER NOT NULL UNIQUE REFERENCES videos(id) ON DELETE CASCADE,
        render_job_id INTEGER NOT NULL REFERENCES video_render_jobs(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        vertical_master_path TEXT,
        horizontal_master_path TEXT,
        vertical_proxy_path TEXT,
        horizontal_proxy_path TEXT,
        duration_seconds REAL,
        completed_at TEXT,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE INDEX IF NOT EXISTS idx_live_assets_ready ON live_assets(status, completed_at, id);
      CREATE TABLE IF NOT EXISTS live_sessions (
        id INTEGER PRIMARY KEY,
        service_date TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'planned',
        started_at TEXT,
        ended_at TEXT,
        current_asset_id INTEGER REFERENCES live_assets(id),
        vertical_pid INTEGER,
        horizontal_pid INTEGER,
        vertical_health TEXT NOT NULL DEFAULT 'stopped',
        horizontal_health TEXT NOT NULL DEFAULT 'stopped',
        error TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS live_playback (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
        asset_id INTEGER REFERENCES live_assets(id),
        sequence INTEGER NOT NULL,
        cycle INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'queued',
        scheduled_at TEXT,
        started_at TEXT,
        ended_at TEXT,
        error TEXT,
        UNIQUE(session_id, sequence)
      );
      CREATE INDEX IF NOT EXISTS idx_live_playback_queue ON live_playback(session_id, status, sequence);
      CREATE TABLE IF NOT EXISTS live_commands (
        id INTEGER PRIMARY KEY,
        command TEXT NOT NULL CHECK(command IN ('stop','restart','skip')),
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        handled_at TEXT,
        error TEXT
      );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_assets)")}
    additions = {
        "horizontal_status": "TEXT NOT NULL DEFAULT \x27pending\x27",
        "proxy_status": "TEXT NOT NULL DEFAULT \x27pending\x27",
        "horizontal_error": "TEXT", "proxy_error": "TEXT",
        "horizontal_completed_at": "TEXT", "proxy_completed_at": "TEXT",
        "worker_pid": "INTEGER", "heartbeat_at": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE live_assets ADD COLUMN {name} {definition}")
    conn.execute("UPDATE live_assets SET horizontal_status=CASE WHEN horizontal_master_path IS NOT NULL THEN 'ready' WHEN status='error' THEN 'error' ELSE 'pending' END, proxy_status=CASE WHEN vertical_proxy_path IS NOT NULL AND horizontal_proxy_path IS NOT NULL THEN 'ready' WHEN status='error' AND horizontal_master_path IS NOT NULL THEN 'error' ELSE 'pending' END, horizontal_error=CASE WHEN status='error' AND horizontal_master_path IS NULL THEN error ELSE horizontal_error END, proxy_error=CASE WHEN status='error' AND horizontal_master_path IS NOT NULL THEN error ELSE proxy_error END, horizontal_completed_at=CASE WHEN horizontal_master_path IS NOT NULL THEN COALESCE(horizontal_completed_at,completed_at,updated_at) ELSE NULL END, proxy_completed_at=CASE WHEN vertical_proxy_path IS NOT NULL AND horizontal_proxy_path IS NOT NULL THEN COALESCE(proxy_completed_at,completed_at,updated_at) ELSE NULL END WHERE horizontal_status='pending' AND proxy_status='pending'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_assets_horizontal_queue ON live_assets(horizontal_status,proxy_status,id)")


def _upgrade_question_explanation(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
    if "explanation" not in columns:
        conn.execute("ALTER TABLE questions ADD COLUMN explanation TEXT NOT NULL DEFAULT ''")


def _upgrade_option_shuffle(conn):
    """Shuffle only review-stage videos that have never entered rendering."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
    if "options_shuffled" not in columns:
        conn.execute(
            "ALTER TABLE questions ADD COLUMN options_shuffled INTEGER NOT NULL DEFAULT 0"
        )
    if "labels_dynamic" not in columns:
        # Existing rendered questions retain their approved A-D presentation.
        conn.execute(
            "ALTER TABLE questions ADD COLUMN labels_dynamic INTEGER NOT NULL DEFAULT 0"
        )

    from .alternatives import shuffle_question_options

    eligible = conn.execute(
        """SELECT vq.video_id,vq.question_id,vq.position
           FROM video_questions vq
           JOIN questions q ON q.id=vq.question_id
           WHERE vq.active=1 AND q.options_shuffled=0
             AND q.status IN ('aguardando_revisao','aprovada','rejeitada')
             AND NOT EXISTS (
                 SELECT 1 FROM video_render_jobs r WHERE r.video_id=vq.video_id
             )
           ORDER BY vq.video_id,vq.position"""
    ).fetchall()
    if eligible:
        conn.executemany(
            "UPDATE questions SET labels_dynamic=1 WHERE id=?",
            [(link["question_id"],) for link in eligible],
        )
    for link in eligible:
        shuffle_question_options(
            conn, link["video_id"], link["question_id"], link["position"]
        )

    # Old rendered, queued and legacy questions must remain byte-for-byte stable.
    conn.execute("UPDATE questions SET options_shuffled=1 WHERE options_shuffled=0")


def _upgrade_video_jobs(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(video_render_jobs)")}
    if "scene_status" not in columns:
        conn.execute("ALTER TABLE video_render_jobs ADD COLUMN scene_status TEXT")
    if "duration_seconds" not in columns:
        conn.execute("ALTER TABLE video_render_jobs ADD COLUMN duration_seconds REAL")
    if "render_version" not in columns:
        conn.execute("ALTER TABLE video_render_jobs ADD COLUMN render_version INTEGER NOT NULL DEFAULT 1")
    if "worker_pid" not in columns:
        conn.execute("ALTER TABLE video_render_jobs ADD COLUMN worker_pid INTEGER")
    if "heartbeat_at" not in columns:
        conn.execute("ALTER TABLE video_render_jobs ADD COLUMN heartbeat_at TEXT")


def _upgrade_shorts_copy(conn):
    """Add editable Shorts copy without rebuilding or losing existing jobs."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(video_render_jobs)")}
    additions = {
        "shorts_title": "TEXT",
        "shorts_description": "TEXT",
        "shorts_copy_status": "TEXT NOT NULL DEFAULT 'aguardando_video'",
        "shorts_copy_error": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE video_render_jobs ADD COLUMN {name} {definition}")
    conn.execute(
        """UPDATE video_render_jobs
           SET shorts_copy_status='na_fila'
           WHERE status='concluida'
             AND (shorts_title IS NULL OR trim(shorts_title)=''
                  OR shorts_description IS NULL OR trim(shorts_description)='')
             AND shorts_copy_status='aguardando_video'"""
    )


def _upgrade_questions(conn):
    """Add alternative D to existing installations without losing legacy rows."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
    if "option_d" in columns:
        return
    # SQLite cannot alter a CHECK constraint, so rebuild only this parent table.
    # Foreign-key enforcement is restored before the connection is returned.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
      CREATE TABLE questions_upgrade (
        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
        question TEXT NOT NULL, option_a TEXT NOT NULL, option_b TEXT NOT NULL,
        option_c TEXT NOT NULL, option_d TEXT NOT NULL DEFAULT '',
        correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3),
        status TEXT NOT NULL DEFAULT 'aguardando_revisao', rejection_reason TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      INSERT INTO questions_upgrade(id,batch_id,question,option_a,option_b,option_c,option_d,correct_option,status,rejection_reason,created_at)
        SELECT id,batch_id,question,option_a,option_b,option_c,'',correct_option,status,rejection_reason,created_at FROM questions;
      DROP TABLE questions;
      ALTER TABLE questions_upgrade RENAME TO questions;
    """)
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def connect(path=DEFAULT_DB):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def list_review_videos(path=DEFAULT_DB):
    """Return only videos that have not entered the render workflow yet."""
    with connect(path) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT v.*,b.id batch_id,b.difficulty,b.theme,n.name niche,n.color
               FROM videos v
               JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM video_render_jobs r WHERE r.video_id=v.id
               )
               ORDER BY v.id DESC"""
        ).fetchall()]


def list_video_jobs(path=DEFAULT_DB):
    """Return render jobs together with their matching dual-format live asset."""
    with connect(path) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT r.*,v.title,n.name niche,
                      a.id live_asset_id,a.status live_asset_status,
                      a.horizontal_status live_horizontal_status,
                      a.proxy_status live_proxy_status,
                      a.vertical_master_path live_vertical_path,
                      a.horizontal_master_path live_horizontal_path,
                      a.error live_asset_error
               FROM video_render_jobs r
               JOIN videos v ON v.id=r.video_id
               JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id
               LEFT JOIN live_assets a ON a.render_job_id=r.id
               ORDER BY r.id DESC"""
        ).fetchall()]


def requeue_video_job(job_id, path=DEFAULT_DB):
    """Reset a completed/failed video job without changing its source content."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT video_id,status FROM video_render_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise RuntimeError("Trabalho de renderização não encontrado.")
        if job["status"] == "renderizando":
            raise RuntimeError("A renderização ainda está em andamento.")
        conn.execute(
            """UPDATE video_render_jobs
               SET status='na_fila',progress=0,output_path=NULL,srt_path=NULL,
                   thumbnail_path=NULL,error=NULL,scene_status=NULL,
                   duration_seconds=NULL,render_version=render_version+1,
                   worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (job_id,),
        )
        conn.execute(
            """UPDATE questions SET status='na_fila'
               WHERE id IN (SELECT question_id FROM video_questions
                            WHERE video_id=? AND active=1)""",
            (job["video_id"],),
        )
        conn.execute("""UPDATE live_assets SET status="pending",horizontal_status="pending",proxy_status="pending",vertical_master_path=NULL,horizontal_master_path=NULL,vertical_proxy_path=NULL,horizontal_proxy_path=NULL,duration_seconds=NULL,completed_at=NULL,horizontal_completed_at=NULL,proxy_completed_at=NULL,error=NULL,horizontal_error=NULL,proxy_error=NULL,worker_pid=NULL,heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE render_job_id=?""", (job_id,))
    return True


def requeue_shorts_copy(job_id, path=DEFAULT_DB):
    """Queue only the Shorts copy, leaving the completed media untouched."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status,shorts_copy_status FROM video_render_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise RuntimeError("Trabalho de renderização não encontrado.")
        if job["status"] != "concluida":
            raise RuntimeError("A copy só pode ser gerada após a conclusão do vídeo.")
        if job["shorts_copy_status"] == "gerando":
            raise RuntimeError("A geração da copy ainda está em andamento.")
        conn.execute(
            """UPDATE video_render_jobs
               SET shorts_copy_status='na_fila',shorts_copy_error=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (job_id,),
        )
    return True


def save_shorts_copy(job_id, title, description, path=DEFAULT_DB):
    """Persist operator edits while retaining the generated-copy lifecycle."""
    title = str(title).strip()
    description = str(description).strip()
    if not title or len(title) > 100:
        raise ValueError("O título precisa ter entre 1 e 100 caracteres.")
    if not description:
        raise ValueError("A descrição não pode ficar vazia.")
    with connect(path) as conn:
        changed = conn.execute(
            """UPDATE video_render_jobs
               SET shorts_title=?,shorts_description=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (title, description, job_id),
        ).rowcount
    if not changed:
        raise RuntimeError("Trabalho de renderização não encontrado.")
    return True


def delete_video(video_id, db_path=DEFAULT_DB, output_dir="output"):
    """Delete one video, its unshared questions, render job and output artifacts."""
    job = None
    question_ids = set()
    batch_id = None
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        video = conn.execute("SELECT batch_id FROM videos WHERE id=?", (video_id,)).fetchone()
        if not video:
            return False
        batch_id = video["batch_id"]
        job = conn.execute("SELECT * FROM video_render_jobs WHERE video_id=?", (video_id,)).fetchone()
        if job and job["status"] == "renderizando":
            raise RuntimeError("Este vídeo está sendo renderizado. Aguarde a conclusão ou o erro antes de excluí-lo.")
        links = conn.execute(
            "SELECT question_id,replaced_question_id FROM video_questions WHERE video_id=?",
            (video_id,),
        ).fetchall()
        for link in links:
            question_ids.add(link["question_id"])
            if link["replaced_question_id"] is not None:
                question_ids.add(link["replaced_question_id"])
        if job:
            conn.execute("DELETE FROM youtube_publications WHERE render_job_id=?", (job["id"],))
        conn.execute("DELETE FROM video_render_jobs WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM video_questions WHERE video_id=?", (video_id,))
        for question_id in question_ids:
            conn.execute("DELETE FROM render_jobs WHERE question_id=?", (question_id,))
            conn.execute(
                "DELETE FROM questions WHERE id=? AND NOT EXISTS (SELECT 1 FROM video_questions WHERE question_id=?)",
                (question_id, question_id),
            )
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        conn.execute("DELETE FROM batches WHERE id=? AND NOT EXISTS (SELECT 1 FROM videos WHERE batch_id=?)", (batch_id, batch_id))

    output_root = Path(output_dir).resolve()
    candidates = []
    if job:
        stem = f"video_{job['id']}"
        candidates.extend(output_root.glob(f"{stem}.*"))
        candidates.extend(output_root.glob(f"{stem}_*"))
        candidates.extend(
            Path(job[column])
            for column in ("output_path", "srt_path", "thumbnail_path")
            if job[column]
        )
    candidates.extend(output_root.glob(f"preview_question_{question_id}.png") for question_id in question_ids)
    flattened = []
    for candidate in candidates:
        if isinstance(candidate, Path):
            flattened.append(candidate)
        else:
            flattened.extend(candidate)
    for candidate in set(flattened):
        resolved = candidate.resolve()
        if resolved.is_relative_to(output_root) and resolved.is_file():
            resolved.unlink()
    return True
