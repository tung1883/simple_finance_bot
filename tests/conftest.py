"""Pytest fixtures.

Every test starts with a fresh in-memory sqlite DB. Modules call db.connect() lazily,
so swapping the connection here propagates everywhere.
"""
import os
import sys
from pathlib import Path

import pytest

# Make the project root importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure proxy/main imports don't fail on missing env at collect time.
os.environ.setdefault("TOKEN", "test-token")
os.environ.setdefault("PROXY_URL", "http://example.invalid/proxy")
os.environ.setdefault("BOT_PROACTIVE_ENABLED", "0")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    import db

    db.reset_for_tests(":memory:")
    yield
    # Connection is cleaned up by the next test's reset_for_tests call.
