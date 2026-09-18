import os
import json
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from publi.database import (
    init_db, connect, delete_video, list_review_videos, list_video_jobs,
    requeue_video_job, requeue_shorts_copy, save_shorts_copy,
)
from publi.worker_manager import start_worker_if_needed
from publi.horizontal_manager import start_horizontal_worker_if_needed
from publi.youtube import list_publications
from publi.youtube_manager import start_youtube_worker_if_needed
from publi.youtube_ui import render_youtube_tab
from publi.questions import request_questions, QuestionValidationError, ProviderError
from publi.artwork import make_preview
from publi.live_service import enqueue_command
from publi.horizontal_assets import retry_phase
from publi.alternatives import (
    OPTION_COLUMNS, VIDEO_QUESTION_COUNT, SUPPORTED_VIDEO_QUESTION_COUNTS,
    question_labels, shuffle_question_options,
)

load_dotenv()
init_db()
st.set_page_config(page_title="Publi", page_icon="🎬", layout="wide")
st.title("Publi · criação de vídeos")
st.markdown("""
<style>
[class*="st-key-video-preview-layout-"] [data-testid="stHorizontalBlock"] { align-items: flex-start; }
@media (max-width: 800px) {
  [class*="st-key-video-preview-layout-"] [data-testid="stHorizontalBlock"] { flex-direction: column; }
  [class*="st-key-video-preview-layout-"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    flex: 1 1 auto !important; width: 100% !important; min-width: 100% !important;
  }
}
</style>
""", unsafe_allow_html=True)


def rows(sql, args=()):
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def delete_niche(niche_id):
    with connect() as conn:
        conn.execute("DELETE FROM youtube_publications WHERE render_job_id IN (SELECT r.id FROM video_render_jobs r JOIN videos v ON v.id=r.video_id JOIN batches b ON b.id=v.batch_id WHERE b.niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM video_render_jobs WHERE video_id IN (SELECT v.id FROM videos v JOIN batches b ON b.id=v.batch_id WHERE b.niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM video_questions WHERE video_id IN (SELECT v.id FROM videos v JOIN batches b ON b.id=v.batch_id WHERE b.niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM videos WHERE batch_id IN (SELECT id FROM batches WHERE niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM render_jobs WHERE question_id IN (SELECT q.id FROM questions q JOIN batches b ON b.id=q.batch_id WHERE b.niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM questions WHERE batch_id IN (SELECT id FROM batches WHERE niche_id=?)", (niche_id,))
        conn.execute("DELETE FROM batches WHERE niche_id=?", (niche_id,))
        conn.execute("DELETE FROM niches WHERE id=?", (niche_id,))


def video_delete_button(video_id, scope, render_status=None, full_width=False):
    confirmation_key = f"confirm-delete-video-{scope}-{video_id}"
    is_rendering = render_status == "renderizando"
    if st.button(
        "Excluir vídeo",
        key=f"delete-video-{scope}-{video_id}",
        disabled=is_rendering,
        help="Aguarde a renderização terminar para excluir." if is_rendering else None,
        use_container_width=full_width,
    ):
        st.session_state[confirmation_key] = True
    return confirmation_key


def video_delete_confirmation(video_id, scope):
    """Render destructive confirmation at the caller's full available width."""
    confirmation_key = f"confirm-delete-video-{scope}-{video_id}"
    if not st.session_state.get(confirmation_key):
        return

    st.warning("Esta ação remove o vídeo, suas questões e os arquivos renderizados. Não pode ser desfeita.")
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("Confirmar exclusão", key=f"confirm-delete-{scope}-{video_id}", type="primary"):
        try:
            delete_video(video_id)
            st.session_state.pop(confirmation_key, None)
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))
    if cancel_col.button("Cancelar", key=f"cancel-delete-{scope}-{video_id}"):
        st.session_state.pop(confirmation_key, None)
        st.rerun()


def video_delete_control(video_id, scope, render_status=None):
    """Render a confirmed delete action with widget keys unique to each tab."""
    video_delete_button(video_id, scope, render_status)
    video_delete_confirmation(video_id, scope)


def save_shorts_edits(job_id, title_key, description_key):
    try:
        save_shorts_copy(job_id, st.session_state[title_key], st.session_state[description_key])
        st.session_state.pop(f"shorts-save-error-{job_id}", None)
    except (ValueError, RuntimeError) as exc:
        st.session_state[f"shorts-save-error-{job_id}"] = str(exc)


def replace_question(link):
    """Preserve the rejected row, replacing only the active composition slot."""
    generated = request_questions(link["niche"], 1, link["difficulty"], link["theme"] or "")
    q = generated[0]
    with connect() as conn:
        new_id = conn.execute(
            "INSERT INTO questions(batch_id,question,option_a,option_b,option_c,option_d,correct_option,explanation) VALUES(?,?,?,?,?,?,?,?)",
            (link["batch_id"], q["question"], *q["options"], q["correct_option"], q.get("explanation", "")),
        ).lastrowid
        conn.execute("UPDATE video_questions SET active=0 WHERE id=?", (link["link_id"],))
        conn.execute("INSERT INTO video_questions(video_id,question_id,position,replaced_question_id) VALUES(?,?,?,?)",
                     (link["video_id"], new_id, link["position"], link["question_id"]))
        conn.execute("UPDATE questions SET labels_dynamic=1 WHERE id=?", (new_id,))
        shuffle_question_options(conn, link["video_id"], new_id, link["position"])


tab_niches, tab_batches, tab_review, tab_videos, tab_publication, tab_lives = st.tabs(["Nichos", "Lotes", "Revisão", "Vídeos", "Publicação", "Lives"])
with tab_niches:
    with st.form("new-niche", clear_on_submit=True):
        a, b, c, d = st.columns(4)
        name = a.text_input("Nome")
        color = b.color_picker("Cor Publi", "#FF6B35")
        voice = c.text_input("Voz", "pt-BR-AntonioNeural")
        outfit = d.text_input("Roupa/acessório (PNG)")
        if st.form_submit_button("Salvar nicho"):
            if not name.strip(): st.error("Informe o nome.")
            elif rows("SELECT 1 FROM niches WHERE name = ? COLLATE NOCASE OR color = ?", (name.strip(), color)):
                st.error("Nome e cor devem ser exclusivos.")
            else:
                with connect() as conn: conn.execute("INSERT INTO niches(name,color,voice,outfit_path) VALUES(?,?,?,?)", (name.strip(), color, voice, outfit or None))
                st.rerun()
    for n in rows("SELECT * FROM niches ORDER BY id DESC"):
        left, right = st.columns([7, 1])
        left.markdown(f"<span style='display:inline-block;width:18px;height:18px;border-radius:50%;background:{n['color']};border:2px solid #000'></span> **{n['name']}** · {n['voice']}", unsafe_allow_html=True)
        if right.button("Apagar", key=f"delete-niche-{n['id']}"):
            delete_niche(n["id"]); st.rerun()

with tab_batches:
    niches = rows("SELECT * FROM niches ORDER BY name")
    if not niches: st.info("Crie um nicho primeiro.")
    else:
        with st.form("batch"):
            niche = st.selectbox("Nicho", niches, format_func=lambda x: x["name"])
            amount, difficulty = st.columns(2)
            quantity = amount.number_input("Número de vídeos", 1, 20, 1)
            level = difficulty.selectbox("Dificuldade", ["fácil", "média", "difícil"])
            theme = st.text_input("Tema (opcional)")
            if st.form_submit_button("Gerar questões"):
                try:
                    video_count = int(quantity)
                    questions = request_questions(niche["name"], video_count * VIDEO_QUESTION_COUNT, level, theme)
                    with connect() as conn:
                        batch_id = conn.execute("INSERT INTO batches(niche_id,quantity,difficulty,theme) VALUES(?,?,?,?)", (niche["id"], video_count, level, theme or None)).lastrowid
                        for video_position in range(1, video_count + 1):
                            video_id = conn.execute("INSERT INTO videos(batch_id,position,title) VALUES(?,?,?)", (batch_id, video_position, f"Vídeo {video_position}")).lastrowid
                            for slot, q in enumerate(questions[(video_position - 1) * VIDEO_QUESTION_COUNT:video_position * VIDEO_QUESTION_COUNT], 1):
                                qid = conn.execute("INSERT INTO questions(batch_id,question,option_a,option_b,option_c,option_d,correct_option,explanation) VALUES(?,?,?,?,?,?,?,?)", (batch_id, q["question"], *q["options"], q["correct_option"], q.get("explanation", ""))).lastrowid
                                conn.execute("INSERT INTO video_questions(video_id,question_id,position) VALUES(?,?,?)", (video_id, qid, slot))
                                shuffle_question_options(conn, video_id, qid, slot)
                                conn.execute("UPDATE questions SET labels_dynamic=1 WHERE id=?", (qid,))
                    st.success(f"{video_count} vídeos e {len(questions)} questões aguardando revisão.")
                except (RuntimeError, QuestionValidationError, ProviderError) as exc: st.error(str(exc))
                except Exception as exc: st.error(f"Falha ao gerar questões: {exc}")

with tab_review:
    videos = list_review_videos()
    if not videos: st.info("Nenhum vídeo novo aguardando revisão. Conteúdo antigo continua na aba Vídeos.")
    for video in videos:
        links = rows("SELECT vq.id link_id,vq.video_id,vq.position,q.id question_id,q.*,b.difficulty,b.theme,n.name niche FROM video_questions vq JOIN questions q ON q.id=vq.question_id JOIN videos v ON v.id=vq.video_id JOIN batches b ON b.id=v.batch_id JOIN niches n ON n.id=b.niche_id WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position", (video["id"],))
        approved = len(links) in SUPPORTED_VIDEO_QUESTION_COUNTS and all(q["status"] == "aprovada" for q in links)
        with st.expander(f"{video['title']} · {video['niche']} · {'Pronto para renderizar' if approved else 'Em revisão'}", expanded=not approved):
            video_delete_control(video["id"], "review")
            for q in links:
                st.markdown(f"**{q['position']}. {q['question']}** · `{q['status']}`")
                option_values = [q[key] for key in OPTION_COLUMNS]
                labels = question_labels(q)
                for i, (label, option) in enumerate(zip(labels, option_values)):
                    st.write(f"{'✅' if i == q['correct_option'] else '○'} {label}. {option}")
                if st.button("Prévia com Publi", key=f"preview-{q['id']}"):
                    preview_path = Path("output") / f"preview_question_{q['id']}.png"
                    try:
                        make_preview(q["question"], option_values, video["color"] if "color" in video else "#FF6B35", preview_path, labels=labels)
                        st.image(str(preview_path), width=180)
                    except Exception as exc:
                        st.error(str(exc))
                a, b, c, d, e = st.columns(5)
                if a.button("Aprovar", key=f"approve-{q['id']}"):
                    with connect() as conn: conn.execute("UPDATE questions SET status='aprovada', rejection_reason=NULL WHERE id=?", (q['id'],))
                    st.rerun()
                if b.button("Rejeitar", key=f"reject-{q['id']}"):
                    with connect() as conn: conn.execute("UPDATE questions SET status='rejeitada', rejection_reason='Rejeitada pelo operador' WHERE id=?", (q['id'],))
                    st.rerun()
                if c.button("↑", key=f"up-{q['link_id']}", disabled=q['position'] == 1):
                    with connect() as conn:
                        other = conn.execute("SELECT id FROM video_questions WHERE video_id=? AND active=1 AND position=?", (q['video_id'], q['position']-1)).fetchone()
                        conn.execute("UPDATE video_questions SET active=0 WHERE id=?", (q['link_id'],))
                        conn.execute("UPDATE video_questions SET position=? WHERE id=?", (q['position'], other['id']))
                        conn.execute("UPDATE video_questions SET position=?, active=1 WHERE id=?", (q['position']-1, q['link_id']))
                    st.rerun()
                if d.button("↓", key=f"down-{q['link_id']}", disabled=q["position"] == len(links)):
                    with connect() as conn:
                        other = conn.execute("SELECT id FROM video_questions WHERE video_id=? AND active=1 AND position=?", (q['video_id'], q['position']+1)).fetchone()
                        conn.execute("UPDATE video_questions SET active=0 WHERE id=?", (q['link_id'],))
                        conn.execute("UPDATE video_questions SET position=? WHERE id=?", (q['position'], other['id']))
                        conn.execute("UPDATE video_questions SET position=?, active=1 WHERE id=?", (q['position']+1, q['link_id']))
                    st.rerun()
                if e.button("Editar", key=f"edit-{q['id']}"): st.session_state[f"edit-{q['id']}"] = True
                if q['status'] == 'rejeitada' and st.button("Gerar substituta", key=f"replace-{q['link_id']}"):
                    try: replace_question(q); st.rerun()
                    except (ProviderError, QuestionValidationError) as exc: st.error(str(exc))
                if st.session_state.get(f"edit-{q['id']}"):
                    with st.form(f"editform-{q['id']}"):
                        text = st.text_area("Pergunta", q['question'])
                        edit_labels = question_labels(q)
                        opts = [st.text_input(f"Opção {label}", q[key]) for label,key in zip(edit_labels, OPTION_COLUMNS)]
                        answer = st.selectbox("Correta", [0,1,2,3], index=q['correct_option'], format_func=lambda i: edit_labels[i])
                        explanation = st.text_area("Explicação", q.get("explanation", ""))
                        if st.form_submit_button("Salvar edição"):
                            with connect() as conn: conn.execute("UPDATE questions SET question=?,option_a=?,option_b=?,option_c=?,option_d=?,correct_option=?,explanation=? WHERE id=?", (text,*opts,answer,explanation,q['id']))
                            st.session_state.pop(f"edit-{q['id']}", None); st.rerun()
            if approved and st.button("Enviar vídeo para renderização", key=f"queue-{video['id']}"):
                with connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO video_render_jobs(video_id) VALUES(?)", (video['id'],))
                    conn.execute("UPDATE questions SET status='na_fila' WHERE id IN (SELECT question_id FROM video_questions WHERE video_id=? AND active=1)", (video['id'],))
                st.rerun()

with tab_videos:
    jobs = list_video_jobs()
    legacy = rows("SELECT r.*,q.question,n.name niche FROM render_jobs r JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id JOIN niches n ON n.id=b.niche_id ORDER BY r.id DESC")
    if not jobs and not legacy: st.info("Nenhum vídeo na fila.")
    for j in jobs:
        qs = rows("SELECT q.question FROM video_questions vq JOIN questions q ON q.id=vq.question_id WHERE vq.video_id=? AND vq.active=1 ORDER BY vq.position", (j['video_id'],))
        st.write(f"**{j['title']} · {j['niche']}**")
        st.caption(" | ".join(q['question'] for q in qs))
        if j.get("duration_seconds"):
            st.caption(f"Duração calculada: {j['duration_seconds']:.2f} s")
        scene_status = json.loads(j.get("scene_status") or "{}")
        if scene_status:
            st.caption(" · ".join(f"Cena {number}: " + ", ".join(f"{name} {state}" for name, state in state_map.items()) for number, state_map in scene_status.items()))
        st.progress(j['progress'], text=f"{j['status']} · {j['progress']}%")
        if j['status'] == 'erro':
            st.error(j['error'] or 'Erro desconhecido')

        output_exists = (
            j['status'] == 'concluida' and j['output_path']
            and Path(j['output_path']).exists()
        )
        if output_exists:
            video_bytes = Path(j['output_path']).read_bytes()
            title_key = f"shorts-title-{j['id']}"
            description_key = f"shorts-description-{j['id']}"
            loaded_key = f"shorts-loaded-{j['id']}"
            fields_ready = bool(j.get("shorts_title") and j.get("shorts_description"))
            persisted_copy = (j.get("shorts_title") or "", j.get("shorts_description") or "")
            if st.session_state.get(loaded_key) != persisted_copy:
                st.session_state[title_key] = persisted_copy[0]
                st.session_state[description_key] = persisted_copy[1]
                st.session_state[loaded_key] = persisted_copy

            with st.container(key=f"video-preview-layout-{j['id']}"):
                vertical_col, horizontal_col = st.columns(2, gap="large")
                with vertical_col:
                    st.markdown("**Vertical (9:16)**")
                    st.video(video_bytes)
                with horizontal_col:
                    st.markdown("**Horizontal (16:9)**")
                    horizontal_path = Path(j["live_horizontal_path"]) if j.get("live_horizontal_path") else None
                    if j.get("live_horizontal_status") == "ready" and horizontal_path and horizontal_path.exists():
                        st.video(horizontal_path.read_bytes())
                        if j.get("live_proxy_status") == "error":
                            st.warning("Horizontal pronto; falha apenas nos proxies da live.")
                            if st.button("Repetir proxies", key=f"retry-proxy-{j['id']}"):
                                retry_phase(j["live_asset_id"], "proxy")
                                st.rerun()
                    elif j.get("live_horizontal_status") == "ready":
                        st.error("Erro: arquivo horizontal não encontrado.")
                    elif j.get("live_horizontal_status") == "error":
                        st.error("Erro na preparação do horizontal.")
                        if j.get("live_asset_error"):
                            st.caption(j["live_asset_error"])
                        if st.button("Repetir horizontal", key=f"retry-horizontal-{j['id']}"):
                            retry_phase(j["live_asset_id"], "horizontal")
                            st.rerun()
                    elif j.get("live_horizontal_status") == "building":
                        st.info("Horizontal em preparação.")
                    else:
                        st.info("Horizontal pendente.")

            if j.get("shorts_copy_status") in ("na_fila", "gerando"):
                st.info("Gerando título e descrição para Shorts…")
            st.text_input(
                "Título do Shorts", key=title_key, max_chars=100,
                disabled=not fields_ready,
                on_change=save_shorts_edits,
                args=(j['id'], title_key, description_key),
            )
            st.text_area(
                "Descrição do Shorts", key=description_key, height=180,
                disabled=not fields_ready,
                on_change=save_shorts_edits,
                args=(j['id'], title_key, description_key),
            )
            if st.session_state.get(f"shorts-save-error-{j['id']}"):
                st.error(st.session_state[f"shorts-save-error-{j['id']}"])
            if j.get("shorts_copy_status") == "erro":
                st.error(j.get("shorts_copy_error") or "Não foi possível gerar a copy.")
                if st.button("Gerar textos novamente", key=f"retry-copy-{j['id']}"):
                    try:
                        requeue_shorts_copy(j['id'])
                        st.rerun()
                    except RuntimeError as exc:
                        st.error(str(exc))

            download_col, delete_col, rerender_col = st.columns(3)
            download_col.download_button(
                "Baixar MP4", video_bytes, Path(j['output_path']).name, "video/mp4",
                key=f"download-video-{j['id']}-v{j['render_version']}",
                use_container_width=True,
            )
            with delete_col:
                video_delete_button(j["video_id"], "videos", j["status"], full_width=True)
            if rerender_col.button(
                "Renderizar novamente", key=f"rerender-video-{j['id']}",
                use_container_width=True,
            ):
                try:
                    requeue_video_job(j['id'])
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
            video_delete_confirmation(j["video_id"], "videos")
        elif j['status'] in ('erro', 'concluida'):
            rerender_col, delete_col = st.columns(2)
            if rerender_col.button("Renderizar novamente", key=f"rerender-video-{j['id']}"):
                try:
                    requeue_video_job(j['id'])
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
            with delete_col:
                video_delete_button(j["video_id"], "videos", j["status"], full_width=True)
            video_delete_confirmation(j["video_id"], "videos")
        else:
            video_delete_control(j["video_id"], "videos", j["status"])
    if legacy:
        st.divider(); st.caption("Vídeos legados (uma questão por arquivo)")
    for j in legacy:
        st.write(f"**{j['niche']}** · {j['question']}"); st.progress(j['progress'], text=f"{j['status']} · {j['progress']}%")
        if j['output_path'] and Path(j['output_path']).exists(): st.video(j['output_path'])

with tab_publication:
    render_youtube_tab()

with tab_lives:
    session = rows("SELECT * FROM live_sessions ORDER BY service_date DESC LIMIT 1")
    assets = rows("""SELECT a.*,v.title,n.name niche FROM live_assets a
                     JOIN videos v ON v.id=a.video_id JOIN batches b ON b.id=v.batch_id
                     JOIN niches n ON n.id=b.niche_id ORDER BY a.completed_at,a.id""")
    ready = sum(asset["status"] == "ready" for asset in assets)
    building = sum(asset["status"] in ("pending", "building") for asset in assets)
    failed = sum(asset["status"] == "error" for asset in assets)
    a, b, c = st.columns(3)
    a.metric("Pares prontos", ready)
    b.metric("Em preparação", building)
    c.metric("Com erro", failed)
    if session:
        current = session[0]
        st.write(f"**Sessão {current['service_date']}** · `{current['status']}`")
        health_a, health_b = st.columns(2)
        health_a.metric("Sinal vertical", current["vertical_health"])
        health_b.metric("Sinal horizontal", current["horizontal_health"])
        if current.get("error"):
            st.error(current["error"])
        controls = st.columns(3)
        if controls[0].button("Parar agora", use_container_width=True):
            enqueue_command("stop"); st.success("Comando de parada enviado.")
        if controls[1].button("Reiniciar sinais", use_container_width=True):
            enqueue_command("restart"); st.success("Reinício conjunto solicitado.")
        if controls[2].button("Ignorar item", use_container_width=True):
            enqueue_command("skip"); st.success("Item atual será ignorado nos dois sinais.")
        queue = rows("""SELECT p.sequence,p.cycle,p.status,p.started_at,p.ended_at,
                               a.duration_seconds,v.title,n.name niche
                        FROM live_playback p LEFT JOIN live_assets a ON a.id=p.asset_id
                        LEFT JOIN videos v ON v.id=a.video_id
                        LEFT JOIN batches b ON b.id=v.batch_id LEFT JOIN niches n ON n.id=b.niche_id
                        WHERE p.session_id=? ORDER BY p.sequence DESC LIMIT 30""", (current["id"],))
        if queue:
            st.dataframe(queue, use_container_width=True, hide_index=True)
    else:
        st.info("O serviço ainda não criou uma sessão diária.")
    st.subheader("Prontidão dos formatos")
    if assets:
        st.dataframe([
            {"vídeo": asset["title"], "nicho": asset["niche"], "estado": asset["status"],
             "duração": asset["duration_seconds"], "erro": asset["error"]}
            for asset in assets
        ], use_container_width=True, hide_index=True)
    else:
        st.info("Nenhum vídeo concluído foi descoberto pelo serviço ainda.")

# Horizontal assets run independently and may overlap one vertical render.
try:
    start_horizontal_worker_if_needed()
except Exception as exc:
    st.error(f"Falha ao iniciar o worker horizontal: {exc}")

# YouTube uploads run in their own process, independently of rendering.
try:
    start_youtube_worker_if_needed()
except Exception as exc:
    st.error(f"Falha ao iniciar o worker de publicação: {exc}")

# Each queued video gets a fresh process; the process renders one job and exits.
try:
    start_worker_if_needed()
except Exception as exc:
    st.error(f"Falha ao iniciar o worker automático: {exc}")

publication_running = any(p["status"] in ("na_fila", "publicando") for p in list_publications())
if publication_running or any(
    j['status'] in ('na_fila', 'renderizando')
    or j.get('shorts_copy_status') in ('na_fila', 'gerando')
    or j.get('live_horizontal_status') in ('pending', 'building') or j.get('live_proxy_status') in ('pending', 'building')
    or (j['status'] == 'concluida' and not j.get('live_asset_status'))
    for j in jobs
):
    time.sleep(2)
    st.rerun()
