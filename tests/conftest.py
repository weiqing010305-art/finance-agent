"""Project-wide pytest configuration.

The live Tencent quote feed AND the cninfo filings API are stubbed for every
test: research-chain tests (``create_app`` -> executor -> registry) otherwise
issue a real network request per run, which makes the whole suite
network-dependent. Tools that import the real classes directly (e.g.
``test_quote_source``, ``test_filings_source``) keep them — only runtime
lookups inside handlers are replaced.
"""

from __future__ import annotations

import pytest

from backend import filings_source, quote_source


class OfflineQuoteSource:
    """Stand-in for the live Tencent feed that never touches the network."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, symbol: str, market: str = "") -> dict | None:
        return None


class OfflineFilingsSource:
    """Stand-in for the live cninfo API: returns no announcements, which
    routes ``search_filings`` down its explicit web-search fallback."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def search(
        self, *, company=None, symbol=None, document_types=None, max_results=20
    ) -> list:
        return []


@pytest.fixture(autouse=True)
def offline_quote_source(monkeypatch):
    monkeypatch.setattr(quote_source, "TencentQuoteSource", OfflineQuoteSource)


@pytest.fixture(autouse=True)
def offline_filings_source(monkeypatch):
    monkeypatch.setattr(filings_source, "CninfoFilingsSource", OfflineFilingsSource)
