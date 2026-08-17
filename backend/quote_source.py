"""Real market quote data source: Tencent (腾讯) free quote API.

``qt.gtimg.cn`` returns GBK-encoded, tilde-separated quote strings for A-share,
HK and US symbols without an API key. The tool parses them into deterministic
numeric fields (price, change, open/high/low, volume, PE/PB, market cap),
matching the product promise that market-analysis numbers come from
deterministic code — the model only picks the symbol and interprets the
result.

The endpoint is an unofficial public feed: treat it as best-effort and always
degrade explicitly on failure.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from backend.redaction import redact_text

QUOTE_URL = "https://qt.gtimg.cn/q="
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# Field index (0-based inside the tilde-separated payload) → quote key.
_FIELD_INDEX = {
    "name": 1,
    "code": 2,
    "price": 3,
    "prev_close": 4,
    "open": 5,
    "volume": 6,        # 手 (A-share) / shares (HK)
    "time": 30,
    "change": 31,
    "change_pct": 32,
    "high": 33,
    "low": 34,
    "turnover": 37,     # 万元 (A-share)
    "turnover_rate": 38,
    "pe": 39,
    "amplitude": 43,
    "float_market_cap": 44,   # 亿元
    "total_market_cap": 45,   # 亿元
    "pb": 46,
}
_MAX_FIELD = max(_FIELD_INDEX.values())


class QuoteSourceError(RuntimeError):
    pass


def _to_number(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned in {"-", "--", ""}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_time(value: str) -> str | None:
    """Normalise the two timestamp formats (A-share compact, HK slashed)."""
    cleaned = value.strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d{14}", cleaned):  # 20260817161403
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]} {cleaned[8:10]}:{cleaned[10:12]}:{cleaned[12:14]}"
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", cleaned):  # 2026/08/17 16:08:35
        return cleaned.replace("/", "-")
    return cleaned


def normalize_symbol(symbol: str, market: str = "") -> str | None:
    """Map a bare symbol + market to the Tencent symbol prefix (sz/sh/hk/us).

    ``0700.HK`` → ``hk00700``, ``000001`` (CN) → ``sz000001``,
    ``600519`` (CN) → ``sh600519``, ``AAPL`` (US) → ``usAAPL``. Symbols that
    already carry a recognised prefix pass through.
    """
    value = symbol.strip()
    if not value:
        return None
    lowered = value.lower()
    if re.match(r"^(sz|sh|hk|us|bj)\d", lowered) or lowered.startswith("us"):
        return lowered
    market_norm = (market or "").strip().upper()
    if "HK" in market_norm or ".HK" in value.upper():
        digits = re.sub(r"\D", "", value.split(".")[0])
        if digits:
            return f"hk{digits.zfill(5)}"
        return None
    if "US" in market_norm:
        return f"us{value.upper()}"
    if "SSE" in market_norm or "SH" in market_norm:
        return f"sh{re.sub(r'\D', '', value)}"
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if digits.startswith(("6", "9", "5")):  # 沪主板/科创/B股/基金
        return f"sh{digits}"
    return f"sz{digits}"  # 深主板/创业板（0/3 开头）及默认


class TencentQuoteSource:
    def __init__(
        self,
        *,
        quote_url: str = QUOTE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.quote_url = quote_url
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def fetch(self, symbol: str, market: str = "") -> dict[str, Any] | None:
        """Fetch one quote; returns a flat numeric dict or None when absent."""
        normalized = normalize_symbol(symbol, market)
        if normalized is None:
            raise QuoteSourceError(f"cannot map symbol {symbol!r} for market {market!r}")
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.get(self.quote_url + normalized, headers=_HEADERS)
        except httpx.HTTPError as exc:
            raise QuoteSourceError(f"quote request failed: {exc}") from exc
        if response.status_code >= 400:
            raise QuoteSourceError(
                f"quote request failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        try:
            text = response.content.decode("gbk", errors="replace")
        except UnicodeDecodeError as exc:
            raise QuoteSourceError("quote response is not GBK text") from exc
        match = re.search(r'="([^"]*)"', text)
        if match is None:
            raise QuoteSourceError("quote response is missing the payload")
        fields = match.group(1).split("~")
        if len(fields) <= _MAX_FIELD:
            raise QuoteSourceError("quote payload has too few fields")
        quote: dict[str, Any] = {"symbol": normalized}
        for key, index in _FIELD_INDEX.items():
            value = fields[index]
            if key == "name":
                quote[key] = redact_text(value.strip())[:200]
            elif key == "code":
                quote[key] = value.strip()[:32]
            elif key == "time":
                quote[key] = _parse_time(value)
            else:
                quote[key] = _to_number(value)
        quote["source"] = "tencent"
        return quote


async def get_quote(
    payload: Any,
    context: Any = None,
    *,
    _source: TencentQuoteSource | None = None,
) -> dict[str, Any]:
    """Tool handler: fetch one deterministic quote for a symbol.

    Never raises on a missing or failed feed: the result degrades to
    ``empty`` with an explicit reason, matching the tool contract.
    """
    from backend.tool_registry import QuoteItem

    source = _source if _source is not None else TencentQuoteSource()
    try:
        quote = await source.fetch(payload.symbol, payload.market)
    except QuoteSourceError as exc:
        return {
            "status": "empty",
            "data": [],
            "evidence": [],
            "degraded": True,
            "degraded_reason": f"quote source unavailable: {exc}"[:500],
            "fallback_used": None,
        }
    if quote is None:
        return {
            "status": "empty",
            "data": [],
            "evidence": [],
            "degraded": True,
            "degraded_reason": f"no quote returned for {payload.symbol}",
            "fallback_used": None,
        }
    item = QuoteItem(
        symbol=quote["symbol"],
        name=quote.get("name") or payload.symbol,
        price=quote.get("price"),
        change=quote.get("change"),
        change_pct=quote.get("change_pct"),
        open=quote.get("open"),
        high=quote.get("high"),
        low=quote.get("low"),
        prev_close=quote.get("prev_close"),
        volume=quote.get("volume"),
        turnover=quote.get("turnover"),
        turnover_rate=quote.get("turnover_rate"),
        pe=quote.get("pe"),
        pb=quote.get("pb"),
        total_market_cap=quote.get("total_market_cap"),
        time=quote.get("time"),
        source=quote.get("source") or "tencent",
    )
    return {
        "status": "ok",
        "data": [item.model_dump()],
        "evidence": [],
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }
