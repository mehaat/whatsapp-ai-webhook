"""
knowledge
---------
The v10.0 retrieval-augmented (RAG) knowledge base for ME-HAAT Fashion AI Bot.

A small, fully-offline TF-IDF retriever over admin-ingested documents (store
policies, FAQs, product information). Documents are chunked, each chunk caches a
term->count map, and :func:`knowledge.rag.search` scores chunks with cosine-
normalized TF-IDF. An optional, guarded Gemini step composes a natural-language
answer grounded on the retrieved chunks; with no API key the retriever answers
directly from the best chunk. The retriever exposes a ``knowledge_search`` tool
for the multi-agent system.

Nothing here reaches the network by default, and no function raises to callers.
"""

from __future__ import annotations

__all__ = ["rag"]
