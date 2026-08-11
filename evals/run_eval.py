from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from evals.graders import failed_grade, grade_task, summarize


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_CASES = Path(__file__).with_name("cases.jsonl")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    required = {
        "id",
        "category",
        "question",
        "expected_company",
        "acceptable_symbols",
        "acceptable_markets",
        "expected_behavior",
    }
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on {path}:{line_number}: {exc}") from exc
        missing = required - set(case)
        if missing:
            raise ValueError(f"Missing fields on {path}:{line_number}: {sorted(missing)}")
        if not isinstance(case["question"], str) or len(case["question"].strip()) < 5:
            raise ValueError(f"Invalid question on {path}:{line_number}")
        if not isinstance(case["acceptable_symbols"], list) or not isinstance(
            case["acceptable_markets"], list
        ):
            raise ValueError(f"Acceptable identities must be lists on {path}:{line_number}")
        identifier = str(case["id"])
        if identifier in identifiers:
            raise ValueError(f"Duplicate case id {identifier!r} on {path}:{line_number}")
        identifiers.add(identifier)
        cases.append(case)
    if not cases:
        raise ValueError(f"No evaluation cases found in {path}")
    return cases


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
        return bool(addresses) and all(
            not (
                (address := ipaddress.ip_address(value)).is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_unspecified
            )
            for value in addresses
        )
    except (OSError, ValueError):
        return False


def check_urls(urls: list[str], timeout_seconds: float = 8.0) -> dict[str, bool]:
    results: dict[str, bool] = {}
    headers = {"User-Agent": "FinScopeEval/0.1"}
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        for url in dict.fromkeys(urls):
            if not _is_public_url(url):
                results[url] = False
                continue
            try:
                with client.stream("GET", url) as response:
                    results[url] = response.status_code < 400
            except httpx.HTTPError:
                results[url] = False
    return results


def run_case(
    client: httpx.Client,
    case: dict[str, Any],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    should_check_urls: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    first_result_seconds: float | None = None
    task: dict[str, Any] | None = None
    try:
        response = client.post(
            "/research",
            json={
                "company": "自动识别中",
                "symbol": None,
                "market": "AUTO",
                "question": case["question"],
                "agent": "financial",
                "depth": "standard",
            },
        )
        response.raise_for_status()
        task = response.json()
        task_id = task["id"]
        while task.get("status") not in TERMINAL_STATUSES:
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                raise TimeoutError(f"Task {task_id} exceeded {timeout_seconds:.0f}s")
            time.sleep(poll_seconds)
            response = client.get(f"/research/{task_id}")
            response.raise_for_status()
            task = response.json()
            if task.get("result") and first_result_seconds is None:
                first_result_seconds = time.monotonic() - started

        total_seconds = time.monotonic() - started
        if task.get("result") and first_result_seconds is None:
            first_result_seconds = total_seconds
        evidence_urls = [
            str(item.get("url") or "").strip()
            for item in task.get("evidence", [])
            if isinstance(item, dict) and item.get("url")
        ]
        reachability = check_urls(evidence_urls) if should_check_urls else None
        return {
            "case_id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "expected_behavior": case["expected_behavior"],
            "task_id": task.get("id"),
            "status": task.get("status"),
            "error": task.get("error"),
            "metrics": grade_task(
                case,
                task,
                first_result_seconds=first_result_seconds,
                total_seconds=total_seconds,
                url_reachability=reachability,
            ),
            "observed": {
                "company": task.get("company"),
                "symbol": task.get("symbol"),
                "market": task.get("market"),
                "provider": (
                    task.get("result", {}).get("provider")
                    if isinstance(task.get("result"), dict)
                    else None
                ),
            },
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError, TimeoutError) as exc:
        total_seconds = time.monotonic() - started
        return {
            "case_id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "expected_behavior": case["expected_behavior"],
            "task_id": task.get("id") if isinstance(task, dict) else None,
            "status": task.get("status") if isinstance(task, dict) else "runner_error",
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": failed_grade(total_seconds),
            "observed": None,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the FinScope baseline evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8780/api")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=Path("evals/reports/baseline.json"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=130.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--check-urls", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.timeout <= 0 or args.poll <= 0:
        raise SystemExit("--timeout and --poll must be positive")

    cases = load_cases(args.cases)
    if args.limit is not None:
        cases = cases[: args.limit]

    started_at = datetime.now(UTC)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=15.0) as client:
        try:
            health_response = client.get("/health")
            health_response.raise_for_status()
            health = health_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SystemExit(f"FinScope API is unavailable at {args.base_url}: {exc}") from exc

        results = []
        for index, case in enumerate(cases, 1):
            print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
            result = run_case(
                client,
                case,
                timeout_seconds=args.timeout,
                poll_seconds=args.poll,
                should_check_urls=args.check_urls,
            )
            results.append(result)
            print(
                f"  status={result['status']} total={result['metrics']['total_seconds']}s",
                flush=True,
            )

    report = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "api_base_url": args.base_url,
        "health": health,
        "url_reachability_checked": args.check_urls,
        "summary": summarize(results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
