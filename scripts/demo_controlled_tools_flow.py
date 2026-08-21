r"""Drive the controlled-tools tool chain end-to-end against a real A-share
target, **without** the full PostgreSQL / Dramatiq / MinIO stack.

The script replicates the first half of the ``controlled_tools`` plan from
``backend/formal_research_api.py``:

    fetch_financial_statements  (real Eastmoney dataset)
        |
        v
    calculate_financial_metrics (pure-Python formulas)

``extract_financial_facts`` exists for the ``search_filings`` (free-text)
path; the structured statements from Eastmoney already carry canonical
metric names, so we forward them straight into the metrics tool. The
result keeps the full citation trail because each fact carries its
``report_period``.

Each step is invoked through the production ``ToolRegistry`` so we
exercise the same schema / timeout / retry / evidence contract that the
durable worker uses in production.

Usage (from the repository root with the project venv):

    .venv\Scripts\python.exe -m scripts.demo_controlled_tools_flow
    .venv\Scripts\python.exe -m scripts.demo_controlled_tools_flow --symbol 600519
    .venv\Scripts\python.exe -m scripts.demo_controlled_tools_flow --symbol 0700 --market HK

Exit code is 0 when the run produced at least one statement row and at
least one computed metric; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys

# Windows terminals default to GBK on stdout; force UTF-8 so the demo prints
# Chinese labels from the Eastmoney dataset and the metrics calculator.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
else:  # pragma: no cover - older Pythons
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from typing import Any

from backend.tool_registry import (
    ToolInvocationContext,
    build_default_registry,
)


_TARGETS = [
    ("600519", "CN", "贵州茅台"),
    ("000858", "CN", "五粮液"),
    ("300750", "CN", "宁德时代"),
    ("601318", "CN", "中国平安"),
    ("000001", "CN", "平安银行"),
    ("0700",   "HK", "腾讯控股"),
    ("AAPL",   "US", "Apple"),
]


def _ctx(step_id):
    return ToolInvocationContext(
        run_id="demo",
        plan_version=1,
        step_id=step_id,
        idempotency_key=f"demo:{step_id}",
    )


def _line(label, value, width):
    return f"{label}={value!s:>{width}}"


async def _run(symbol, market, label):
    headline = "\n=== " + label + "  (" + symbol + "." + market + ") ==="
    print(headline)

    registry = build_default_registry()
    fetch_input = {"symbol": symbol, "market": market, "periods": 4}
    fetch_exec = await registry.execute(
        "fetch_financial_statements", fetch_input, context=_ctx("fetch_statements"),
    )
    fetch_out = fetch_exec.output
    print(
        "[fetch_financial_statements]",
        str(fetch_exec.duration_ms) + "ms",
        "status=" + str(fetch_out["status"]),
        "degraded=" + str(fetch_out["degraded"]),
        "coverage=" + str(fetch_out.get("coverage")),
    )
    if fetch_out.get("degraded_reason"):
        print("  reason:", fetch_out["degraded_reason"])
    if fetch_out.get("source_url"):
        print("  source_url:", fetch_out["source_url"])

    rows = fetch_out.get("data") or []
    if not rows:
        print("  no rows; downstream steps would degrade to filings search.")
        return 1
    print("  " + str(len(rows)) + " statement rows:")
    for row in rows:
        rev = row["metrics"].get("revenue", {}).get("value")
        np = row["metrics"].get("net_profit", {}).get("value")
        roe = row["metrics"].get("roe_weighted", {}).get("value")
        eps = row["metrics"].get("eps_basic", {}).get("value")
        print(
            "    period=" + str(row["period"])
            + "  report=" + str(row.get("report_type"))
            + "  revenue=" + str(rev)
            + "  net_profit=" + str(np)
            + "  ROE=" + str(roe) + "%"
            + "  EPS=" + str(eps),
        )

    facts = []
    FACT_KEY_MAP = {
        "revenue": "营收",
        "operating_revenue": "营收",
        "cost_of_revenue": "营业成本",
        "gross_profit": None,
        "net_profit": "净利润",
        "net_profit_deducted": "净利润",
        "operating_profit": "营业利润",
        "total_assets": "总资产",
        "total_liabilities": "总负债",
        "total_equity": "股东权益",
        "operating_cash_flow": "经营现金流",
        "investing_cash_flow": None,
        "financing_cash_flow": None,
        "shares_outstanding": "股本",
        "eps_basic": None,
        "book_value_per_share": None,
        "operating_cash_per_share": None,
    }
    for row in rows:
        period = row.get("period")
        if not period:
            continue
        for key, value in row.get("metrics", {}).items():
            v = value.get("value")
            if v is None:
                continue
            canonical = FACT_KEY_MAP.get(key)
            if canonical is None:
                continue
            facts.append({
                "name": canonical,
                "value": v,
                "period": period,
                "unit": value.get("unit", "raw"),
                "currency": row.get("currency", "CNY"),
            })
    print(
        "\n[handoff -> calculate_financial_metrics]  "
        + str(len(facts)) + " structured facts across " + str(len(rows)) + " periods"
    )

    metrics_input = {"metrics": ["growth", "margin", "roe"], "facts": facts}
    calc_exec = await registry.execute(
        "calculate_financial_metrics", metrics_input, context=_ctx("calculate_metrics"),
    )
    calc_out = calc_exec.output
    metrics_list = calc_out.get("data") if isinstance(calc_out.get("data"), list) else []
    print(
        "\n[calculate_financial_metrics]",
        str(calc_exec.duration_ms) + "ms",
        "status=" + str(calc_out.get("status")),
        "degraded=" + str(calc_out.get("degraded")),
        "computed=" + str(len(metrics_list)),
    )
    if metrics_list:
        for item in metrics_list:
            print(
                "    " + str(item.get("name")) + ": " + str(item.get("value")) + str(item.get("unit", ""))
                + "  [" + str(item.get("formula")) + "]",
            )
    else:
        print("    (empty result: " + str(calc_out.get("degraded_reason")) + ")")

    evidence = fetch_out.get("evidence") or []
    print("\n[evidence] " + str(len(evidence)) + " citation refs from Eastmoney:")
    for ev in evidence[:3]:
        print("    " + str(ev.get("source_id")) + "  " + str(ev.get("title")))
        print("      " + str(ev.get("url")))
    if len(evidence) > 3:
        print("    ... and " + str(len(evidence) - 3) + " more")

    produced_metrics = bool(metrics_list)
    return 0 if (rows and produced_metrics) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=None, help="ticker; defaults to the curated targets")
    parser.add_argument("--market", default=None, help="market; defaults to CN when --symbol is given")
    args = parser.parse_args()

    if args.symbol:
        targets = [(args.symbol, args.market or "CN", args.symbol)]
    else:
        targets = _TARGETS

    rc = 0
    for sym, mkt, label in targets:
        try:
            step_rc = asyncio.run(_run(sym, mkt, label))
        except Exception as exc:
            print("  EXCEPTION:", exc)
            step_rc = 1
        rc = rc or step_rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
