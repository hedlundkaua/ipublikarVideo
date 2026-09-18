#!/usr/bin/env python3
"""One-time OAuth authorization helper for YouTube publication."""
import os
from pathlib import Path

from dotenv import load_dotenv

from publi.youtube import YOUTUBE_SCOPE, credential_paths


def main():
    load_dotenv(override=True)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit("Instale as dependências com: pip install -r requirements.txt") from exc
    client_file, token_file = credential_paths()
    if not client_file.is_file():
        raise SystemExit(f"Cliente OAuth não encontrado: {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), [YOUTUBE_SCOPE])
    auth_port = int(os.getenv("YOUTUBE_AUTH_PORT", "8765"))
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=auth_port,
        open_browser=False,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "Com o túnel SSH ativo, abra este endereço no navegador do seu computador:\n{url}\n"
        ),
        success_message="Autorização concluída. Você já pode fechar esta janela.",
    )
    if not credentials.refresh_token:
        raise SystemExit("O Google não retornou um refresh token; revogue o acesso e tente novamente.")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    os.chmod(token_file, 0o600)
    print(f"Autorização salva em {token_file} (modo 0600).")


if __name__ == "__main__":
    main()
