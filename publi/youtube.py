"""Durable YouTube publication queue and resumable upload worker."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

from .database import DEFAULT_DB, connect

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def credential_paths():
    """Resolve OAuth files without ever storing their contents in the database."""
    client_default = Path("secrets/youtube-client-secret.json")
    token_default = Path("secrets/youtube-token.json")
    client_value = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", str(client_default))
    token_value = os.getenv("YOUTUBE_TOKEN_FILE", str(token_default))
    # Never interpret or echo a pasted client_secret as a filesystem path.
    if client_value.startswith("GOCSPX-") or not client_value.lower().endswith(".json"):
        if client_default.is_file():
            client_value = str(client_default)
        else:
            raise RuntimeError(
                "YOUTUBE_CLIENT_SECRETS_FILE deve conter o caminho do JSON OAuth, não o client_secret."
            )
    if not token_value.lower().endswith(".json"):
        token_value = str(token_default)
    return Path(client_value), Path(token_value)


def build_youtube_service():
    """Build one service per thread; httplib2 clients are not thread-safe."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Dependências da API do YouTube não estão instaladas.") from exc
    _, token_file = credential_paths()
    if not token_file.is_file():
        raise RuntimeError(f"Token OAuth não encontrado: {token_file}. Execute youtube_auth.py.")
    credentials = Credentials.from_authorized_user_file(str(token_file), [YOUTUBE_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        token_file.chmod(0o600)
    if not credentials.valid:
        raise RuntimeError("Token OAuth inválido ou sem refresh token. Execute youtube_auth.py novamente.")
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def list_publication_candidates(path=DEFAULT_DB):
    """List only complete vertical/horizontal pairs with finished copy."""
    with connect(path) as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT r.id render_job_id,r.video_id,r.output_path vertical_path,
                      r.shorts_title title,r.shorts_description description,
                      a.horizontal_master_path horizontal_path,n.name niche,n.color,n.outfit_path,
                      p.id publication_id,p.status publication_status
               FROM video_render_jobs r
               JOIN live_assets a ON a.render_job_id=r.id AND (a.horizontal_status='ready' OR a.status='ready')
               JOIN videos v ON v.id=r.video_id JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id
               LEFT JOIN youtube_publications p ON p.render_job_id=r.id
               WHERE r.status='concluida' AND r.shorts_copy_status='concluida'
                 AND trim(coalesce(r.shorts_title,''))!=''
                 AND trim(coalesce(r.shorts_description,''))!=''
                 AND r.output_path IS NOT NULL AND a.horizontal_master_path IS NOT NULL
               ORDER BY r.id DESC"""
        ).fetchall()]
    return [row for row in rows if Path(row["vertical_path"]).is_file()
            and Path(row["horizontal_path"]).is_file()]


def list_publications(path=DEFAULT_DB):
    with connect(path) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT p.*,n.name niche,r.output_path vertical_path,
                      a.horizontal_master_path horizontal_path
               FROM youtube_publications p JOIN video_render_jobs r ON r.id=p.render_job_id
               JOIN live_assets a ON a.render_job_id=r.id
               JOIN videos v ON v.id=r.video_id JOIN batches b ON b.id=v.batch_id
               JOIN niches n ON n.id=b.niche_id ORDER BY p.id DESC"""
        ).fetchall()]


def queue_publication(render_job_id, thumbnail_path, path=DEFAULT_DB):
    """Atomically freeze the currently approved copy and enqueue both formats."""
    thumbnail = Path(thumbnail_path)
    if not thumbnail.is_file():
        raise RuntimeError("A thumbnail horizontal ainda não foi gerada.")
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            """SELECT r.shorts_title,r.shorts_description,r.output_path,
                      r.status,r.shorts_copy_status,a.horizontal_status asset_status,a.horizontal_master_path
               FROM video_render_jobs r LEFT JOIN live_assets a ON a.render_job_id=r.id
               WHERE r.id=?""", (render_job_id,),
        ).fetchone()
        if not item:
            raise RuntimeError("Vídeo não encontrado.")
        ready = (item["status"] == "concluida" and item["shorts_copy_status"] == "concluida"
                 and item["asset_status"] in ("ready", "pending") and item["shorts_title"]
                 and item["shorts_description"] and item["output_path"]
                 and item["horizontal_master_path"] and Path(item["output_path"]).is_file()
                 and Path(item["horizontal_master_path"]).is_file())
        if not ready:
            raise RuntimeError("O par precisa de vertical, horizontal e copy concluídos antes da publicação.")
        existing = conn.execute(
            "SELECT id FROM youtube_publications WHERE render_job_id=?", (render_job_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        return conn.execute(
            """INSERT INTO youtube_publications(render_job_id,title,description,thumbnail_path)
               VALUES(?,?,?,?)""",
            (render_job_id, item["shorts_title"], item["shorts_description"], str(thumbnail)),
        ).lastrowid


def retry_publication(publication_id, path=DEFAULT_DB):
    """Requeue failures only; successfully persisted video IDs are immutable."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
        if not item or item["status"] not in ("erro", "parcial"):
            raise RuntimeError("Esta publicação não possui uma falha que possa ser repetida.")
        vertical = "na_fila" if item["vertical_status"] == "erro" else item["vertical_status"]
        horizontal = "na_fila" if item["horizontal_status"] == "erro" else item["horizontal_status"]
        thumbnail = "na_fila" if item["thumbnail_status"] == "erro" else item["thumbnail_status"]
        if horizontal == "na_fila" and not item["horizontal_youtube_id"]:
            thumbnail = "aguardando_horizontal"
        conn.execute(
            """UPDATE youtube_publications SET status='na_fila',vertical_status=?,horizontal_status=?,
                      thumbnail_status=?,vertical_error=NULL,horizontal_error=NULL,thumbnail_error=NULL,
                      worker_pid=NULL,heartbeat_at=NULL,completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (vertical, horizontal, thumbnail, publication_id),
        )


def _update(path, publication_id, **fields):
    allowed = {
        "status", "vertical_status", "horizontal_status", "thumbnail_status",
        "vertical_progress", "horizontal_progress", "vertical_youtube_id",
        "horizontal_youtube_id", "vertical_url", "horizontal_url",
        "vertical_error", "horizontal_error", "thumbnail_error",
    }
    if not fields or not set(fields) <= allowed:
        raise ValueError("Campo de publicação inválido.")
    assignments = ",".join(f"{name}=?" for name in fields)
    with connect(path) as conn:
        conn.execute(
            f"UPDATE youtube_publications SET {assignments},heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*fields.values(), publication_id),
        )


def _upload_video(publication, kind, media_path, db_path, service_factory):
    status_key, progress_key = f"{kind}_status", f"{kind}_progress"
    id_key, url_key, error_key = f"{kind}_youtube_id", f"{kind}_url", f"{kind}_error"
    _update(db_path, publication["id"], **{status_key: "enviando", progress_key: 0, error_key: None})
    try:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(media_path), chunksize=8 * 1024 * 1024, resumable=True)
        body = {
            "snippet": {"title": publication["title"], "description": publication["description"],
                        "categoryId": publication["category_id"], "defaultLanguage": publication["language"]},
            "status": {"privacyStatus": publication["privacy_status"],
                       "selfDeclaredMadeForKids": bool(publication["made_for_kids"])},
        }
        request = service_factory().videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            upload_status, response = request.next_chunk()
            if upload_status:
                _update(db_path, publication["id"], **{progress_key: min(99, int(upload_status.progress() * 100))})
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("O YouTube concluiu o envio sem retornar o ID do vídeo.")
        url = (f"https://www.youtube.com/shorts/{video_id}" if kind == "vertical"
               else f"https://youtu.be/{video_id}")
        _update(db_path, publication["id"], **{
            status_key: "concluido", progress_key: 100, id_key: video_id,
            url_key: url, error_key: None,
        })
        return video_id
    except Exception as exc:
        _update(db_path, publication["id"], **{status_key: "erro", error_key: str(exc)[-2000:]})
        return None


def _set_thumbnail(publication, video_id, db_path, service_factory):
    try:
        from googleapiclient.http import MediaFileUpload
        _update(db_path, publication["id"], thumbnail_status="enviando", thumbnail_error=None)
        media = MediaFileUpload(publication["thumbnail_path"], mimetype="image/png", resumable=False)
        service_factory().thumbnails().set(videoId=video_id, media_body=media).execute()
        _update(db_path, publication["id"], thumbnail_status="concluida", thumbnail_error=None)
        return True
    except Exception as exc:
        _update(db_path, publication["id"], thumbnail_status="erro", thumbnail_error=str(exc)[-2000:])
        return False


def _finish(publication_id, db_path):
    with connect(db_path) as conn:
        item = conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication_id,)).fetchone()
        done = (item["vertical_status"] == "concluido" and item["horizontal_status"] == "concluido"
                and item["thumbnail_status"] == "concluida")
        errors = any(item[key] == "erro" for key in ("vertical_status", "horizontal_status", "thumbnail_status"))
        any_done = any(item[key] in ("concluido", "concluida") for key in
                       ("vertical_status", "horizontal_status", "thumbnail_status"))
        state = "concluida" if done else ("parcial" if errors and any_done else "erro")
        conn.execute(
            """UPDATE youtube_publications SET status=?,worker_pid=NULL,heartbeat_at=NULL,
                      completed_at=CASE WHEN ?='concluida' THEN CURRENT_TIMESTAMP ELSE NULL END,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""", (state, state, publication_id),
        )
    return state


def process_next_publication(db_path=DEFAULT_DB, service_factory=build_youtube_service):
    """Claim one publication, upload missing videos concurrently, then set the horizontal thumbnail."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM youtube_publications WHERE status='na_fila' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        changed = conn.execute(
            """UPDATE youtube_publications SET status='publicando',attempts=attempts+1,worker_pid=?,
                      heartbeat_at=CURRENT_TIMESTAMP,started_at=coalesce(started_at,CURRENT_TIMESTAMP),
                      updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='na_fila'""",
            (os.getpid(), row["id"]),
        ).rowcount
        if not changed:
            return None
        publication = dict(conn.execute(
            """SELECT p.*,r.output_path vertical_path,a.horizontal_master_path horizontal_path
               FROM youtube_publications p JOIN video_render_jobs r ON r.id=p.render_job_id
               JOIN live_assets a ON a.render_job_id=r.id WHERE p.id=?""", (row["id"],)
        ).fetchone())

    work = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for kind in ("vertical", "horizontal"):
            if publication[f"{kind}_status"] == "na_fila" and not publication[f"{kind}_youtube_id"]:
                work[kind] = pool.submit(
                    _upload_video, publication, kind, publication[f"{kind}_path"], db_path, service_factory
                )
        results = {kind: future.result() for kind, future in work.items()}

    with connect(db_path) as conn:
        current = dict(conn.execute("SELECT * FROM youtube_publications WHERE id=?", (publication["id"],)).fetchone())
    horizontal_id = current["horizontal_youtube_id"] or results.get("horizontal")
    if horizontal_id and current["thumbnail_status"] in ("aguardando_horizontal", "na_fila"):
        _set_thumbnail(current, horizontal_id, db_path, service_factory)
    return _finish(publication["id"], db_path)
