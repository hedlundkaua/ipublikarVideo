import json
import os
import re

from .database import DEFAULT_DB, connect
from .alternatives import SUPPORTED_VIDEO_QUESTION_COUNTS
from .questions import ProviderError, _post_with_retries

MAX_SHORTS_TITLE = 100
MAX_SHORTS_DESCRIPTION = 700
SHORTS_SCHEMA = {
    "type": "OBJECT",
    "properties": {"title": {"type": "STRING"}, "description": {"type": "STRING"}},
    "required": ["title", "description"],
}


class ShortsCopyValidationError(ValueError):
    pass


def validate_shorts_copy(payload):
    if not isinstance(payload, dict):
        raise ShortsCopyValidationError("A copy precisa ser um objeto JSON.")
    title = str(payload.get("title", payload.get("titulo", ""))).strip()
    description = str(payload.get("description", payload.get("descricao", ""))).strip()
    if not title or len(title) > MAX_SHORTS_TITLE:
        raise ShortsCopyValidationError("O título deve ter entre 1 e 100 caracteres.")
    if not description or len(description) > MAX_SHORTS_DESCRIPTION:
        raise ShortsCopyValidationError("A descrição deve ser curta e ter no máximo 700 caracteres.")
    hashtags = re.findall(r"(?<!\w)#[\wÀ-ÿ]+", description, flags=re.UNICODE)
    if not 5 <= len(hashtags) <= 8:
        raise ShortsCopyValidationError("A descrição deve terminar com 5 a 8 hashtags.")
    lines = description.splitlines()
    hashtag_lines = [index for index, line in enumerate(lines) if "#" in line]
    if not hashtag_lines or hashtag_lines[0] != len(lines) - 1:
        raise ShortsCopyValidationError("As hashtags devem ficar somente no final da descrição.")
    if re.sub(r"(?<!\w)#[\wÀ-ÿ]+", "", lines[-1], flags=re.UNICODE).strip():
        raise ShortsCopyValidationError("A última linha deve conter somente hashtags.")
    if "#" in "\n".join(lines[:-1]):
        raise ShortsCopyValidationError("As hashtags devem ficar somente no final da descrição.")
    return {"title": title, "description": description}


def _copy_prompt(niche, theme, difficulty, questions):
    question_list = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    return (
        "Crie a copy em português do Brasil para publicar este quiz como YouTube Shorts. "
        "Retorne somente JSON com title e description. O título deve ser atrativo, fiel ao conteúdo "
        "e ter no máximo 100 caracteres. A descrição deve ser curta, convidar a pessoa a participar "
        "e não revelar nem insinuar nenhuma resposta. Termine a descrição com uma nova linha contendo "
        "somente de 5 a 8 hashtags relevantes. Não use hashtags no título.\n"
        f"Nicho: {niche}\nTema: {theme or 'não informado'}\nDificuldade: {difficulty}\n"
        f"Perguntas do vídeo:\n{question_list}"
    )


def _google_copy(prompt):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY não está definida.")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    data = _post_with_retries(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"responseMimeType": "application/json", "responseSchema": SHORTS_SCHEMA}},
    )
    try:
        return validate_shorts_copy(json.loads(data["candidates"][0]["content"]["parts"][0]["text"]))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("O Google AI Studio não retornou uma copy válida.") from exc


def _openrouter_copy(prompt):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ProviderError("OPENROUTER_API_KEY não está definida.")
    configured = [item.strip() for item in os.getenv("SHORTS_MODELS", "").split(",") if item.strip()]
    models = configured or [os.getenv("SHORTS_MODEL", os.getenv("QUESTION_MODEL", "openrouter/free"))]
    if "openrouter/free" not in models:
        models.append("openrouter/free")
    data = _post_with_retries(
        "https://openrouter.ai/api/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"models": models, "messages": [{"role": "user", "content": prompt}],
         "response_format": {"type": "json_object"}},
    )
    try:
        content = data["choices"][0]["message"]["content"]
        return validate_shorts_copy(json.loads(re.sub(r"^```json\s*|\s*```$", "", content.strip())))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("A OpenRouter não retornou uma copy válida.") from exc


def request_shorts_copy(niche, theme, difficulty, questions):
    if len(questions) not in SUPPORTED_VIDEO_QUESTION_COUNTS:
        raise ShortsCopyValidationError("São necessárias quatro perguntas; vídeos antigos com cinco continuam compatíveis.")
    prompt = _copy_prompt(niche, theme, difficulty, questions)
    errors = []
    if os.getenv("GEMINI_API_KEY"):
        try:
            return _google_copy(prompt)
        except (ProviderError, ShortsCopyValidationError) as exc:
            errors.append(f"Google AI Studio: {exc}")
    if os.getenv("OPENROUTER_API_KEY"):
        try:
            return _openrouter_copy(prompt)
        except (ProviderError, ShortsCopyValidationError) as exc:
            errors.append(f"OpenRouter: {exc}")
    if not errors:
        raise ProviderError("Defina GEMINI_API_KEY ou OPENROUTER_API_KEY no .env.")
    raise ProviderError("; ".join(errors))


def generate_shorts_copy_for_job(job_id, db_path=DEFAULT_DB):
    """Generate copy while keeping a completed MP4 completed on any failure."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            """SELECT r.id,r.status,r.shorts_copy_status,b.theme,b.difficulty,n.name niche
               FROM video_render_jobs r JOIN videos v ON v.id=r.video_id
               JOIN batches b ON b.id=v.batch_id JOIN niches n ON n.id=b.niche_id
               WHERE r.id=?""", (job_id,),
        ).fetchone()
        if not job or job["status"] != "concluida" or job["shorts_copy_status"] != "na_fila":
            return False
        conn.execute(
            "UPDATE video_render_jobs SET shorts_copy_status='gerando',shorts_copy_error=NULL WHERE id=?",
            (job_id,),
        )
        questions = [row["question"] for row in conn.execute(
            """SELECT q.question FROM video_render_jobs r
               JOIN video_questions vq ON vq.video_id=r.video_id
               JOIN questions q ON q.id=vq.question_id
               WHERE r.id=? AND vq.active=1 ORDER BY vq.position""", (job_id,),
        ).fetchall()]
    try:
        copy = request_shorts_copy(job["niche"], job["theme"], job["difficulty"], questions)
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE video_render_jobs SET shorts_title=?,shorts_description=?,
                   shorts_copy_status='concluida',shorts_copy_error=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND shorts_copy_status='gerando'""",
                (copy["title"], copy["description"], job_id),
            )
        return True
    except Exception as exc:
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE video_render_jobs SET shorts_copy_status='erro',shorts_copy_error=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND shorts_copy_status='gerando'""",
                (str(exc), job_id),
            )
        return False


def process_next_shorts_copy(db_path=DEFAULT_DB):
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT id FROM video_render_jobs
               WHERE status='concluida' AND shorts_copy_status='na_fila'
               ORDER BY id LIMIT 1"""
        ).fetchone()
    return None if not row else generate_shorts_copy_for_job(row["id"], db_path)
