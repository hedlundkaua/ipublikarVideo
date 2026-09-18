# ipublikarVideo

# Painel Publi

Painel local para criar, revisar e renderizar vídeos de perguntas por nicho.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Defina `OPENROUTER_API_KEY` no ambiente ou em `.env` antes de gerar questões. A renderização requer `ffmpeg`. Ao colocar um vídeo na fila, a própria interface inicia um processo novo para renderizar um trabalho e encerrá-lo; não é necessário executar `worker.py` em outro terminal. Falhas ficam visíveis na aba Vídeos e podem ser reenfileiradas com **Renderizar novamente**.

## Live dual do YouTube

O serviço separado compartilha o SQLite e os arquivos renderizados, reconstrói o master 16:9 com os áudios já existentes e prepara proxies 720p30 H.264/AAC de 4 Mbps. Rode o backfill antes da primeira homologação:

```bash
.venv/bin/python live_service.py --prepare-all
```

Copie `deploy/live.env.example` para `/etc/publi/live.env`, preencha as duas chaves persistentes e proteja o arquivo com modo `0600`. As chaves não são salvas no banco nem exibidas no painel. Ajuste os caminhos/usuário de `deploy/publi-live.service`, instale a unidade e o arquivo de logrotate, então habilite o serviço. A trava em `live/live-service.lock` impede duas instâncias.

No YouTube Studio, habilite a transmissão dual, use Encoder para o vertical, associe as chaves horizontal e vertical, habilite início/fim automáticos, marque que não é conteúdo infantil e homologue primeiro como privada ou não listada. A operação automática usa `America/Sao_Paulo`, diariamente das 09:00 às 17:00. A aba **Lives** mostra ativos, fila, saúde e comandos de emergência.


## Publicação no YouTube

Consulte [`docs/YOUTUBE.md`](docs/YOUTUBE.md) para configurar OAuth, credenciais e o worker da aba **Publicação**.
