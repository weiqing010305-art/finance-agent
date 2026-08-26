"""AkShare-backed stock price source for A-share / HK / US symbols.

The controlled-tools pipeline needs a real market-price path in addition to
the Eastmoney financial statements. AkShare is free and tokenless, and its
``stock_zh_a_daily`` / ``stock_hk_daily`` / ``stock_us_daily`` functions
work against Sina / Tencent endpoints that are reachable from this machine
(the Eastmoney kline endpoints are blocked by the local proxy, so we avoid
the ``*_em`` variants that depend on them).

Every call is synchronous inside AkShare, so the async tool handler wraps it
in ``asyncio.to_thread`` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from backend.redaction import redact_text


class AkShareSourceError(RuntimeError):
    pass


# Source URL used for evidence / citation purposes (stable public pages).
EVIDENCE_URLS = {
    "CN": "https://finance.sina.com.cn/realstock/company/{prefix}{symbol}/nc.shtml",
    "HK": "https://stock.finance.sina.com.cn/hkstock/quotes/{symbol}.html",
    "US": "https://stock.finance.sina.com.cn/usstock/quotes/{symbol}.html",
}

_MARKET_PREFIX = {
    "CN": {"SH": "sh", "SZ": "sz", "BJ": "bj"},
    "HK": "",
    "US": "",
}


def _normalise_symbol(symbol: str, market: str) -> str:
    """Return the bare symbol used by the AkShare call (e.g. 600519, 00700, AAPL)."""
    base = (symbol or "").strip().upper()
    # Strip an exchange suffix if already present ("600519.SH" -> "600519").
    if "." in base:
        base = base.split(".", 1)[0]
    return base


def _security_prefix(symbol: str, market: str) -> str:
    """Return the exchange prefix for A-share Sina codes (sh600519 / sz000001 / bj)."""
    if market != "CN":
        return ""
    code = _normalise_symbol(symbol, "CN")
    if code.startswith(("60", "68", "9")):
        return _MARKET_PREFIX["CN"]["SH"]
    if code.startswith(("00", "30", "20")):
        return _MARKET_PREFIX["CN"]["SZ"]
    if code.startswith(("4", "8")):
        return _MARKET_PREFIX["CN"]["BJ"]
    return _MARKET_PREFIX["CN"]["SH"]


def _evidence_url(symbol: str, market: str) -> str:
    if market == "CN":
        prefix = _security_prefix(symbol, "CN")
        return EVIDENCE_URLS["CN"].format(prefix=prefix, symbol=_normalise_symbol(symbol, "CN"))
    return EVIDENCE_URLS[market].format(symbol=_normalise_symbol(symbol, market))


class AkShareQuoteSource:
    """Fetches daily OHLCV bars through AkShare (Sina / Tencent backends)."""

    def __init__(
        self,
        *,
        market: str = "CN",
        periods: int = 30,
        caller: Callable[..., list[dict[str, Any]]] | None = None,
    ) -> None:
        self.market = market.upper()
        self.periods = periods
        self._caller = caller or self._default_caller

    def _default_caller(self, symbol: str) -> list[dict[str, Any]]:
        import akshare as ak

        if self.market == "CN":
            prefix = _security_prefix(symbol, "CN")
            frame = ak.stock_zh_a_daily(
                symbol=f"{prefix}{_normalise_symbol(symbol, 'CN')}",
                adjust="qfq",
            )
        elif self.market == "HK":
            frame = ak.stock_hk_daily(symbol=_normalise_symbol(symbol, "HK"), adjust="qfq")
        elif self.market == "US":
            frame = ak.stock_us_daily(symbol=_normalise_symbol(symbol, "US"), adjust="qfq")
        else:
            raise AkShareSourceError(f"unsupported market for AkShare: {self.market}")

        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            date = row.get("date")
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
            rows.append({
                "date": date_str,
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
                "volume": float(row.get("volume") or 0),
            })
        return rows

    async def fetch(self, symbol: str) -> list[dict[str, Any]]:
        """Return the most recent ``periods`` daily bars, newest first."""
        rows = await asyncio.to_thread(self._caller, symbol)
        if not rows:
            return []
        rows = sorted(rows, key=lambda r: r["date"])[-self.periods:]
        return list(reversed(rows))


class AkShareFinancialStatements:
    """Fetches HK / US headline financials through AkShare (Eastmoney-backed)."""

    def __init__(
        self,
        *,
        market: str = "HK",
        periods: int = 4,
        caller: Callable[..., list[dict[str, Any]]] | None = None,
    ) -> None:
        self.market = market.upper()
        self.periods = periods
        self._caller = caller or self._default_caller

    def _default_caller(self, symbol: str) -> list[dict[str, Any]]:
        import akshare as ak

        if self.market == "HK":
            frame = ak.stock_financial_hk_analysis_indicator_em(symbol=_normalise_symbol(symbol, "HK"))
        elif self.market == "US":
            frame = ak.stock_financial_us_analysis_indicator_em(symbol=_normalise_symbol(symbol, "US"))
        else:
            raise AkShareSourceError(f"unsupported market for AkShare financials: {self.market}")

        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            rows.append({k: v for k, v in row.items()})
        return rows

    async def fetch(self, symbol: str) -> list[dict[str, Any]]:
        """Return the most recent ``periods`` statement rows, newest first."""
        rows = await asyncio.to_thread(self._caller, symbol)
        if not rows:
            return []
        # Sort by REPORT_DATE descending when available.
        def _date_key(row: dict[str, Any]) -> str:
            return str(row.get("REPORT_DATE") or row.get("report_date") or "")
        rows = sorted(rows, key=_date_key, reverse=True)
        return rows[: self.periods]

    @staticmethod
    def to_statement_rows(rows: list[dict[str, Any]], market: str) -> list[dict[str, Any]]:
        """Convert AkShare raw records to the StatementRow shape used by the
        Eastmoney path, so downstream calculate_financial_metrics works."""
        # Field map: AkShare HK/US indicator column -> canonical metric.
        if market == "HK":
            field_map = {
                "OPERATE_INCOME": ("营业收入", "revenue"),
                "GROSS_PROFIT": ("毛利", "gross_profit"),
                "HOLDER_PROFIT": ("归属母公司净利润", "net_profit"),
                "BASIC_EPS": ("基本每股收益", "eps_basic"),
                "BPS": ("每股净资产", "book_value_per_share"),
                "ROE_AVG": ("ROE（平均）", "roe_weighted"),
                "GROSS_PROFIT_RATIO": ("销售毛利率", "gross_margin_pct"),
                "NET_PROFIT_RATIO": ("销售净利率", "net_margin_pct"),
                "DEBT_ASSET_RATIO": ("资产负债率", "debt_to_asset_pct"),
                "CURRENT_RATIO": ("流动比率", "current_ratio"),
                "PER_NETCASH_OPERATE": ("每股经营现金流", "operating_cash_per_share"),
            }
            currency = "CNY"
        else:  # US
            field_map = {
                "OPERATE_INCOME": ("营业收入", "revenue"),
                "GROSS_PROFIT": ("毛利", "gross_profit"),
                "PARENT_HOLDER_NETPROFIT": ("归属母公司净利润", "net_profit"),
                "BASIC_EPS": ("基本每股收益", "eps_basic"),
                "BPS": ("每股净资产", "book_value_per_share"),
                "ROE_AVG": ("ROE（平均）", "roe_weighted"),
                "GROSS_PROFIT_RATIO": ("销售毛利率", "gross_margin_pct"),
                "NET_PROFIT_RATIO": ("销售净利率", "net_margin_pct"),
                "DEBT_ASSET_RATIO": ("资产负债率", "debt_to_asset_pct"),
                "CURRENT_RATIO": ("流动比率", "current_ratio"),
            }
            currency = "USD"

        statements: list[dict[str, Any]] = []
        for row in rows:
            period = _parse_report_date(row.get("REPORT_DATE") or row.get("report_date"))
            if not period:
                continue
            metrics: dict[str, Any] = {}
            for column, (label, key) in field_map.items():
                value = _to_number(row.get(column))
                if value is None:
                    continue
                metrics[key] = {
                    "label": label,
                    "value": value,
                    "unit": currency,
                    "source_field": column,
                    "report_period": period,
                }
            statements.append({
                "period": period,
                "currency": currency,
                "unit": "raw",
                "report_type": "AkShare financial summary",
                "report_year": int(period[:4]) if len(period) >= 4 and period[:4].isdigit() else None,
                "metrics": metrics,
            })
        return statements


def _parse_report_date(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return raw.split(" ")[0]


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(",", "")
    if not cleaned or cleaned in {"-", "--", "None", "nan"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


async def fetch_financials_via_akshare(
    payload: Any,
    context: Any = None,
    *,
    _source: AkShareFinancialStatements | None = None,
) -> dict[str, Any]:
    """Tool handler: fetch HK / US headline financials through AkShare.

    Returns the same shape as
    :func:`backend.financial_statements.fetch_financial_statements` so the
    controlled-tools processor treats it identically.
    """
    market = (getattr(payload, "market", "") or "").strip().upper()
    symbol = (getattr(payload, "symbol", "") or "").strip()
    periods = int(getattr(payload, "periods", 4) or 4)
    if market not in {"HK", "US"}:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": f"market {market!r} not supported by AkShare financials",
            "fallback_used": None,
        }
    if not symbol:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": "symbol is required",
            "fallback_used": None,
        }
    source = _source or AkShareFinancialStatements(market=market, periods=periods)
    try:
        rows = await source.fetch(symbol)
    except Exception as exc:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": f"AkShare financials failed: {type(exc).__name__}: {exc}"[:500],
            "fallback_used": "filings_search",
        }
    statements = AkShareFinancialStatements.to_statement_rows(rows, market)
    if not statements:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": f"AkShare returned no financial rows for {symbol}",
            "fallback_used": "filings_search",
        }
    evidence = []
    for row in statements:
        period = row.get("period") or ""
        evidence.append({
            "source_id": f"akshare-fin:{market}:{_normalise_symbol(symbol, market)}:{period}",
            "title": f"{symbol} 财务摘要 {period}",
            "url": _evidence_url(symbol, market),
            "publisher": "AkShare / Eastmoney",
        })
    return {
        "status": "ok",
        "data": statements,
        "evidence": evidence,
        "coverage": "hk" if market == "HK" else "us",
        "source_url": _evidence_url(symbol, market),
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }


async def fetch_stock_prices(
    payload: Any,
    context: Any = None,
    *,
    _source: AkShareQuoteSource | None = None,
) -> dict[str, Any]:
    """Tool handler: fetch recent daily price bars for a symbol.

    Returns ``status="ok"`` with ``data`` being a list of
    ``{date, open, high, low, close, volume}`` bars (newest first) plus a
    small ``quote`` summary (latest close, change vs previous bar, high/low
    over the window). On failure it degrades explicitly.
    """
    market = (getattr(payload, "market", "") or "").strip().upper()
    symbol = (getattr(payload, "symbol", "") or "").strip()
    periods = int(getattr(payload, "periods", 30) or 30)
    if market not in {"CN", "HK", "US"}:
        return {
            "status": "empty", "data": [], "quote": {},
            "degraded": True,
            "degraded_reason": f"market {market!r} is not supported by AkShare",
            "fallback_used": None,
        }
    if not symbol:
        return {
            "status": "empty", "data": [], "quote": {},
            "degraded": True,
            "degraded_reason": "symbol is required",
            "fallback_used": None,
        }

    source = _source or AkShareQuoteSource(market=market, periods=periods)
    try:
        bars = await source.fetch(symbol)
    except Exception as exc:
        return {
            "status": "empty", "data": [], "quote": {},
            "degraded": True,
            "degraded_reason": f"AkShare price fetch failed: {type(exc).__name__}: {exc}"[:500],
            "fallback_used": "get_quote",
        }
    if not bars:
        return {
            "status": "empty", "data": [], "quote": {},
            "degraded": True,
            "degraded_reason": f"AkShare returned no price bars for {symbol}",
            "fallback_used": "get_quote",
        }

    latest = bars[0]
    prev = bars[1] if len(bars) > 1 else None
    quote = {
        "latest_close": latest["close"],
        "latest_date": latest["date"],
        "change": (latest["close"] - prev["close"]) if prev else None,
        "change_pct": ((latest["close"] - prev["close"]) / prev["close"] * 100) if prev and prev["close"] else None,
        "window_high": max(bar["high"] for bar in bars),
        "window_low": min(bar["low"] for bar in bars),
        "bars": len(bars),
    }
    return {
        "status": "ok",
        "data": bars,
        "quote": quote,
        "evidence": [{
            "source_id": f"akshare:{market}:{_normalise_symbol(symbol, market)}",
            "title": f"{symbol} 日线行情（AkShare）",
            "url": _evidence_url(symbol, market),
            "publisher": "AkShare / Sina",
        }],
        "coverage": "akshare",
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }


def fetched_at() -> str:
    return datetime.now(timezone.utc).isoformat()