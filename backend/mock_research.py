from __future__ import annotations

import asyncio
from typing import Any

from backend.database import Repository, TERMINAL_STATUSES


STAGES = [
    ("planning", 12, "正在拆解研究问题"),
    ("searching", 32, "正在检索公司披露与交易所材料"),
    ("reading", 56, "正在阅读候选网页与在线 PDF"),
    ("verifying", 78, "正在交叉验证核心结论"),
    ("writing", 94, "正在生成带引用的财报案卷"),
]


MOCK_EVIDENCE = [
    {
        "citation_number": 1,
        "title": "腾讯控股年度业绩公告",
        "publisher": "香港交易所",
        "url": "https://www.hkexnews.hk/",
        "source_type": "一手来源",
        "excerpt": "利润改善与主要业务分部表现的核心披露。",
        "agent": "财报分析 Agent",
    },
    {
        "citation_number": 2,
        "title": "投资者演示材料",
        "publisher": "腾讯投资者关系",
        "url": "https://www.tencent.com/",
        "source_type": "一手来源",
        "excerpt": "业务分部、毛利和费用结构变化说明。",
        "agent": "财报分析 Agent",
    },
    {
        "citation_number": 3,
        "title": "业务回顾与分部资料",
        "publisher": "公司披露",
        "url": "https://www.tencent.com/",
        "source_type": "一手来源",
        "excerpt": "主要业务的收入贡献与经营表现说明。",
        "agent": "财报分析 Agent",
    },
]


MOCK_REPORT: dict[str, Any] = {
    "title": "财务改善来自业务恢复与成本纪律",
    "summary": "游戏业务恢复、广告效率提升与费用纪律共同推动利润增长；资本开支与 AI 投入仍需持续跟踪。",
    "sections": [
        {
            "key": "financial-performance",
            "title": "财务表现",
            "content": "近三年收入恢复增长，利润增速高于收入，显示业务组合变化与成本控制共同发挥作用。",
            "citations": [1, 2],
        },
        {
            "key": "business-drivers",
            "title": "业务驱动",
            "content": "游戏业务恢复提供规模贡献，视频号商业化和广告推荐效率提升带来更高边际弹性。",
            "citations": [2, 3],
        },
        {
            "key": "risks",
            "title": "风险与未知",
            "content": "AI 资本开支节奏、游戏监管和广告需求仍是主要变量，当前案卷保留证据不足提示。",
            "citations": [1],
        },
    ],
    "synthetic": True,
}


async def run_mock_research(repository: Repository, task_id: str, delay_seconds: float) -> None:
    try:
        for step, progress, message in STAGES:
            while True:
                task = repository.get_task(task_id)
                if task is None or task["status"] in TERMINAL_STATUSES:
                    return
                if task["status"] != "paused":
                    break
                await asyncio.sleep(min(delay_seconds, 0.1) or 0.01)

            repository.update_task(
                task_id,
                status="running",
                step=step,
                progress=progress,
                message=message,
            )
            await asyncio.sleep(delay_seconds)

        repository.replace_evidence(task_id, MOCK_EVIDENCE)
        repository.update_task(
            task_id,
            status="completed",
            step="completed",
            progress=100,
            message="研究完成",
            kind="task.completed",
            result=MOCK_REPORT,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        repository.update_task(
            task_id,
            status="failed",
            step="failed",
            progress=0,
            message="研究执行失败",
            kind="task.failed",
            error=str(exc),
        )

