
"""Real financial statement data source: Eastmoney (东方财富) public API.

The ``datacenter-web.eastmoney.com`` JSON endpoint exposes a Chinese A-share
company's headline financial metrics across quarterly / annual reports
without an API key. We use the ``RPT_F10_FINANCE_MAINFINADATA`` table
because it carries the broadest set of canonical Chinese accounting fields
(income / balance sheet / cash flow / leverage / liquidity / valuation)
in a single response, which downstream tools
(``calculate_financial_metrics``, the controlled-tools report) can cite
directly. Numbers come from code, not the model.

Coverage is currently A-share only. For HK / US symbols the source returns
an explicit ``degraded`` result so callers know to fall back to
``search_filings`` rather than hallucinate figures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from backend.redaction import redact_text


EASTMONEY_DATACENTER_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
)
EASTMONEY_REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
_PAGE_SIZE_DEFAULT = 20

# Canonical key -> (Chinese label, upstream Eastmoney column, natural unit,
# scale multiplier). ``scale`` re-scales fields that arrive as 0..1
# decimals (growth rates, etc.) so the tool always returns ``%`` for
# percentages.
FIELD_MAP: dict[str, tuple[str, str, str, float]] = {
    "TOTALOPERATEREVE":    ("营业总收入",            "revenue",                   "CNY",    1.0),
    "OPERATE_INCOME_PK":   ("营业收入",              "operating_revenue",         "CNY",    1.0),
    # "SALE_ER_PK" removed: Eastmoney returns it as a small decimal ratio,
    # not as raw CNY. Use the derived gross_margin_pct + gross_profit for
    # margin analysis; cost_of_revenue can be reconstructed if needed.
    "MLR":                 ("毛利",                  "gross_profit",              "CNY",    1.0),
    "PARENTNETPROFIT":     ("归属母公司净利润",       "net_profit",                "CNY",    1.0),
    "KCFJCXSYJLR":         ("扣非净利润",             "net_profit_deducted",       "CNY",    1.0),
    "OPERATE_PROFIT_PK":   ("营业利润",              "operating_profit",          "CNY",    1.0),
    "TOTAL_ASSETS_PK":     ("资产总计",              "total_assets",              "CNY",    1.0),
    "LIABILITY":           ("负债合计",              "total_liabilities",         "CNY",    1.0),
    "TOTAL_EQUITY_PK":     ("股东权益合计",           "total_equity",              "CNY",    1.0),
    "NETCASH_OPERATE_PK":  ("经营活动现金流量净额",    "operating_cash_flow",       "CNY",    1.0),
    "NETCASH_INVEST_PK":   ("投资活动现金流量净额",    "investing_cash_flow",       "CNY",    1.0),
    "NETCASH_FINANCE_PK":  ("筹资活动现金流量净额",    "financing_cash_flow",       "CNY",    1.0),
    "TOTAL_SHARE":         ("总股本",                "shares_outstanding",        "shares", 1.0),
    "EPSJB":               ("基本每股收益",            "eps_basic",                 "CNY",    1.0),
    "EPSKCJB":             ("扣非每股收益",            "eps_deducted",              "CNY",    1.0),
    "BPS":                 ("每股净资产",             "book_value_per_share",      "CNY",    1.0),
    "MGJYXJJE":            ("每股经营现金流",          "operating_cash_per_share",  "CNY",    1.0),
    "XSMLL":               ("销售毛利率",             "gross_margin_pct",          "%",      1.0),
    "ROEJQ":               ("ROE加权",               "roe_weighted",              "%",      1.0),
    "ROIC":                ("投入资本回报率",          "roic_pct",                  "%",      1.0),
    "XSJLL":               ("销售净利率",             "net_margin_pct",            "%",      1.0),
    "ZZCJLL":              ("总资产净利率",           "roa_pct",                   "%",      1.0),
    "ZCFZL":               ("资产负债率",             "debt_to_asset_pct",         "%",      1.0),
    "TOTALOPERATEREVETZ":  ("营业总收入同比",          "revenue_yoy_pct",           "%",      100.0),
    "PARENTNETPROFITTZ":   ("归母净利润同比",          "net_profit_yoy_pct",        "%",      100.0),
    "KCFJCXSYJLRTZ":       ("扣非净利润同比",          "net_profit_deducted_yoy_pct","%",     100.0),
    "TA_YOYRATIO_PK":      ("总资产同比",              "total_assets_yoy_pct",      "%",      1.0),
    "EQUITY_YOYRATIO_PK":  ("股东权益同比",            "equity_yoy_pct",            "%",      1.0),
    "OI_YOYRATIO_PK":      ("营业收入同比",            "operating_revenue_yoy_pct", "%",      1.0),
    "QYCS":                ("权益乘数",               "equity_multiplier",         "x",      1.0),
    "LD":                  ("流动比率",               "current_ratio",             "x",      1.0),
    "SD":                  ("速动比率",               "quick_ratio",               "x",      1.0),
    "XJLLB":               ("现金比率",               "cash_ratio",                "x",      1.0),
    "INTEREST_DEBT_RATIO": ("带息债务比率",            "interest_debt_ratio",       "%",      1.0),
    "NCO_NETPROFIT":       ("经营现金流/净利润",        "cash_to_net_profit",        "x",      1.0),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_A_SHARE_SECUCODES = {
    "CN", "A", "CN_A", "SSE", "SZSE",
    "SH", "SZ",
}


class FinancialStatementError(RuntimeError):
    pass


def _normalise_secucode(symbol: str, market: str) -> str | None:
    cleaned = (symbol or "").strip().upper()
    if not cleaned:
        return None
    m = (market or "").strip().upper()
    if "." in cleaned:
        return cleaned
    if m in _A_SHARE_SECUCODES or cleaned.isdigit() and len(cleaned) == 6:
        if cleaned.startswith(("60", "68", "90")) or m in {"SH", "SSE"}:
            return f"{cleaned}.SH"
        if cleaned.startswith(("00", "30", "20")) or m in {"SZ", "SZSE"}:
            return f"{cleaned}.SZ"
        if m == "CN":
            return f"{cleaned}.SH"
    return None


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(",", "")
    if not cleaned or cleaned in {"-", "--", "None"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(report_date: Any) -> str | None:
    if not report_date:
        return None
    raw = str(report_date).strip()
    if not raw:
        return None
    return raw.split(" ", 1)[0]


class EastmoneyFinancialStatements:
    def __init__(
        self,
        *,
        url: str = EASTMONEY_DATACENTER_URL,
        report_name: str = EASTMONEY_REPORT_NAME,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.url = url
        self.report_name = report_name
        self._client = client
        self._timeout = timeout_seconds

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        *,
        symbol: str,
        market: str = "",
        periods: int = 4,
    ) -> list[dict[str, Any]]:
        secucode = _normalise_secucode(symbol, market)
        if not secucode:
            raise FinancialStatementError(
                f"market {market or '<unknown>'} for symbol {symbol!r} "
                "is not currently supported by the Eastmoney public API"
            )
        client = await self._ensure_client()
        params = {
            "reportName": self.report_name,
            "columns": "ALL",
            "filter": f"(SECUCODE=\"{secucode}\")",
            "pageNumber": "1",
            "pageSize": str(max(1, min(periods, 60))),
        }
        try:
            response = await client.get(self.url, params=params)
        except httpx.HTTPError as exc:
            raise FinancialStatementError(f"Eastmoney request failed: {exc}") from exc
        if response.status_code != 200:
            raise FinancialStatementError(
                f"Eastmoney returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FinancialStatementError("Eastmoney returned non-JSON body") from exc
        if not payload.get("success", True):
            message = payload.get("message") or "unknown error"
            raise FinancialStatementError(f"Eastmoney rejected the request: {message}")
        rows = (payload.get("result") or {}).get("data") or []
        return [self._normalise_row(row) for row in rows]

    @staticmethod
    def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
        period = _parse_date(row.get("REPORT_DATE"))
        out: dict[str, Any] = {
            "period": period,
            "currency": str(row.get("CURRENCY") or "CNY"),
            "unit": "raw",
            "report_type": redact_text(str(row.get("REPORT_TYPE") or "")),
            "report_year": _to_number(row.get("REPORT_YEAR")),
            "notice_date": _parse_date(row.get("NOTICE_DATE")),
        }
        for upstream, (label, key, unit, scale) in FIELD_MAP.items():
            raw = _to_number(row.get(upstream))
            if raw is None:
                continue
            out[key] = {
                "label": label,
                "value": raw * scale,
                "unit": unit,
                "source_field": upstream,
                "report_period": period,
            }
        return out


def build_financial_statements_client(
    *, client: httpx.AsyncClient | None = None,
) -> EastmoneyFinancialStatements:
    return EastmoneyFinancialStatements(client=client)


def fetched_at() -> str:
    return datetime.now(timezone.utc).isoformat()


async def fetch_financial_statements(
    payload: Any,
    context: Any = None,
    *,
    _client: EastmoneyFinancialStatements | None = None,
) -> dict[str, Any]:
    """Tool handler: fetch headline financial metrics from Eastmoney.

    Returns ``status="ok"`` for supported A-share symbols (rich Chinese
    canonical fields, see :data:`FIELD_MAP`). For HK / US symbols or
    upstream failures the response carries an explicit ``degraded`` flag
    with a fallback hint so downstream steps can route to
    ``search_filings`` rather than hallucinate numbers.
    """
    periods = int(getattr(payload, "periods", 4) or 4)
    symbol = getattr(payload, "symbol", "") or ""
    market = getattr(payload, "market", "") or ""
    if not symbol:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": "symbol is required",
            "fallback_used": None,
        }
    secucode = _normalise_secucode(symbol, market)
    if not secucode:
        # A-share only via Eastmoney; HK / US route through Tushare Pro
        # when the operator has set TUSHARE_TOKEN. Without a token the
        # Tushare handler returns its own degraded response so the
        # controlled-tools plan degrades to search_filings.
        if market.strip().upper() in {"HK", "US"}:
            from backend.tushare_source import fetch_via_tushare
            return await fetch_via_tushare(payload, context)
        return {
            "status": "empty", "data": [], "evidence": [],
            "coverage": "unsupported",
            "degraded": True,
            "degraded_reason": (
                f"market {market or '<unknown>'} for symbol {symbol!r} is "
                "not covered by the Eastmoney public API"
            ),
            "fallback_used": "filings_search",
        }
    source = _client or EastmoneyFinancialStatements()
    try:
        rows = await source.fetch(symbol=symbol, market=market, periods=periods)
    except FinancialStatementError as exc:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": str(exc)[:500],
            "fallback_used": "filings_search",
        }
    if not rows:
        return {
            "status": "empty", "data": [], "evidence": [],
            "degraded": True,
            "degraded_reason": "Eastmoney returned no rows for the symbol",
            "fallback_used": "filings_search",
        }
    normalised = [_to_statement_row(row) for row in rows]
    evidence = [_evidence_for_row(row, secucode) for row in normalised if row.period]
    return {
        "status": "ok",
        "data": [row.model_dump() for row in normalised],
        "evidence": evidence,
        "coverage": "a_share",
        "source_url": (
            f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code={secucode.replace('.', '')}"
        ),
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }


def _to_statement_row(row: dict[str, Any]) -> "StatementRow":
    # Late import to avoid a hard cycle with backend.tool_registry.
    from backend.tool_registry import StatementRow, StatementMetric
    metrics: dict[str, StatementMetric] = {}
    for key, value in row.items():
        if key in {"period", "report_type", "notice_date", "currency", "unit", "report_year"}:
            continue
        if not isinstance(value, dict) or "value" not in value:
            continue
        metrics[key] = StatementMetric(
            label=value.get("label", key),
            value=float(value["value"]),
            unit=value.get("unit", "raw"),
            source_field=value.get("source_field", key),
            report_period=value.get("report_period"),
        )
    return StatementRow(
        period=row.get("period"),
        report_type=row.get("report_type"),
        notice_date=row.get("notice_date"),
        currency=row.get("currency", "CNY"),
        unit=row.get("unit", "raw"),
        metrics=metrics,
    )


def _evidence_for_row(row: "StatementRow", secucode: str) -> dict[str, Any]:
    period = row.period or ""
    return {
        "source_id": f"eastmoney:{secucode}:{period}",
        "title": f"{secucode} 财务摘要 {period}".strip(),
        "url": (
            f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code={secucode.replace('.', '')}"
        ),
        "publisher": "东方财富",
        "report_type": row.report_type or "",
    }
