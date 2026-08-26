"""Tushare Pro financial-statements source for HK / US symbols.

The controlled-tools pipeline now has a real HK / US data path on top of
the public Tushare Pro REST API. The project owns the integration but
**does not** ship a token; users must set ``TUSHARE_TOKEN`` in their
environment (or the project ``backend/.env``) to enable it. Without a
token the source returns ``None`` from :py:meth:`from_env`, and
``fetch_financial_statements`` degrades to the existing search_filings
fallback path (no fabricated numbers).

We deliberately do **not** depend on the ``tushare`` Python package: as of
1.4.29 it ships a hard-coded legacy endpoint (``api.waditu.com``) that has
been decommissioned for years, so any ``pro.stock_hk_income(...)`` call
fails with "请指定正确的接口名". Calling the real ``https://api.tushare.pro``
endpoint over ``httpx`` works around the stale client.

Reference: https://tushare.pro/document/2 — HK endpoints (``stock_hk_*``)
and US endpoints (``us_*``) require ``pro`` tier or above; the free tier
shares the same interfaces with a shared daily quota (~200 calls / day).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.redaction import redact_text

TUSHARE_PRO_URL = "https://api.tushare.pro"
TUSHARE_ENV_TOKEN = "TUSHARE_TOKEN"
TUSHARE_TIMEOUT = 15.0


class TushareFinancialStatementsError(RuntimeError):
    pass


# Tushare column -> (Chinese label, canonical key, unit, scale)
HK_INCOME_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "TOTAL_REVENUE": ("营业总收入", "revenue", "CNY", 1.0),
    "REVENUE": ("营业收入", "operating_revenue", "CNY", 1.0),
    "OPERATE_PROFIT": ("营业利润", "operating_profit", "CNY", 1.0),
    "TOTAL_PROFIT": ("利润总额", "total_profit", "CNY", 1.0),
    "INCOME_TAX": ("所得税", "income_tax", "CNY", 1.0),
    "NETPROFIT": ("净利润", "net_profit", "CNY", 1.0),
    "PARENT_NETPROFIT": ("归属母公司净利润", "net_profit", "CNY", 1.0),
    "BASIC_EPS": ("基本每股收益", "eps_basic", "CNY", 1.0),
    "DEDUCTED_BASIC_EPS": ("扣非每股收益", "eps_deducted", "CNY", 1.0),
}

HK_BALANCE_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "TOTAL_ASSETS": ("资产总计", "total_assets", "CNY", 1.0),
    "TOTAL_LIAB": ("负债合计", "total_liabilities", "CNY", 1.0),
    "TOTAL_EQUITY": ("股东权益合计", "total_equity", "CNY", 1.0),
    "TOTAL_SHARE": ("股本", "shares_outstanding", "shares", 1.0),
    "BPS": ("每股净资产", "book_value_per_share", "CNY", 1.0),
}

HK_CASHFLOW_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "NETCASH_OPERATE": ("经营活动现金流净额", "operating_cash_flow", "CNY", 1.0),
    "NETCASH_INVEST": ("投资活动现金流净额", "investing_cash_flow", "CNY", 1.0),
    "NETCASH_FINANCE": ("筹资活动现金流净额", "financing_cash_flow", "CNY", 1.0),
}

US_INCOME_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "TOTAL_REVENUE": ("Total Revenue", "revenue", "USD", 1.0),
    "OPERATE_INCOME": ("Operating Income", "operating_profit", "USD", 1.0),
    "INCOME_TAX": ("Income Tax", "income_tax", "USD", 1.0),
    "NETPROFIT": ("Net Profit", "net_profit", "USD", 1.0),
    "PARENT_NETPROFIT": ("Net Profit Attributable to Parent", "net_profit", "USD", 1.0),
    "BASIC_EPS": ("Basic EPS", "eps_basic", "USD", 1.0),
    "DEDUCTED_BASIC_EPS": ("Diluted EPS", "eps_deducted", "USD", 1.0),
}

US_BALANCE_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "TOTAL_ASSETS": ("Total Assets", "total_assets", "USD", 1.0),
    "TOTAL_LIAB": ("Total Liabilities", "total_liabilities", "USD", 1.0),
    "TOTAL_EQUITY": ("Total Equity", "total_equity", "USD", 1.0),
    "TOTAL_SHARE": ("Total Shares", "shares_outstanding", "shares", 1.0),
    "BPS": ("Book Value per Share", "book_value_per_share", "USD", 1.0),
}

US_CASHFLOW_FIELDS: dict[str, tuple[str, str, str, float]] = {
    "NETCASH_OPERATE": ("Operating Cash Flow", "operating_cash_flow", "USD", 1.0),
    "NETCASH_INVEST": ("Investing Cash Flow", "investing_cash_flow", "USD", 1.0),
    "NETCASH_FINANCE": ("Financing Cash Flow", "financing_cash_flow", "USD", 1.0),
}


FIELD_MAPS: dict[tuple[str, str], dict[str, tuple[str, str, str, float]]] = {
    ("HK", "income"): HK_INCOME_FIELDS,
    ("HK", "balance"): HK_BALANCE_FIELDS,
    ("HK", "cashflow"): HK_CASHFLOW_FIELDS,
    ("US", "income"): US_INCOME_FIELDS,
    ("US", "balance"): US_BALANCE_FIELDS,
    ("US", "cashflow"): US_CASHFLOW_FIELDS,
}

# Map a (market, statement_kind) to the Tushare Pro api_name.
API_NAMES: dict[tuple[str, str], str] = {
    ("HK", "income"): "stock_hk_income",
    ("HK", "balance"): "stock_hk_balance",
    ("HK", "cashflow"): "stock_hk_cashflow",
    ("US", "income"): "us_income",
    ("US", "balance"): "us_balance",
    ("US", "cashflow"): "us_cashflow",
}

DEFAULT_CURRENCY: dict[str, str] = {"HK": "CNY", "US": "USD"}


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


def _parse_period(end_date: Any) -> str | None:
    if end_date is None:
        return None
    raw = str(end_date).strip()
    if not raw:
        return None
    raw = raw.replace("-", "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw.split(" ")[0] or None


class TushareFinancialStatements:
    """Async-friendly wrapper around Tushare Pro for HK / US data."""

    def __init__(
        self,
        token: str,
        *,
        market: str = "HK",
        base_url: str = TUSHARE_PRO_URL,
        timeout: float = TUSHARE_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise TushareFinancialStatementsError("Tushare token is empty")
        self.token = token
        self.market = market.upper()
        self.base_url = base_url
        self.timeout = timeout
        self._client = client

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        market: str = "HK",
        base_url: str = TUSHARE_PRO_URL,
        timeout: float = TUSHARE_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> "TushareFinancialStatements | None":
        values = os.environ if env is None else env
        token = (values.get(TUSHARE_ENV_TOKEN) or "").strip()
        if not token:
            return None
        return cls(
            token=token, market=market, base_url=base_url,
            timeout=timeout, client=client,
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, api_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        import asyncio

        client = self._get_client()
        body = {
            "api_name": api_name,
            "token": self.token,
            "params": {**params, "limit": max(1, int(params.get("limit", 4)))},
            "fields": "",
        }
        try:
            response = await client.post(self.base_url, json=body)
        except httpx.HTTPError as exc:
            raise TushareFinancialStatementsError(
                f"Tushare HTTP request failed: {exc}"
            ) from exc
        if response.status_code != 200:
            raise TushareFinancialStatementsError(
                f"Tushare returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TushareFinancialStatementsError(
                f"Tushare returned non-JSON body: {exc}"
            ) from exc
        if payload.get("code") != 0:
            raise TushareFinancialStatementsError(
                f"Tushare error: {payload.get('msg') or 'unknown'}"
            )
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(fields, item)) for item in items]

    async def fetch(
        self,
        ts_code: str,
        *,
        periods: int = 4,
    ) -> list[dict[str, Any]]:
        """Fetch the most recent ``periods`` rows of HK / US income / balance /
        cashflow statements. Each returned dict matches the StatementRow
        shape used by the Eastmoney adapter so the downstream controlled-
        tools processor treats both sources identically."""
        market = self.market
        if (market, "income") not in API_NAMES:
            raise TushareFinancialStatementsError(
                f"unsupported market for Tushare adapter: {market}"
            )
        import asyncio
        params = {"ts_code": ts_code, "limit": max(1, int(periods))}
        income_rows, balance_rows, cashflow_rows = await asyncio.gather(
            self._post(API_NAMES[(market, "income")], params),
            self._post(API_NAMES[(market, "balance")], params),
            self._post(API_NAMES[(market, "cashflow")], params),
        )
        return self._merge_rows(income_rows, balance_rows, cashflow_rows)

    def _sync_fetch(self, api_name: str, ts_code: str, periods: int) -> list[dict[str, Any]]:
        # Run in a worker thread (no event loop available). We need a
        # synchronous HTTP client for that thread.
        with httpx.Client(timeout=self.timeout) as sync_client:
            return self._sync_post(sync_client, api_name, {"ts_code": ts_code, "limit": periods})

    def _sync_post(self, client: httpx.Client, api_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        body = {
            "api_name": api_name,
            "token": self.token,
            "params": {**params, "limit": max(1, int(params.get("limit", 4)))},
            "fields": "",
        }
        try:
            response = client.post(self.base_url, json=body)
        except httpx.HTTPError as exc:
            raise TushareFinancialStatementsError(
                f"Tushare HTTP request failed: {exc}"
            ) from exc
        if response.status_code != 200:
            raise TushareFinancialStatementsError(
                f"Tushare returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TushareFinancialStatementsError(
                f"Tushare returned non-JSON body: {exc}"
            ) from exc
        if payload.get("code") != 0:
            raise TushareFinancialStatementsError(
                f"Tushare error: {payload.get('msg') or 'unknown'}"
            )
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(fields, item)) for item in items]

    def _merge_rows(
        self,
        income_rows: list[dict[str, Any]],
        balance_rows: list[dict[str, Any]],
        cashflow_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        income_map = FIELD_MAPS[(self.market, "income")]
        balance_map = FIELD_MAPS[(self.market, "balance")]
        cashflow_map = FIELD_MAPS[(self.market, "cashflow")]

        def _key(row: dict[str, Any]) -> str:
            period = _parse_period(row.get("END_DATE") or row.get("end_date"))
            return period or ""

        grouped: dict[str, dict[str, Any]] = {}
        for row in income_rows:
            period = _key(row)
            entry = grouped.setdefault(period, {"period": period})
            self._fill_metrics(entry, row, income_map)
        for row in balance_rows:
            period = _key(row)
            entry = grouped.setdefault(period, {"period": period})
            self._fill_metrics(entry, row, balance_map)
        for row in cashflow_rows:
            period = _key(row)
            entry = grouped.setdefault(period, {"period": period})
            self._fill_metrics(entry, row, cashflow_map)

        return sorted(
            (self._row_to_statement(period, metrics) for period, metrics in grouped.items() if period),
            key=lambda r: r["period"],
            reverse=True,
        )

    @staticmethod
    def _fill_metrics(
        entry: dict[str, Any],
        row: dict[str, Any],
        field_map: dict[str, tuple[str, str, str, float]],
    ) -> None:
        for upstream, (label, key, unit, scale) in field_map.items():
            value = _to_number(row.get(upstream) or row.get(upstream.lower()))
            if value is None:
                continue
            entry.setdefault(key, {
                "label": label,
                "value": value * scale,
                "unit": unit,
                "source_field": upstream,
                "report_period": entry.get("period"),
            })

    @staticmethod
    def _row_to_statement(period: str, entry: dict[str, Any]) -> dict[str, Any]:
        currency = ""
        for metric in entry.values():
            if isinstance(metric, dict) and metric.get("unit") in {"CNY", "USD"}:
                currency = metric["unit"]
                break
        return {
            "period": period,
            "currency": currency,
            "unit": "raw",
            "report_type": "Tushare financial summary",
            "report_year": int(period[:4]) if len(period) >= 4 and period[:4].isdigit() else None,
            "metrics": entry,
        }


# ----- tool handler entry point -------------------------------------------


async def fetch_via_tushare(
    payload: Any,
    context: Any = None,
    *,
    _client: TushareFinancialStatements | None = None,
) -> dict[str, Any]:
    """Tool handler: fetch HK / US financial metrics from Tushare Pro.

    Returns the same shape as
    :func:`backend.financial_statements.fetch_financial_statements` so the
    controlled-tools processor treats it identically.
    """
    market = (getattr(payload, "market", "") or "").strip().upper()
    symbol = (getattr(payload, "symbol", "") or "").strip().upper()
    periods = int(getattr(payload, "periods", 4) or 4)

    if market not in {"HK", "US"}:
        raise TushareFinancialStatementsError(
            f"Tushare adapter only supports HK/US; got {market!r}"
        )
    if not symbol:
        raise TushareFinancialStatementsError("symbol is required")

    ts_code = _to_tushare_symbol(symbol, market)
    if not ts_code:
        raise TushareFinancialStatementsError(
            f"cannot map {symbol}.{market} to a Tushare ts_code"
        )

    client = _client or TushareFinancialStatements.from_env(market=market)
    if client is None:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": (
                "TUSHARE_TOKEN is not configured; HK/US fundamentals "
                "are unavailable without it"
            ),
            "fallback_used": "filings_search",
        }
    try:
        rows = await client.fetch(ts_code, periods=periods)
    finally:
        if _client is None:
            await client.aclose()
    if not rows:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": (
                f"Tushare returned no rows for {ts_code}; the symbol may be "
                "delisted or have no recent filings"
            ),
            "fallback_used": "filings_search",
        }
    return {
        "status": "ok",
        "data": rows,
        "evidence": [],
        "coverage": "hk" if market == "HK" else "us",
        "source_url": f"https://api.tushare.pro (token: configured)",
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }


def _to_tushare_symbol(symbol: str, market: str) -> str | None:
    """Coerce ``"0700"`` + ``"HK"`` -> ``"00700.HK"`` etc."""
    base = symbol.strip().upper()
    if not base:
        return None
    if "." in base:
        return base
    suffix = "HK" if market == "HK" else "US"
    return f"{base}.{suffix}"


def fetched_at() -> str:
    return datetime.now(timezone.utc).isoformat()