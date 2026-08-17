"""Project-wide pytest configuration.

The live Tencent quote feed is stubbed out for every test: research-chain
tests (``create_app`` -> executor -> registry) otherwise issue a real quote
request per run, which makes the whole suite network-dependent. Tools that
import ``TencentQuoteSource`` directly (e.g. ``test_quote_source``) keep the
real class — only runtime lookups inside handlers are replaced.
"""

from __future__ import annotations

import pytest

from backend import quote_source


class OfflineQuoteSource:
    """Stand-in for the live Tencent feed that never touches the network."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch(self, symbol: str, market: str = "") -> dict | None:
        return None


@pytest.fixture(autouse=True)
def offline_quote_source(monkeypatch):
    monkeypatch.setattr(quote_source, "TencentQuoteSource", OfflineQuoteSource)
