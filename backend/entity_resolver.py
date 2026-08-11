from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from backend.schemas import EntityResolution, SecurityCandidate


_SYMBOL = re.compile(
    r"(?i)(?<![A-Z0-9])(?:\d{6}\.(?:SH|SZ)|\d{4,5}\.HK|[A-Z]{1,5}\.(?:US|NASDAQ|NYSE))(?![A-Z0-9])"
)
_CASE_REFERENCE = re.compile(r"(该公司|这家公司|当前公司|它|其|该股|这只股票)")
_SHORT_ALIAS_TAIL = re.compile(
    r"^(的|近|公司|集团|股份|财务|财报|盈利|利润|收入|营收|现金流|负债|估值|"
    r"股价|市场|分红|风险|业绩|业务|供应链)"
)


def _normalized(value: str) -> str:
    return re.sub(r"[\s·・,，。()（）]", "", value).upper()


class EntityResolver:
    def __init__(self, catalog_path: str | Path | None = None):
        path = Path(catalog_path) if catalog_path else Path(__file__).with_name("securities.json")
        rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        self._rows = rows

    @staticmethod
    def _candidate(row: dict[str, Any], alias: str, confidence: float) -> SecurityCandidate:
        market = str(row["market"]).upper()
        symbol = str(row["symbol"]).upper()
        return SecurityCandidate(
            candidate_id=f"{market}:{symbol}",
            company=str(row["company"]),
            symbol=symbol,
            market=market,
            confidence=confidence,
            matched_alias=alias,
        )

    def resolve(
        self,
        message: str,
        *,
        current_company: str | None = None,
        current_symbol: str | None = None,
        current_market: str | None = None,
    ) -> EntityResolution:
        text = message.strip()
        symbol_match = _SYMBOL.search(text)
        if symbol_match:
            target = _normalized(symbol_match.group(0))
            matches = [row for row in self._rows if _normalized(str(row["symbol"])) == target]
            if len(matches) == 1:
                selected = self._candidate(matches[0], symbol_match.group(0), 1.0)
                return EntityResolution(
                    status="resolved", query=symbol_match.group(0), candidates=[selected],
                    selected=selected, reason_codes=["EXACT_SYMBOL"],
                )

        if current_company and _CASE_REFERENCE.search(text):
            if current_symbol and current_market:
                row = {
                    "company": current_company,
                    "symbol": current_symbol,
                    "market": current_market,
                }
                selected = self._candidate(row, current_company, 0.99)
                return EntityResolution(
                    status="resolved", query=current_company, candidates=[selected],
                    selected=selected, reason_codes=["CURRENT_CASE_REFERENCE"],
                )

        normalized_text = _normalized(text)
        alias_matches: list[tuple[int, str, dict[str, Any]]] = []
        for row in self._rows:
            for alias in row.get("aliases", []):
                normalized_alias = _normalized(str(alias))
                if normalized_alias and normalized_alias in normalized_text:
                    position = normalized_text.find(normalized_alias)
                    remainder = normalized_text[position + len(normalized_alias):]
                    if len(normalized_alias) <= 2 and remainder and not _SHORT_ALIAS_TAIL.search(remainder):
                        continue
                    alias_matches.append((len(normalized_alias), str(alias), row))
        if alias_matches:
            longest = max(item[0] for item in alias_matches)
            best = [item for item in alias_matches if item[0] == longest]
            unique: dict[str, SecurityCandidate] = {}
            for _length, alias, row in best:
                candidate = self._candidate(row, alias, 0.98 if len(best) == 1 else 0.92)
                unique[candidate.candidate_id] = candidate
            candidates = sorted(unique.values(), key=lambda item: item.candidate_id)
            if len(candidates) == 1:
                return EntityResolution(
                    status="resolved", query=best[0][1], candidates=candidates,
                    selected=candidates[0], reason_codes=["UNIQUE_ALIAS"],
                )
            return EntityResolution(
                status="ambiguous", query=best[0][1], candidates=candidates,
                reason_codes=["MULTIPLE_LISTINGS", "USER_CONFIRMATION_REQUIRED"],
            )

        if current_company and _normalized(current_company) in normalized_text:
            if current_symbol and current_market:
                selected = self._candidate(
                    {"company": current_company, "symbol": current_symbol, "market": current_market},
                    current_company,
                    0.99,
                )
                return EntityResolution(
                    status="resolved", query=current_company, candidates=[selected],
                    selected=selected, reason_codes=["CURRENT_CASE_COMPANY"],
                )

        query = text[:240] or "unknown"
        return EntityResolution(
            status="unresolved", query=query, candidates=[], selected=None,
            reason_codes=["NO_CATALOG_MATCH", "CLARIFICATION_REQUIRED"],
        )
