"""Streamlit presentation for the YouTube publication queue."""
from pathlib import Path

import streamlit as st

from .artwork import make_youtube_thumbnail
from .youtube import (
    list_publication_candidates, list_publications, queue_publication,
    retry_publication,
)


def render_youtube_tab():
    st.caption("O par é enviado ao mesmo canal como Não listado; a thumbnail é aplicada somente ao horizontal.")
    candidates = list_publication_candidates()
    queued_ids = {item["render_job_id"] for item in list_publications()}
    available = [item for item in candidates if item["render_job_id"] not in queued_ids]
    if not available and not queued_ids:
        st.info("Nenhum par com vertical, horizontal e copy concluídos está pronto para publicação.")
    for item in available:
        thumbnail_path = Path("output") / f"video_{item['render_job_id']}_youtube_thumbnail.png"
        try:
            make_youtube_thumbnail(item["niche"], item["color"], thumbnail_path, item["outfit_path"])
        except Exception as exc:
            st.error(f"Não foi possível gerar a thumbnail de {item['niche']}: {exc}")
            continue
        with st.expander(f"{item['niche']} · pronto para confirmar"):
            preview_col, metadata_col = st.columns([1, 1])
            preview_col.image(str(thumbnail_path), caption="Thumbnail do vídeo horizontal")
            metadata_col.markdown(f"**Título**  \n{item['title']}")
            metadata_col.text(item["description"])
            confirmation_key = f"confirm-youtube-{item['render_job_id']}"
            if st.button("Publicar par", key=f"publish-youtube-{item['render_job_id']}"):
                st.session_state[confirmation_key] = True
            if st.session_state.get(confirmation_key):
                st.warning("Confirma o envio dos dois vídeos como Não listados ao canal autorizado?")
                yes, no = st.columns(2)
                if yes.button("Confirmar publicação", key=f"confirm-publish-{item['render_job_id']}", type="primary"):
                    try:
                        queue_publication(item["render_job_id"], thumbnail_path)
                        st.session_state.pop(confirmation_key, None)
                        st.rerun()
                    except RuntimeError as exc:
                        st.error(str(exc))
                if no.button("Cancelar", key=f"cancel-publish-{item['render_job_id']}"):
                    st.session_state.pop(confirmation_key, None)
                    st.rerun()

    publications = list_publications()
    if publications:
        st.subheader("Fila e histórico")
    for item in publications:
        with st.expander(f"{item['niche']} · {item['status']}", expanded=item["status"] != "concluida"):
            st.markdown(f"**{item['title']}**")
            vertical_col, horizontal_col, thumb_col = st.columns(3)
            vertical_col.progress(item["vertical_progress"], text=f"Short · {item['vertical_status']}")
            horizontal_col.progress(item["horizontal_progress"], text=f"Horizontal · {item['horizontal_status']}")
            thumb_col.write(f"**Thumbnail:** {item['thumbnail_status']}")
            if item.get("vertical_url"):
                vertical_col.link_button("Abrir Short", item["vertical_url"], use_container_width=True)
            if item.get("horizontal_url"):
                horizontal_col.link_button("Abrir horizontal", item["horizontal_url"], use_container_width=True)
            for label, error in (("Short", item.get("vertical_error")),
                                 ("Horizontal", item.get("horizontal_error")),
                                 ("Thumbnail", item.get("thumbnail_error"))):
                if error:
                    st.error(f"{label}: {error}")
            if item["status"] in ("erro", "parcial"):
                if st.button("Tentar novamente", key=f"retry-youtube-{item['id']}"):
                    try:
                        retry_publication(item["id"])
                        st.rerun()
                    except RuntimeError as exc:
                        st.error(str(exc))
