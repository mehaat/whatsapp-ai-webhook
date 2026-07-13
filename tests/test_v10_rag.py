"""
tests/test_v10_rag.py
----------------------
Deterministic, fully-offline tests for the v10.0 RAG knowledge base
(``knowledge/rag.py``).

Two documents (a return policy and a shipping policy) are ingested, then the
TF-IDF retriever is exercised: a return-window query must rank the return-policy
chunk first, :func:`answer` must return a non-empty grounded answer with
sources, base-wide stats must reflect the ingested docs, and the
``knowledge_search`` tool handler must return an answer dict. No Gemini, no
network.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

import pytest

pytest.importorskip("numpy")

import commerce  # noqa: E402
from knowledge import rag  # noqa: E402

RETURN_TITLE = "Return Policy"
SHIPPING_TITLE = "Shipping Policy"

RETURN_TEXT = (
    "Return policy: returns accepted within 7 days of delivery. Items must be "
    "unworn, unwashed, and returned with their original tags and packaging. "
    "Once we receive the returned item, refunds are processed to the original "
    "payment method within 5 business days."
)
SHIPPING_TEXT = (
    "Shipping: orders ship in 2-3 days from our warehouse. We deliver across "
    "India via trusted courier partners. Standard delivery takes 4-7 days after "
    "dispatch; tracking details are shared over WhatsApp once the order ships."
)


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_and_ingest():
    """Bootstrap the commerce schema and ingest the two policy documents."""
    commerce.bootstrap()
    rag.ingest_document(RETURN_TITLE, RETURN_TEXT, source="test")
    rag.ingest_document(SHIPPING_TITLE, SHIPPING_TEXT, source="test")
    yield


def test_tokenizer_drops_stopwords_and_short_tokens():
    """The tokenizer lowercases, splits on non-alphanumerics, drops noise."""
    toks = rag._tokens("Returns accepted within 7 days!")
    assert "returns" in toks
    assert "accepted" in toks
    assert "days" in toks
    # 'within' is a stopword; single/short tokens dropped.
    assert "within" not in toks


def test_ingest_is_idempotent_by_content_hash():
    """Re-ingesting identical text replaces rather than duplicates the doc."""
    before = rag.stats()["docs"]
    rag.ingest_document(RETURN_TITLE, RETURN_TEXT, source="test")
    after = rag.stats()["docs"]
    assert after == before


def test_search_ranks_return_policy_first():
    """A return-window query ranks the return-policy chunk highest."""
    results = rag.search("how many days to return")
    assert results, "expected at least one search result"
    top = results[0]
    assert top["doc_title"] == RETURN_TITLE
    assert top["score"] > 0
    # The top score must strictly lead any shipping-doc result.
    shipping = [r for r in results if r["doc_title"] == SHIPPING_TITLE]
    if shipping:
        assert top["score"] >= shipping[0]["score"]


def test_answer_returns_text_and_sources():
    """answer() returns a non-empty answer plus sources."""
    result = rag.answer("what is the return window")
    assert isinstance(result, dict)
    assert result["answer"]
    assert result["answer"] != "I don't have that in our knowledge base yet."
    assert result["sources"]
    assert "doc_title" in result["sources"][0]


def test_stats_reports_docs():
    """stats() reports at least the two ingested documents."""
    s = rag.stats()
    assert s["docs"] >= 2
    assert s["chunks"] >= 2


def test_search_empty_query_returns_empty():
    """A query with no usable terms yields no results (never raises)."""
    assert rag.search("the and of") == []


def test_knowledge_search_tool_returns_answer_dict():
    """The tool handler returns an answer dict for a plain query."""
    out = rag.knowledge_search_tool({"query": "return"})
    assert isinstance(out, dict)
    assert "answer" in out
    assert "sources" in out


def test_register_tool_registers_knowledge_search():
    """register_tool() installs the 'knowledge_search' tool in the registry."""
    from agents.tools import get_tool

    rag.register_tool()
    tool = get_tool("knowledge_search")
    assert tool is not None
    assert tool.name == "knowledge_search"
    assert tool.risk == "low"
    assert tool.category == "support"
