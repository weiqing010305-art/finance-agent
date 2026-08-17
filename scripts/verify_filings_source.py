"""Verify the real cninfo (巨潮资讯) filings source against the live API.

Usage (from the repository root with the project venv):

    .\\.venv\\Scripts\\python.exe -m scripts.verify_filings_source

Requires network access to www.cninfo.com.cn. Exits non-zero when the live
query returns no announcements.
"""

from __future__ import annotations

import asyncio
import sys

from backend.filings_source import CninfoFilingsSource, FilingSourceError


async def main() -> int:
    company = sys.argv[1] if len(sys.argv) > 1 else "平安银行"
    source = CninfoFilingsSource()
    try:
        hits = await source.search(company=company, max_results=5)
    except FilingSourceError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"cninfo returned {len(hits)} announcements for 「{company}」:")
    for hit in hits:
        print(f"  - {hit.title}")
        print(f"    {hit.url}")
        print(f"    {hit.snippet}")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
