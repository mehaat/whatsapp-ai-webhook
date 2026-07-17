"""
knowledge/rag.py
-----------------
The retrieval-augmented (RAG) knowledge base for ME-HAAT Fashion AI Bot v10.0.

This module implements a compact, dependency-light retriever:

    * Documents are split into ~600-character chunks (preferring paragraph and
      sentence boundaries) and persisted as :class:`~database.models.KnowledgeDoc`
      rows with their :class:`~database.models.KnowledgeChunk` children. Every
      chunk caches a ``term -> count`` map so retrieval never re-tokenizes.
    * :func:`search` ranks chunks with cosine-normalized TF-IDF (the vector math
      is done with NumPy) and works fully offline — there is no external
      embedding call.
    * :func:`answer` retrieves the best chunks and composes a reply. When a
      Gemini API key is configured it grounds Gemini on the retrieved context
      (guarded: any failure falls back); otherwise it returns a concise answer
      built directly from the top chunk.

Every public function is guarded — a failure returns a safe empty/default value
rather than raising, so the retriever can never crash an agent turn, a webhook,
or the admin dashboard.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional

import numpy as np

from config import config
from database.db import session_scope
from database.models import KnowledgeChunk, KnowledgeDoc
from utils.logging import logger

# --------------------------------------------------------------------------
# Tokenization
# --------------------------------------------------------------------------

#: A small English stopword list. Kept intentionally short — TF-IDF's IDF term
#: already down-weights ubiquitous words, so this only trims the most common
#: function words that add noise to short queries.
STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our",
    "so", "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "to", "up", "us", "was", "we", "were", "what", "when",
    "which", "who", "will", "with", "within", "would", "you", "your",
})

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_CHUNK_TARGET_CHARS = 600


def _tokens(text: str) -> List[str]:
    """Tokenize free text into normalized search terms.

    The text is lowercased and split on any run of non-alphanumeric characters;
    stopwords and tokens shorter than two characters are dropped.

    Args:
        text: Arbitrary input text (may be empty or ``None``-like).

    Returns:
        A list of lowercase term strings (order preserved, duplicates kept).
    """
    if not text:
        return []
    out: List[str] = []
    for tok in _TOKEN_SPLIT_RE.split(text.lower()):
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        out.append(tok)
    return out


def _term_counts(text: str) -> Dict[str, int]:
    """Return a ``term -> count`` map for a chunk of text."""
    counts: Dict[str, int] = {}
    for tok in _tokens(text):
        counts[tok] = counts.get(tok, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def _split_long_paragraph(paragraph: str, target: int) -> List[str]:
    """Split a paragraph that exceeds ``target`` chars on sentence boundaries.

    Sentences longer than ``target`` are hard-split at the character level so a
    chunk never grows unbounded.
    """
    chunks: List[str] = []
    buf = ""
    for sentence in _SENTENCE_SPLIT_RE.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > target:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(sentence), target):
                chunks.append(sentence[i:i + target])
            continue
        if buf and len(buf) + len(sentence) + 1 > target:
            chunks.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}" if buf else sentence
    if buf:
        chunks.append(buf)
    return chunks


def _chunk_text(text: str, target: int = _CHUNK_TARGET_CHARS) -> List[str]:
    """Split ``text`` into ~``target``-character chunks on natural boundaries.

    Paragraphs (blank-line separated) are packed together until adding the next
    one would exceed ``target``; an over-long paragraph is further split on
    sentence boundaries. This keeps semantically related text in one chunk.

    Args:
        text: The full document text.
        target: The soft maximum chunk size in characters.

    Returns:
        A list of non-empty chunk strings (empty when ``text`` is blank).
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > target:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_split_long_paragraph(para, target))
            continue
        if buf and len(buf) + len(para) + 2 > target:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of a document's text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

def ingest_document(
    title: str,
    text: str,
    *,
    source: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Ingest a document into the knowledge base.

    The text is chunked (~600 chars, on paragraph/sentence boundaries), each
    chunk's ``term -> count`` map is cached as JSON, and a
    :class:`~database.models.KnowledgeDoc` plus its
    :class:`~database.models.KnowledgeChunk` rows are persisted. Ingestion is
    idempotent by content hash: re-ingesting identical text for the same tenant
    replaces the previous copy (its chunks are rebuilt).

    Args:
        title: Human-facing document title.
        text: The full document body.
        source: Optional provenance string (filename, URL, "pasted", ...).
        tenant_id: Optional tenant scope (``None`` = default tenant).

    Returns:
        ``{"doc_id": int|None, "title": str, "chunks": int}``. On failure a
        best-effort dict with ``chunks == 0`` (and ``doc_id`` ``None``).
    """
    title = (title or "Untitled").strip()[:512]
    try:
        chunks = _chunk_text(text)
        digest = _content_hash(text)

        with session_scope() as db:
            # Idempotency: drop any existing doc with the same content hash for
            # this tenant, then rebuild it fresh.
            existing = (
                db.query(KnowledgeDoc)
                .filter(
                    KnowledgeDoc.content_hash == digest,
                    KnowledgeDoc.tenant_id == tenant_id,
                )
                .all()
            )
            for old in existing:
                db.query(KnowledgeChunk).filter(
                    KnowledgeChunk.doc_id == old.id
                ).delete(synchronize_session=False)
                db.delete(old)
            db.flush()

            doc = KnowledgeDoc(
                tenant_id=tenant_id,
                title=title,
                source=(source or None),
                content_hash=digest,
                chunk_count=len(chunks),
            )
            db.add(doc)
            db.flush()  # populate doc.id

            for index, chunk in enumerate(chunks):
                db.add(KnowledgeChunk(
                    doc_id=doc.id,
                    tenant_id=tenant_id,
                    chunk_index=index,
                    text=chunk,
                    terms=json.dumps(_term_counts(chunk)),
                ))

            doc_id = doc.id

        logger.info("RAG | ingested doc #%s '%s' (%d chunks)", doc_id, title, len(chunks))
        return {"doc_id": doc_id, "title": title, "chunks": len(chunks)}
    except Exception as exc:  # noqa: BLE001 - ingestion must never raise
        logger.error("RAG | ingest failed for '%s': %s", title, exc)
        return {"doc_id": None, "title": title, "chunks": 0}


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def _load_chunks(tenant_id: Optional[int]) -> List[Dict[str, Any]]:
    """Load every stored chunk (for a tenant) with its decoded term map."""
    rows: List[Dict[str, Any]] = []
    with session_scope() as db:
        titles = {
            d.id: d.title
            for d in db.query(KnowledgeDoc).filter(
                KnowledgeDoc.tenant_id == tenant_id
            ).all()
        }
        chunks = db.query(KnowledgeChunk).filter(
            KnowledgeChunk.tenant_id == tenant_id
        ).all()
        for chunk in chunks:
            try:
                terms = json.loads(chunk.terms) if chunk.terms else {}
            except (ValueError, TypeError):
                terms = {}
            rows.append({
                "doc_id": chunk.doc_id,
                "doc_title": titles.get(chunk.doc_id, ""),
                "text": chunk.text or "",
                "terms": {str(k): int(v) for k, v in terms.items()},
            })
    return rows


def search(
    query: str,
    *,
    top_k: Optional[int] = None,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Retrieve the most relevant chunks for a query via TF-IDF.

    Document frequency is computed across all stored chunks; each chunk is
    scored by the cosine similarity between its TF-IDF vector and the query's
    IDF vector (both computed with NumPy). Chunks are returned ranked by
    descending score.

    Args:
        query: The natural-language search query.
        top_k: Maximum number of chunks to return. Defaults to
            ``config.rag_top_k``.
        tenant_id: Optional tenant scope (``None`` = default tenant).

    Returns:
        A ranked list of ``{"doc_id", "doc_title", "text", "score"}`` dicts.
        Empty when the query has no usable terms or the knowledge base is empty.
    """
    if top_k is None:
        top_k = getattr(config, "rag_top_k", 4)
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 4

    query_terms = _tokens(query)
    if not query_terms:
        return []

    try:
        chunks = _load_chunks(tenant_id)
        if not chunks:
            return []

        n_docs = len(chunks)
        # Document frequency per term across all chunks.
        doc_freq: Dict[str, int] = {}
        for chunk in chunks:
            for term in chunk["terms"]:
                doc_freq[term] = doc_freq.get(term, 0) + 1

        # Smoothed inverse document frequency.
        idf: Dict[str, float] = {
            term: math.log((n_docs + 1) / (df + 1)) + 1.0
            for term, df in doc_freq.items()
        }

        # Unique query terms that actually appear in the corpus.
        q_unique = [t for t in dict.fromkeys(query_terms) if t in idf]
        if not q_unique:
            return []

        q_vec = np.array([idf[t] for t in q_unique], dtype=np.float64)
        q_norm = float(np.linalg.norm(q_vec))
        if q_norm == 0.0:
            return []

        scored: List[Dict[str, Any]] = []
        for chunk in chunks:
            terms = chunk["terms"]
            # Full chunk-vector norm (over all of the chunk's terms).
            chunk_weights = np.array(
                [count * idf.get(term, 0.0) for term, count in terms.items()],
                dtype=np.float64,
            )
            chunk_norm = float(np.linalg.norm(chunk_weights))
            if chunk_norm == 0.0:
                continue
            # Dot product restricted to the query terms.
            c_vec = np.array(
                [terms.get(t, 0) * idf[t] for t in q_unique], dtype=np.float64
            )
            dot = float(np.dot(c_vec, q_vec))
            if dot <= 0.0:
                continue
            score = dot / (chunk_norm * q_norm)
            scored.append({
                "doc_id": chunk["doc_id"],
                "doc_title": chunk["doc_title"],
                "text": chunk["text"],
                "score": round(score, 6),
            })

        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]
    except Exception as exc:  # noqa: BLE001 - retrieval must never raise
        logger.error("RAG | search failed for %r: %s", query, exc)
        return []


# --------------------------------------------------------------------------
# Answer composition
# --------------------------------------------------------------------------

_NO_ANSWER = "I don't have that in our knowledge base yet."


def _compose_offline_answer(query: str, hits: List[Dict[str, Any]]) -> str:
    """Build a concise answer directly from the top retrieved chunk."""
    top = hits[0]["text"].strip()
    # Prefer the first couple of sentences for brevity; cap the length.
    sentences = _SENTENCE_SPLIT_RE.split(top)
    snippet = " ".join(sentences[:3]).strip() if sentences else top
    if len(snippet) > 500:
        snippet = snippet[:500].rsplit(" ", 1)[0].strip() + "…"
    return snippet or top[:500]


def _compose_gemini_answer(query: str, hits: List[Dict[str, Any]]) -> Optional[str]:
    """Ground Gemini on the retrieved chunks. Returns ``None`` on any failure."""
    try:
        from ai.gemini import (
            ERROR_MESSAGE,
            FALLBACK_MESSAGE,
            QUOTA_MESSAGE,
            generate_reply,
        )

        context = "\n\n---\n\n".join(
            f"[{h['doc_title']}]\n{h['text']}" for h in hits
        )
        reply = generate_reply(
            history=[],
            customer_name="",
            language="en",
            verified_context=context,
            user_message=query,
        )
        reply = (reply or "").strip()
        if not reply or reply in {ERROR_MESSAGE, FALLBACK_MESSAGE, QUOTA_MESSAGE}:
            return None
        return reply
    except Exception as exc:  # noqa: BLE001 - Gemini is optional; fall back
        logger.warning("RAG | Gemini composition failed: %s", exc)
        return None


def answer(query: str, *, tenant_id: Optional[int] = None) -> Dict[str, Any]:
    """Answer a question from the knowledge base.

    Retrieves the top chunks; if none are found, returns a fixed "not in the
    knowledge base" reply. Otherwise composes an answer: when a Gemini API key
    is configured the answer is grounded on the retrieved chunks (guarded, with
    a fall back to the offline composer on any failure); with no key the answer
    is built directly from the best chunk.

    Args:
        query: The natural-language question.
        tenant_id: Optional tenant scope (``None`` = default tenant).

    Returns:
        ``{"answer": str, "sources": [{"doc_title": str, "score": float}, ...]}``.
    """
    try:
        hits = search(query, tenant_id=tenant_id)
        if not hits:
            return {"answer": _NO_ANSWER, "sources": []}

        text: Optional[str] = None
        if getattr(config, "gemini_api_key", ""):
            text = _compose_gemini_answer(query, hits)
        if not text:
            text = _compose_offline_answer(query, hits)

        sources = [
            {"doc_title": h["doc_title"], "score": h["score"]} for h in hits
        ]
        return {"answer": text, "sources": sources}
    except Exception as exc:  # noqa: BLE001 - answering must never raise
        logger.error("RAG | answer failed for %r: %s", query, exc)
        return {"answer": _NO_ANSWER, "sources": []}


# --------------------------------------------------------------------------
# Management / introspection
# --------------------------------------------------------------------------

def list_docs(*, tenant_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """List ingested documents (newest first) for a tenant."""
    try:
        with session_scope() as db:
            docs = (
                db.query(KnowledgeDoc)
                .filter(KnowledgeDoc.tenant_id == tenant_id)
                .order_by(KnowledgeDoc.id.desc())
                .all()
            )
            return [{
                "id": d.id,
                "title": d.title,
                "source": d.source,
                "chunk_count": d.chunk_count,
                "content_hash": d.content_hash,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            } for d in docs]
    except Exception as exc:  # noqa: BLE001
        logger.error("RAG | list_docs failed: %s", exc)
        return []


def get_doc(doc_id: int, *, tenant_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return a single document (with its live chunk count), or ``None``."""
    try:
        with session_scope() as db:
            doc = db.get(KnowledgeDoc, doc_id)
            if doc is None or (tenant_id is not None and doc.tenant_id != tenant_id):
                return None
            chunks = (
                db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.doc_id == doc.id)
                .count()
            )
            return {
                "id": doc.id,
                "title": doc.title,
                "source": doc.source,
                "chunk_count": chunks,
                "content_hash": doc.content_hash,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.error("RAG | get_doc failed for #%s: %s", doc_id, exc)
        return None


def delete_doc(doc_id: int, *, tenant_id: Optional[int] = None) -> bool:
    """Delete a document and its chunks. Returns ``True`` if one was removed."""
    try:
        with session_scope() as db:
            doc = db.get(KnowledgeDoc, doc_id)
            if doc is None or (tenant_id is not None and doc.tenant_id != tenant_id):
                return False
            db.query(KnowledgeChunk).filter(
                KnowledgeChunk.doc_id == doc.id
            ).delete(synchronize_session=False)
            db.delete(doc)
        logger.info("RAG | deleted doc #%s", doc_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("RAG | delete_doc failed for #%s: %s", doc_id, exc)
        return False


def stats(*, tenant_id: Optional[int] = None) -> Dict[str, int]:
    """Return knowledge-base counts: ``{"docs": n, "chunks": m}``."""
    try:
        with session_scope() as db:
            docs = (
                db.query(KnowledgeDoc)
                .filter(KnowledgeDoc.tenant_id == tenant_id)
                .count()
            )
            chunks = (
                db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.tenant_id == tenant_id)
                .count()
            )
            return {"docs": int(docs), "chunks": int(chunks)}
    except Exception as exc:  # noqa: BLE001
        logger.error("RAG | stats failed: %s", exc)
        return {"docs": 0, "chunks": 0}


# --------------------------------------------------------------------------
# Agent tool
# --------------------------------------------------------------------------

def knowledge_search_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    """Tool handler: answer ``args['query']`` from the knowledge base.

    Args:
        args: ``{"query": str, "tenant_id": int (optional)}``.

    Returns:
        The :func:`answer` dict (``{"answer", "sources"}``).
    """
    query = str((args or {}).get("query", "")).strip()
    tenant_id = (args or {}).get("tenant_id")
    return answer(query, tenant_id=tenant_id)


def register_tool() -> None:
    """Register the ``knowledge_search`` tool with the agent tool registry.

    Called by the application at startup. Idempotent.
    """
    from agents.tools import register

    register(
        "knowledge_search",
        "Answer a question from the store's knowledge base "
        "(policies, FAQs, product info).",
        knowledge_search_tool,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question to answer from the knowledge base.",
                },
            },
            "required": ["query"],
        },
        risk="low",
        category="support",
    )
    logger.info("RAG | registered 'knowledge_search' tool")
