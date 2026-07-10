"""
utils/ratelimit.py
-------------------
Simple in-memory sliding-window rate limiter, keyed per WhatsApp number
(or any other string key, e.g. per-shop for Shopify calls).

Not distributed-safe; swap for Redis in a multi-worker production setup.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict


class RateLimiter:
    """A thread-safe, in-memory sliding-window rate limiter."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """Check whether a new request for ``key`` is allowed right now."""
        now = time.time()
        with self._lock:
            window = self._hits[key]
            while window and now - window[0] > self.window_seconds:
                window.popleft()

            if len(window) >= self.max_requests:
                return False

            window.append(now)
            return True

    def remaining(self, key: str) -> int:
        """Return how many requests are still allowed for ``key`` in this window."""
        now = time.time()
        with self._lock:
            window = self._hits[key]
            while window and now - window[0] > self.window_seconds:
                window.popleft()
            return max(0, self.max_requests - len(window))
