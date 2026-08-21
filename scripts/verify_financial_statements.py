"""Smoke-verify the Eastmoney public financial-statements endpoint.

Hits the same endpoint ``fetch_financial_statements`` uses in production
against a representative set of A-share tickers so CI / humans can confirm
the public dataset still answers without an API key and returns the
expected headline metrics (revenue, net profit, ROE, EPS).

Exit code is non-zero if every ticker fails — partial degradation (some
tickers work, some return empty) is acceptable and is reported per row.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from backend.financial_statements import (
    EastmoneyFinancialStatements,
    FinancialStatementError,
)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _probe(client: EastmoneyFinancialStatements, market: str, symbol: str) -> dict[str, Any]:
    try:
        rows = await client.fetch(symbol=symbol, market=market, periods=1)
    except FinancialStatementError as exc:
        return {"symbol": symbol, "market": market, "ok": False, "reason": str(exc)[:200], "metric_count": 0}
    if not rows:
        return {"symbol": symbol, "market": market, "ok": False, "reason": "no rows", "metric_count": 0}
    latest = rows[0]
    return {
        "symbol": symbol,
        "market": market,
        "ok": True,
        "period": latest.get("period"),
        "metric_count": len([k for k in latest if isinstance(latest[k], dict)]),
        "revenue_label": latest.get("revenue", {}).get("label"),
        "net_profit_value": latest.get("net_profit", {}).get("value"),
        "roe_weighted_value": latest.get("roe_weighted", {}).get("value"),
        "eps_basic_value": latest.get("eps_basic", {}).get("value"),
    }


DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("600519", "CN"),  # 贵州茅台
    ("000001", "CN"),  # 平安银行
    ("300750", "CN"),  # 宁德时代
    ("688981", "CN"),  # 中芯国际
    ("601318", "CN"),  # 中国平安
    ("000858", "CN"),  # 五粮液
]


async def _run(symbols: list[tuple[str, str]]) -> int:
    client = EastmoneyFinancialStatements()
    failed = 0
    try:
        for symbol, market in symbols:
            row = await _probe(client, market, symbol)
            if not row["ok"]:
                failed += 1
            print(
                f"{symbol:<10} {market:<6} ok={row['ok']!s:<5} "
                f"period={row.get('period')} metrics={row.get('metric_count')} "
                f"net_profit={row.get('net_profit_value')} "
                f"roe={row.get('roe_weighted_value')} "
                f"reason={row.get('reason') or '-'}"
            )
    finally:
        await client.aclose()
    print()
    if failed == len(symbols):
        print("ERROR: every target failed; the Eastmoney dataset is unreachable from this host.")
        return 1
    if failed:
        print(f"WARNING: {failed}/{len(symbols)} targets failed; investigate partial degradation.")
    else:
        print(f"OK: all {len(symbols)} targets returned headline financial metrics.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", default=[],
                        help="repeatable; format SYMBOL[:MARKET] (default market=CN)")
    args = parser.parse_args()
    if args.symbol:
        targets: list[tuple[str, str]] = []
        for raw in args.symbol:
            if ":" in raw:
                sym, mkt = raw.split(":", 1)
            else:
                sym, mkt = raw, "CN"
            targets.append((mkt.upper(), sym))
    else:
        targets = DEFAULT_TARGETS
    return asyncio.run(_run(targets))


if __name__ == "__main__":
    sys.exit(main())