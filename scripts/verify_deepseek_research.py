from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from backend.deepseek_research import DEFAULT_DEEPSEEK_MODEL, DeepSeekConfig, DeepSeekResearchClient
from backend.environment import load_environment


PROGRESS_EVENTS = {
    "response.web_search_call.searching": "[搜索] 正在联网搜索…",
    "response.web_search_call.completed": "[阅读] 搜索完成，正在整理来源…",
    "response.reasoning_text.delta": "[分析] 正在分析已检索信息…",
    "response.output_text.delta": "[写作] 正在生成研究报告…",
}


def _print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print(f"标题：{report.get('title') or '(未返回标题)'}")
    print(f"公司：{report.get('company') or '(未识别)'}")
    print(f"证券：{report.get('symbol') or '-'}（{report.get('market', 'OTHER')}）")
    summary = str(report.get("summary") or "").strip()
    if summary:
        print(f"\n摘要：{summary}")
    sections = report.get("sections") or []
    print(f"\n章节（{len(sections)} 节）：")
    for section in sections:
        print(f"\n## {section.get('title') or '(无标题)'}")
        content = str(section.get("content") or "").strip()
        if content:
            print(content)
        for point in section.get("points") or []:
            label = str(point.get("label") or "").strip()
            text = str(point.get("text") or "").strip()
            if label or text:
                print(f"- {label}：{text}" if label else f"- {text}")
        citations = section.get("citations") or []
        if citations:
            print(f"  引用：[{'、'.join(str(c) for c in citations)}]")


def _print_evidence(evidence: list[dict]) -> None:
    print("\n" + "=" * 60)
    print(f"证据来源（{len(evidence)} 条）：")
    for item in evidence:
        number = item.get("citation_number")
        title = item.get("title") or "(无标题)"
        url = item.get("url") or "(无 URL)"
        print(f"[{number}] {title}")
        print(f"    {url}")


async def _run(question: str, company: str, symbol: str, market: str) -> int:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY。请设置环境变量，或写入 backend/.env。", file=sys.stderr)
        print("示例：backend/.env 中写入 DEEPSEEK_API_KEY=sk-xxxx", file=sys.stderr)
        return 2
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL
    client = DeepSeekResearchClient(DeepSeekConfig(api_key=api_key, model=model))

    seen_steps: set[str] = set()

    def on_event(event: dict) -> None:
        event_type = str(event.get("type") or "")
        message = PROGRESS_EVENTS.get(event_type)
        if message and event_type not in seen_steps:
            seen_steps.add(event_type)
            print(message)

    task = {
        "question": question,
        "company": company,
        "symbol": symbol,
        "market": market,
    }
    print(f"模型：{model}")
    print(f"研究问题：{question}")
    print(f"目标公司：{company}（{symbol}，{market}）")
    print("开始联网调研（首次需联网搜索+阅读，可能需要 1-3 分钟）…")
    try:
        report, evidence = await client.research(task, on_event=on_event)
    except Exception as exc:  # noqa: BLE001
        print(f"\n验证失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_report(report)
    _print_evidence(evidence)
    print("\n" + "=" * 60)
    print("验证通过：DeepSeek 联网搜索研究链路可用。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="端到端验证 DeepSeek 联网搜索研究链路")
    parser.add_argument(
        "--question", default="腾讯近三年利润增长由哪些业务驱动，这种增长是否可持续？",
    )
    parser.add_argument("--company", default="腾讯控股")
    parser.add_argument("--symbol", default="0700")
    parser.add_argument("--market", default="HK")
    args = parser.parse_args()
    load_environment(Path(__file__).resolve().parents[1] / "backend" / ".env")
    sys.exit(asyncio.run(_run(args.question, args.company, args.symbol, args.market)))


if __name__ == "__main__":
    main()
