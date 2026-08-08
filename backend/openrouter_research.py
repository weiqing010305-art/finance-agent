from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter cannot return a usable research result."""


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = "https://openrouter.ai/api/v1"
    search_engine: str = "parallel"
    max_results: int = 6
    timeout_seconds: float = 180.0


SYSTEM_PROMPT = """你是 FinScope 的财报分析 Agent。你需要使用联网搜索结果完成公司研究。

要求：
1. 优先采用公司官网、交易所、监管机构和正式公告，其次才是权威媒体。
2. 不得编造数字、日期、事件或来源；证据不足时必须明确写出不确定性。
3. 每个结论都要关联支持它的网页 URL。
4. 只输出 JSON，不要输出 Markdown 代码围栏或额外解释。

JSON 结构必须是：
{
  "company": "从用户问题中识别出的公司全称",
  "symbol": "证券代码；无法确认时为空字符串",
  "market": "CN、HK、US 或 OTHER",
  "title": "报告标题",
  "summary": "两到三句话的执行摘要",
  "sections": [
    {
      "key": "稳定的英文短标识",
      "title": "章节标题",
      "content": "基于证据的分析",
      "source_urls": ["完整来源 URL"]
    }
  ]
}

这是第一阶段快速研究。优先返回可验证的核心材料，不追求穷尽；至少包含：核心结论、财务与业务驱动、风险与未知三个简短章节。每节控制在 180 字以内。source_urls 只能使用搜索结果中真实出现的 URL。"""


SYNTHESIS_PROMPT = """你是 FinScope 的财报分析 Agent。根据已经检索到的证据和第一阶段简报，整理完整公司研究报告。

要求：
1. 不再搜索，也不得引入所给证据之外的事实或 URL。
2. 区分事实、推断和未知；证据不足时明确说明。
3. 只输出 JSON，不要输出 Markdown 代码围栏或额外解释。
4. JSON 结构与第一阶段简报相同，source_urls 只能使用证据列表中的完整 URL。
5. 生成 3 至 5 个章节，优先覆盖核心结论、财务与业务驱动、风险与未知。"""


class OpenRouterResearchClient:
    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    async def research(self, task: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = self._build_payload(task)
        message = await self._request(payload)

        report = self._parse_report(message.get("content"))
        evidence, url_to_citation = self._build_evidence(
            message.get("annotations", []),
            report,
        )
        normalized_report = self._normalize_report(report, url_to_citation)
        return normalized_report, evidence

    async def synthesize(
        self,
        task: dict[str, Any],
        quick_report: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_context = [
            {
                "citation_number": item["citation_number"],
                "title": item["title"],
                "url": item["url"],
                "excerpt": str(item.get("excerpt") or "")[:500],
            }
            for item in evidence
        ]
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYNTHESIS_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": task["question"],
                            "company": quick_report.get("company") or task["company"],
                            "symbol": quick_report.get("symbol") or task.get("symbol") or "",
                            "market": quick_report.get("market") or task["market"],
                            "quick_report": quick_report,
                            "evidence": source_context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 2_600,
        }
        message = await self._request(payload)
        report = self._parse_report(message.get("content"))
        url_to_citation = {
            item["url"]: item["citation_number"]
            for item in evidence
            if item.get("url") and item.get("citation_number")
        }
        return self._normalize_report(report, url_to_citation)

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8770",
            "X-Title": "FinScope MVP",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            response = await client.post("/chat/completions", json=payload)

        if response.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter 请求失败（HTTP {response.status_code}），请检查密钥、余额和模型配置"
            )

        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenRouterError("OpenRouter 返回了无法识别的响应") from exc
        if not isinstance(message, dict):
            raise OpenRouterError("OpenRouter 返回了无法识别的消息")
        return message

    def _build_payload(self, task: dict[str, Any]) -> dict[str, Any]:
        auto_detect = task["company"] == "自动识别中" or task["market"] == "AUTO"
        company_context = []
        if auto_detect:
            company_context.append("请先从用户问题中识别研究公司、证券代码和所属市场，再开展研究。")
        else:
            company_context.extend([f"公司：{task['company']}", f"市场：{task['market']}"])
            if task.get("symbol"):
                company_context.append(f"证券代码：{task['symbol']}")
        company_context.extend(
            [
                f"研究深度：{task['depth']}",
                f"用户问题：{task['question']}",
                "请检索最新且可验证的信息，并在结论中区分事实、推断和未知。",
            ]
        )
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(company_context)},
            ],
            "response_format": {"type": "json_object"},
            "plugins": [
                {
                    "id": "web",
                    "engine": self.config.search_engine,
                    "max_results": self.config.max_results,
                }
            ],
            "temperature": 0.2,
            "max_tokens": 1_400,
        }

    @staticmethod
    def _parse_report(content: Any) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterError("OpenRouter 没有返回报告内容")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            report = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter 返回的报告不是有效 JSON") from exc
        if not isinstance(report, dict) or not isinstance(report.get("sections"), list):
            raise OpenRouterError("OpenRouter 返回的报告缺少章节")
        return report

    @classmethod
    def _build_evidence(
        cls,
        annotations: Any,
        report: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        sources: dict[str, dict[str, str]] = {}
        if isinstance(annotations, list):
            for annotation in annotations:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                citation = annotation.get("url_citation") or {}
                url = cls._clean_url(citation.get("url"))
                if not url:
                    continue
                sources.setdefault(
                    url,
                    {
                        "title": str(citation.get("title") or url),
                        "excerpt": str(citation.get("content") or "")[:1_200],
                    },
                )

        for section in report.get("sections", []):
            if not isinstance(section, dict):
                continue
            for raw_url in section.get("source_urls", []) or []:
                url = cls._clean_url(raw_url)
                if url:
                    sources.setdefault(url, {"title": url, "excerpt": ""})

        evidence: list[dict[str, Any]] = []
        url_to_citation: dict[str, int] = {}
        for number, (url, source) in enumerate(sources.items(), start=1):
            domain = urlparse(url).netloc.lower().removeprefix("www.") or "网页来源"
            url_to_citation[url] = number
            evidence.append(
                {
                    "citation_number": number,
                    "title": source["title"],
                    "publisher": domain,
                    "url": url,
                    "source_type": cls._source_type(domain),
                    "excerpt": source["excerpt"] or "点击访问原始来源并核对完整上下文。",
                    "agent": "财报分析 Agent",
                }
            )
        return evidence, url_to_citation

    @classmethod
    def _normalize_report(
        cls,
        report: dict[str, Any],
        url_to_citation: dict[str, int],
    ) -> dict[str, Any]:
        sections = []
        for index, section in enumerate(report.get("sections", []), start=1):
            if not isinstance(section, dict):
                continue
            citations = []
            for raw_url in section.get("source_urls", []) or []:
                number = url_to_citation.get(cls._clean_url(raw_url))
                if number and number not in citations:
                    citations.append(number)
            sections.append(
                {
                    "key": str(section.get("key") or f"section-{index}"),
                    "title": str(section.get("title") or f"研究发现 {index}"),
                    "content": str(section.get("content") or ""),
                    "citations": citations,
                }
            )
        return {
            "company": str(report.get("company") or "").strip(),
            "symbol": str(report.get("symbol") or "").strip(),
            "market": str(report.get("market") or "OTHER").strip().upper(),
            "title": str(report.get("title") or "公司研究报告"),
            "summary": str(report.get("summary") or ""),
            "sections": sections,
            "synthetic": False,
            "provider": "openrouter",
        }

    @staticmethod
    def _clean_url(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return url

    @staticmethod
    def _source_type(domain: str) -> str:
        primary_domains = (
            "sec.gov",
            "hkexnews.hk",
            "cninfo.com.cn",
            "sse.com.cn",
            "szse.cn",
        )
        return "一手来源" if domain.endswith(primary_domains) else "网页来源"
