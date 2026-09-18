import json
import os
import re
import time

import requests

MAX_QUESTION = 145
MAX_OPTION = 65
QUESTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "options": {"type": "ARRAY", "minItems": 4, "maxItems": 4, "items": {"type": "STRING"}},
                    "correct_option": {"type": "INTEGER"},
                    "explanation": {"type": "STRING"},
                },
                "required": ["question", "options", "correct_option", "explanation"],
            },
        },
    },
    "required": ["questions"],
}


class QuestionValidationError(ValueError):
    pass


class ProviderError(RuntimeError):
    pass


def validate_questions(payload):
    if isinstance(payload, dict):
        payload = payload.get("questions", payload.get("questoes", []))
    if not isinstance(payload, list) or not payload:
        raise QuestionValidationError("A resposta deve conter uma lista de questões.")
    cleaned = []
    for item in payload:
        if not isinstance(item, dict):
            raise QuestionValidationError("Cada questão precisa ser um objeto JSON.")
        question = str(item.get("question", item.get("pergunta", ""))).strip()
        options = item.get("options", item.get("alternativas", []))
        answer = item.get("correct_option", item.get("resposta_correta"))
        explanation = str(item.get("explanation", item.get("explicacao", ""))).strip()
        if not question or len(question) > MAX_QUESTION:
            raise QuestionValidationError("Pergunta ausente ou longa demais.")
        if not isinstance(options, list) or len(options) != 4:
            raise QuestionValidationError("Cada questão precisa de exatamente quatro alternativas.")
        options = [str(x).strip() for x in options]
        if any(not x or len(x) > MAX_OPTION for x in options) or len({x.casefold() for x in options}) != 4:
            raise QuestionValidationError("Alternativas devem ser únicas, preenchidas e curtas.")
        if isinstance(answer, str) and answer.strip().upper() in ("A", "B", "C", "D"):
            answer = "ABCD".index(answer.strip().upper())
        if not isinstance(answer, int) or answer not in range(4):
            raise QuestionValidationError("Resposta correta deve ser A, B, C, D ou índice 0 a 3.")
        if len(explanation) > 220:
            raise QuestionValidationError("Explicação longa demais.")
        cleaned.append({"question": question, "options": options, "correct_option": answer, "explanation": explanation})
    return cleaned


def _prompt(niche, quantity, difficulty, theme):
    scope = f" Tema: {theme}." if theme else ""
    return (f"Crie {quantity} perguntas de quiz em português para o nicho {niche}, dificuldade {difficulty}.{scope} "
            'Retorne somente JSON com {"questions":[{"question":"...","options":["...","...","...","..."],"correct_option":0,"explanation":"..."}]}. '
            f"Exatamente quatro alternativas únicas, resposta índice 0 a 3 e explicação curta de no máximo 220 caracteres. Pergunta com no máximo {MAX_QUESTION} caracteres e cada alternativa com no máximo {MAX_OPTION} caracteres.")


def _post_with_retries(url, headers, payload):
    max_attempts = max(1, int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "5")))
    retryable = {429, 500, 502, 503, 504}
    response = None
    for attempt in range(max_attempts):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
        except requests.RequestException as exc:
            if attempt == max_attempts - 1:
                raise ProviderError(f"Falha de rede após {max_attempts} tentativas: {exc}") from exc
            time.sleep(min(2 ** attempt, 20))
            continue
        if response.status_code not in retryable:
            break
        if attempt < max_attempts - 1:
            try:
                delay = min(float(response.headers.get("Retry-After", 2 ** attempt)), 20)
            except (TypeError, ValueError):
                delay = min(2 ** attempt, 20)
            time.sleep(max(delay, 0))
    if response is None:
        raise ProviderError("O provedor não retornou resposta.")
    if response.status_code == 429:
        raise ProviderError(f"Limite de requisições persistiu após {max_attempts} tentativas; aguarde a renovação da cota ou use um modelo com créditos.")
    if response.status_code in {500, 502, 503, 504}:
        raise ProviderError(f"Serviço indisponível ({response.status_code}) após {max_attempts} tentativas.")
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ProviderError(f"Erro do provedor ({response.status_code}).") from exc
    try:
        return response.json()
    except requests.JSONDecodeError as exc:
        raise ProviderError("O provedor retornou uma resposta que não é JSON.") from exc


def _google_questions(prompt):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY não está definida.")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    data = _post_with_retries(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": QUESTION_SCHEMA}},
    )
    try:
        return validate_questions(json.loads(data["candidates"][0]["content"]["parts"][0]["text"]))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("O Google AI Studio não retornou JSON de questões válido.") from exc


def _openrouter_questions(prompt):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ProviderError("OPENROUTER_API_KEY não está definida.")
    model = os.getenv("QUESTION_MODEL", "openrouter/free")
    configured = [item.strip() for item in os.getenv("QUESTION_MODELS", "").split(",") if item.strip()]
    models = configured or [model]
    if "openrouter/free" not in models:
        models.append("openrouter/free")
    data = _post_with_retries(
        "https://openrouter.ai/api/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"models": models, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}},
    )
    try:
        content = data["choices"][0]["message"]["content"]
        return validate_questions(json.loads(re.sub(r"^```json\s*|\s*```$", "", content.strip())))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("A OpenRouter não retornou JSON de questões válido.") from exc


def request_questions(niche, quantity, difficulty, theme=""):
    """Google AI Studio é a origem preferencial; OpenRouter é somente fallback."""
    prompt = _prompt(niche, quantity, difficulty, theme)
    errors = []
    if os.getenv("GEMINI_API_KEY"):
        try:
            result = _google_questions(prompt)
            if len(result) != quantity: raise QuestionValidationError(f"O provedor retornou {len(result)} questões; eram esperadas {quantity}.")
            return result
        except (ProviderError, QuestionValidationError) as exc:
            errors.append(f"Google AI Studio: {exc}")
    if os.getenv("OPENROUTER_API_KEY"):
        try:
            result = _openrouter_questions(prompt)
            if len(result) != quantity: raise QuestionValidationError(f"O provedor retornou {len(result)} questões; eram esperadas {quantity}.")
            return result
        except (ProviderError, QuestionValidationError) as exc:
            errors.append(f"OpenRouter: {exc}")
    if not errors:
        raise ProviderError("Defina GEMINI_API_KEY ou OPENROUTER_API_KEY no .env.")
    raise ProviderError("; ".join(errors))
