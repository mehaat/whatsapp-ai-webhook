"""
memory/store.py
----------------
In-memory conversation store for ME-HAAT Fashion AI Bot v3.0.

Keeps the last N messages per WhatsApp number and automatically expires
conversations that have been inactive beyond a configurable timeout.

NOTE: This is process-local memory. For a multi-worker Gunicorn deployment
or a restart-safe deployment, replace this with Redis or a database-backed
store (the public interface below is intentionally small so that swap is
straightforward).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, List

from utils.logging import logger

_MAX_TURNS_PER_USER = 10
_DEFAULT_TIMEOUT_SECONDS = 60 * 60  # 1 hour of inactivity clears the conversation


class ConversationMemory:
    """Thread-safe, in-memory conversation history keyed by WhatsApp number."""

    def __init__(
        self,
        max_turns: int = _MAX_TURNS_PER_USER,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.max_turns = max_turns
        self.timeout_seconds = timeout_seconds
        self._store: Dict[str, Deque[Dict[str, str]]] = {}
        self._last_seen: Dict[str, float] = {}
        self._names: Dict[str, str] = {}
        # v4.0: per-customer product-search pagination state. Each entry is
        # {"query": str, "products": List[dict], "offset": int}.
        self._searches: Dict[str, Dict[str, object]] = {}
        self._lock = threading.Lock()

    def _expire_if_needed(self, wa_number: str) -> None:
        """Clear history for wa_number if it has been inactive too long."""
        last_seen = self._last_seen.get(wa_number)
        if last_seen is None:
            return
        if time.time() - last_seen > self.timeout_seconds:
            logger.info("MEMORY | Conversation expired for %s due to inactivity", wa_number)
            self._store.pop(wa_number, None)
            self._names.pop(wa_number, None)
            self._searches.pop(wa_number, None)

    def add_turn(self, wa_number: str, role: str, text: str) -> None:
        """Append a conversation turn for a given WhatsApp number."""
        with self._lock:
            self._expire_if_needed(wa_number)
            if wa_number not in self._store:
                self._store[wa_number] = deque(maxlen=self.max_turns)
            self._store[wa_number].append({"role": role, "text": text})
            self._last_seen[wa_number] = time.time()

    def get_history(self, wa_number: str) -> List[Dict[str, str]]:
        """Return the stored conversation history for a WhatsApp number."""
        with self._lock:
            self._expire_if_needed(wa_number)
            return list(self._store.get(wa_number, deque()))

    def set_customer_name(self, wa_number: str, name: str) -> None:
        """Cache the customer's WhatsApp profile name."""
        if not name:
            return
        with self._lock:
            self._names[wa_number] = name

    def get_customer_name(self, wa_number: str) -> str:
        """Retrieve a cached customer name, if known."""
        with self._lock:
            return self._names.get(wa_number, "")

    # ----------------------------------------------------------------------
    # Product-search pagination (v4.0, Task 10)
    # ----------------------------------------------------------------------

    def set_last_search(
        self, wa_number: str, query: str, products: List[Dict[str, object]]
    ) -> None:
        """Record the full ranked result set for a customer's product search.

        The offset is reset to ``len(products already shown)`` by the caller via
        ``get_next_search_page``; here we simply store the whole list at offset 0.
        """
        with self._lock:
            self._searches[wa_number] = {
                "query": query,
                "products": list(products),
                "offset": 0,
            }
            self._last_seen[wa_number] = time.time()

    def get_next_search_page(
        self, wa_number: str, page_size: int = 5
    ) -> List[Dict[str, object]]:
        """Return the next page of products for the customer and advance state.

        Returns an empty list when there is no active search or no more
        products remain.
        """
        with self._lock:
            self._expire_if_needed(wa_number)
            state = self._searches.get(wa_number)
            if not state:
                return []
            products = state["products"]  # type: ignore[index]
            offset = int(state["offset"])  # type: ignore[arg-type]
            page = products[offset : offset + page_size]
            state["offset"] = offset + len(page)
            self._last_seen[wa_number] = time.time()
            return list(page)

    def has_active_search(self, wa_number: str) -> bool:
        """Return True if the customer has a stored search with items remaining."""
        with self._lock:
            state = self._searches.get(wa_number)
            if not state:
                return False
            return int(state["offset"]) < len(state["products"])  # type: ignore[arg-type]

    def clear_search(self, wa_number: str) -> None:
        """Forget any stored product-search pagination state for a customer."""
        with self._lock:
            self._searches.pop(wa_number, None)

    def cleanup_expired(self) -> int:
        """Sweep all conversations and remove expired ones.

        Returns:
            Number of conversations expired during this sweep.
        """
        expired = 0
        now = time.time()
        with self._lock:
            stale_numbers = [
                number
                for number, last_seen in self._last_seen.items()
                if now - last_seen > self.timeout_seconds
            ]
            for number in stale_numbers:
                self._store.pop(number, None)
                self._names.pop(number, None)
                self._last_seen.pop(number, None)
                self._searches.pop(number, None)
                expired += 1

        if expired:
            logger.info("MEMORY | Cleanup expired %d inactive conversation(s)", expired)
        return expired


# Module-level singleton used across the Flask app
conversation_memory = ConversationMemory()
