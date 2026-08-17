"""Verify the real Tencent (腾讯) quote source against the live API.

Usage (from the repository root with the project venv):

    .\\\\.venv\\\\Scripts\\\\python.exe -m scripts.verify_quote [symbol] [market]

Examples:
    .\\\\.venv\\\\Scripts\\\\python.exe -m scripts.verify_quote 000001 CN
    .\\\\.venv\\\\Scripts\\\\python.exe -m scripts.verify_quote 0700.HK HK
    .\\\\.venv\\\\Scripts\\\\python.exe -m scripts.verify_quote AAPL US

Exits non-zero when the live feed returns no usable quote.
"""

from __future__ import annotations

import asyncio
import sys

from backend.quote_source import QuoteSourceError, TencentQuoteSource


async def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "000001"
    market = sys.argv[2] if len(sys.argv) > 2 else "CN"
    source = TencentQuoteSource()
    try:
        quote = await source.fetch(symbol, market)
    except QuoteSourceError as exc:
        print(f"FAILED: {exc}")
        return 1
    if quote is None:
        print(f"FAILED: no quote returned for {symbol} ({market})")
        return 1
    print(f"quote {quote['symbol']} ({quote.get('name')}):")
    for key in ("price", "change", "change_pct", "open", "high", "low", "prev_close",
                "volume", "turnover", "pe", "pb", "total_market_cap", "time"):
        print(f"  {key}: {quote.get(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
