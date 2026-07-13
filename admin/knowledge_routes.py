"""
admin/knowledge_routes.py
--------------------------
Admin "Knowledge Base" area for the v10.0 RAG retriever: a page that lists every
ingested document with base-wide stats, an add-document form (paste text or
upload a ``.txt`` / ``.md`` file), a delete action, and a live test-search box
backed by a small JSON endpoint.

This is an additive blueprint mounted at ``/admin/knowledge``. Every route
requires :func:`admin.security.login_required`; the state-changing routes
(add / delete) additionally require the ``manager`` role
(:func:`admin.rbac.role_required`) and pass :func:`admin.security.csrf_protect`.
It shares the existing admin session, CSRF token and template layout
(``admin/base.html``) and never mutates the core ``/admin`` blueprint.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from admin.rbac import role_required
from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from knowledge import rag
from utils.logging import logger

admin_knowledge_bp = Blueprint(
    "admin_knowledge",
    __name__,
    url_prefix="/admin/knowledge",
    template_folder="templates",
)

# Accepted upload extensions for document ingestion.
_ALLOWED_EXTENSIONS = (".txt", ".md")
# Hard cap on ingested text size (defensive; ~1 MB of characters).
_MAX_TEXT_CHARS = 1_000_000


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_knowledge_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "app_version": _app_version(),
        "nav_active": request.endpoint,
    }


def _app_version() -> str:
    try:
        from config import config

        return config.version
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# List + dashboard
# --------------------------------------------------------------------------

@admin_knowledge_bp.route("/", methods=["GET"])
@login_required
def knowledge_home() -> Any:
    """Knowledge-base home: document list, stats, add form, and a test box."""
    docs = rag.list_docs()
    stats = rag.stats()
    return render_template(
        "admin/knowledge.html",
        docs=docs,
        stats=stats,
    )


# --------------------------------------------------------------------------
# Add document (paste text OR upload a .txt / .md file)
# --------------------------------------------------------------------------

@admin_knowledge_bp.route("/add", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def knowledge_add() -> Any:
    """Ingest a pasted document or an uploaded ``.txt`` / ``.md`` file."""
    home_url = url_for("admin_knowledge.knowledge_home")
    title = clean_query(request.form.get("title", ""), 512)
    text = request.form.get("text", "") or ""
    source = "pasted"

    upload = request.files.get("file")
    if upload is not None and (upload.filename or "").strip():
        filename = upload.filename.strip()
        if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
            flash("Only .txt or .md files can be uploaded.", "error")
            return redirect(home_url)
        try:
            raw = upload.read() or b""
            text = raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - never 500 on a bad upload
            logger.error("ADMIN | knowledge upload read failed: %s", exc)
            flash("Could not read the uploaded file.", "error")
            return redirect(home_url)
        source = filename
        if not title:
            title = filename.rsplit(".", 1)[0][:512]

    text = text.strip()
    if not text:
        flash("Please paste some text or upload a file.", "error")
        return redirect(home_url)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    if not title:
        title = "Untitled document"

    try:
        result = rag.ingest_document(title, text, source=source)
        if result.get("chunks"):
            flash(
                f"Ingested '{result['title']}' "
                f"({result['chunks']} chunk(s)).", "success",
            )
        else:
            flash("Nothing was ingested (empty or unparseable text).", "warning")
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on an action
        logger.error("ADMIN | knowledge ingest failed: %s", exc)
        flash(f"Ingestion failed: {exc}", "error")

    return redirect(home_url)


# --------------------------------------------------------------------------
# Delete document
# --------------------------------------------------------------------------

@admin_knowledge_bp.route("/<int:doc_id>/delete", methods=["POST"])
@login_required
@role_required("manager")
@csrf_protect
def knowledge_delete(doc_id: int) -> Any:
    """Delete a document (and its chunks) from the knowledge base."""
    home_url = url_for("admin_knowledge.knowledge_home")
    try:
        if rag.delete_doc(doc_id):
            flash(f"Deleted document #{doc_id}.", "success")
        else:
            flash("Document not found.", "error")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | knowledge delete failed for #%s: %s", doc_id, exc)
        flash(f"Delete failed: {exc}", "error")
    return redirect(home_url)


# --------------------------------------------------------------------------
# Test search (JSON, for the live test box)
# --------------------------------------------------------------------------

@admin_knowledge_bp.route("/search", methods=["GET"])
@login_required
def knowledge_search() -> Any:
    """Return JSON search results for the admin test box (``?q=...``)."""
    query = clean_query(request.args.get("q", ""), 240)
    if not query:
        return jsonify({"query": "", "results": []})
    results = rag.search(query)
    return jsonify({"query": query, "results": results})
