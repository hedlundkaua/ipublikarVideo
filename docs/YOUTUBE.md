# Publicação no YouTube

Crie credenciais OAuth do tipo **Aplicativo para computador** em um projeto com a YouTube Data API v3 habilitada. Durante os testes, inclua como **Usuário de teste** o e-mail Google que administra o canal.

Salve as credenciais fora do Git:

- desenvolvimento: `secrets/youtube-client-secret.json` e `secrets/youtube-token.json`;
- produção: `/etc/publi/youtube-client-secret.json` e `/etc/publi/youtube-token.json`.

Em produção, ambos os arquivos devem pertencer ao usuário `publi` e usar modo `0600`. Configure apenas os caminhos no `.env`:

```dotenv
YOUTUBE_CLIENT_SECRETS_FILE=secrets/youtube-client-secret.json
YOUTUBE_TOKEN_FILE=secrets/youtube-token.json
```

Autorize o canal uma única vez:

```bash
.venv/bin/python youtube_auth.py
chmod 600 secrets/youtube-client-secret.json secrets/youtube-token.json
```

O escopo solicitado é somente `youtube.upload` ([OAuth 2.0](https://developers.google.com/identity/protocols/oauth2)). Apps OAuth externos no estado **Testando** normalmente recebem refresh tokens que expiram em sete dias; publique o app para operação contínua. Conforme a [documentação de vídeos](https://developers.google.com/youtube/v3/docs/videos), projetos novos da YouTube API ainda não auditados podem ter uploads forçados para **Privado**, embora o painel solicite **Não listado**.

A aba **Publicação** só lista pares com vertical, horizontal e copy concluídos. A confirmação congela título e descrição. Um worker separado envia os dois formatos em paralelo com upload retomável e aplica a thumbnail somente ao horizontal. Falhas podem ser repetidas individualmente sem reenviar partes já concluídas.
