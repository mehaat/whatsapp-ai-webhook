"""
tests/conftest.py
------------------
Test-suite configuration. Runs before any test module is imported (pytest loads
conftest first), so environment set here is picked up by ``config`` when it is
first imported.

We disable the background job workers during tests: the queue mechanics are
exercised deterministically via ``commerce.jobs.process_next`` / ``run_async``,
and auto-started worker threads (spawned when ``app`` is imported) would
otherwise race those tests for the same in-process queue.
"""

import os

# Force synchronous job execution + no worker threads during the test run.
os.environ["JOBS_ENABLED"] = "false"

# v10.1: pin the unified database to a local working-directory file for tests so
# the canonical resolver never picks up a stale /var/data/mehaat.db (which only
# exists inside Render-style containers). Deterministic + easy to clean.
os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")
