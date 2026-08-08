from __future__ import annotations

import asyncio
import ipaddress
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


class DeepSeekError(RuntimeError):
    """Raised when DeepSeek cannot return a usable research result."""


class _PageMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True
            return
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        name = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content", "").strip()
        if name and content:
            self.meta[name] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and data.strip():
            self.title_parts.append(data.strip())

    def result(self) -> dict[str, str]:
        return {
            "title": self.meta.get("og:title") or self.meta.get("twitter:title") or " ".join(self.title_parts),
            "publisher": self.meta.get("og:site_name") or self.meta.get("application-name") or "",
            "excerpt": self.meta.get("description") or self.meta.get("og:description") or self.meta.get("twitter:description") or "",
        }


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    model: str = DEFAULT_DEEPSEEK_MODEL
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 180.0


SYSTEM_PROMPT = """你是 FinScope 的财报分析 Agent。请使用服务端联网搜索调查用户指定的公司。

输出协议：
1. 先输出一段可读的研究草稿，用 `REPORT_DRAFT:` 开头。草稿使用自然语言和 Markdown 标题/要点，方便用户在页面上实时阅读。
2. 草稿结束后输出 `FINAL_JSON:`，其后只放完整 JSON，不要放 Markdown 代码围栏。

要求：
1. 优先采用交易所、监管机构、公司官网和正式公告，其次采用权威财经媒体。
2. 不得编造数字、日期、事件或来源；证据不足时明确写出未知。
3. 区分事实与推断，每个关键结论关联真实来源 URL。
4. `FINAL_JSON:` 之后必须是可被 JSON.parse 解析的对象，不要输出额外解释。
5. 联网搜索最多打开 12 个页面；达到上限后必须停止搜索并立即整理最终报告。

JSON 结构：
{
  "company": "公司全称",
  "symbol": "证券代码，无法确认时为空字符串",
  "market": "CN、HK、US 或 OTHER",
  "title": "报告标题",
  "summary": "执行摘要",
  "sources": [
    {
      "url": "完整来源 URL",
      "title": "网页原始标题",
      "publisher": "网站或机构名称",
      "excerpt": "网页正文开头的简短原文摘录"
    }
  ],
  "sections": [
    {
      "key": "稳定英文标识",
      "title": "章节标题",
      "content": "本节 2 至 3 句导语；不要把多个发现挤在同一长段落中",
      "points": [
        {"label": "简短要点标题", "text": "基于证据的完整说明"}
      ],
      "source_urls": ["完整来源 URL"]
    }
  ]
}

至少覆盖核心结论、财务与业务驱动、风险与未知。source_urls 只能使用联网搜索真实返回的 URL。
每节包含多个发现时，必须拆入 points；label 简洁明确，text 使用完整句子，避免在 content 中写 1)、2)、3) 形式的长串内容。
sources 必须覆盖所有被引用 URL；title、publisher 和 excerpt 必须来自实际浏览页面，不得凭空补写。"""


class DeepSeekResearchClient:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    async def research(
        self,
        task: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = {
            "model": self.config.model,
            "instructions": SYSTEM_PROMPT,
            "input": self._task_input(task),
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "stream": True,
        }
        completed_response: dict[str, Any] | None = None
        terminal_event: dict[str, Any] | None = None
        observed_event_types: list[str] = []
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            async with client.stream("POST", "/responses", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    raise DeepSeekError(f"DeepSeek 请求失败（HTTP {response.status_code}）：{body}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "")
                    if event_type:
                        observed_event_types.append(event_type)
                    if on_event is not None:
                        on_event(event)
                    if event_type == "response.completed" and isinstance(event.get("response"), dict):
                        completed_response = event["response"]
                    elif event_type in {"response.failed", "response.incomplete", "error"}:
                        terminal_event = event

        if completed_response is None:
            if terminal_event is not None:
                raise DeepSeekError(self._terminal_error_message(terminal_event))
            tail = ", ".join(observed_event_types[-5:]) or "无事件"
            raise DeepSeekError(f"DeepSeek 流式响应未包含完成事件；最后事件：{tail}")
        try:
            text, annotations = self._extract_output(completed_response)
        except DeepSeekError as exc:
            if str(exc) != "DeepSeek 响应没有报告文本":
                raise
            if on_event is not None:
                on_event({
                    "type": "finscope.report_recovery",
                    "message": "DeepSeek 未返回报告正文，正在停止搜索并重新整理报告",
                })
            recovered_response = await self._recover_report(completed_response, task)
            text, annotations = self._extract_output(recovered_response)
        report = self._parse_report(text)
        evidence, url_to_citation = self._build_evidence(annotations, report)
        evidence = await self.enrich_evidence(evidence)
        return self._normalize_report(report, url_to_citation), evidence

    async def _recover_report(
        self,
        completed_response: dict[str, Any],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        response_id = str(completed_response.get("id") or "").strip()
        payload: dict[str, Any] = {
            "model": self.config.model,
            "instructions": SYSTEM_PROMPT + (
                "\n恢复要求：上一轮已经完成联网调查。禁止再次搜索或调用任何工具，"
                "请直接基于上一轮上下文输出完整 JSON 报告。"
            ),
            "input": "请停止搜索，立即把已经调查到的信息整理为要求的完整 JSON 报告。",
            "stream": False,
        }
        if response_id:
            payload["previous_response_id"] = response_id
        else:
            payload["input"] = (
                f"{self._task_input(task)}\n\n上一轮联网调查没有生成最终正文。"
                "禁止再次搜索，请直接输出完整 JSON 报告。"
            )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            response = await client.post("/responses", json=payload)
        if response.status_code >= 400:
            body = response.text[:500]
            raise DeepSeekError(f"DeepSeek 报告恢复失败（HTTP {response.status_code}）：{body}")
        try:
            recovered = response.json()
        except json.JSONDecodeError as exc:
            raise DeepSeekError("DeepSeek 报告恢复响应不是有效 JSON") from exc
        if not isinstance(recovered, dict):
            raise DeepSeekError("DeepSeek 报告恢复响应格式无效")
        return recovered

    async def enrich_evidence(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched = [dict(item) for item in evidence]
        candidates = [item for item in enriched if self._needs_page_metadata(item)]
        if not candidates:
            return enriched

        semaphore = asyncio.Semaphore(6)
        timeout = httpx.Timeout(6.0)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FinScopeResearch/0.1; +http://127.0.0.1)"}
        async with httpx.AsyncClient(timeout=timeout, headers=headers, transport=self.transport) as client:
            async def enrich_one(item: dict[str, Any]) -> None:
                async with semaphore:
                    metadata = await self._fetch_page_metadata(client, str(item.get("url") or ""))
                if metadata.get("title"):
                    item["title"] = metadata["title"][:500]
                if metadata.get("publisher"):
                    item["publisher"] = metadata["publisher"][:200]
                if metadata.get("excerpt"):
                    item["excerpt"] = metadata["excerpt"][:1_200]
                if self._is_url_title(item.get("title"), item.get("url")):
                    site = str(item.get("publisher") or urlparse(str(item.get("url") or "")).netloc or "网页来源")
                    item["title"] = f"{site} 网页内容"

            await asyncio.gather(*(enrich_one(item) for item in candidates))
        return enriched

    @classmethod
    async def _fetch_page_metadata(
        cls,
        client: httpx.AsyncClient,
        url: str,
    ) -> dict[str, str]:
        current_url = url
        try:
            for _ in range(4):
                if not cls._is_public_web_url(current_url):
                    return {}
                response = await client.get(current_url, follow_redirects=False)
                if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                    current_url = urljoin(current_url, response.headers["location"])
                    continue
                if response.status_code >= 400 or "html" not in response.headers.get("content-type", "").lower():
                    return {}
                parser = _PageMetadataParser()
                parser.feed(response.text[:600_000])
                return {key: " ".join(value.split()) for key, value in parser.result().items() if value.strip()}
        except (httpx.HTTPError, UnicodeError, ValueError):
            return {}
        return {}

    @classmethod
    def _needs_page_metadata(cls, item: dict[str, Any]) -> bool:
        excerpt = str(item.get("excerpt") or "").strip()
        return cls._is_url_title(item.get("title"), item.get("url")) or not excerpt or excerpt.startswith("点击访问原始来源")

    @staticmethod
    def _is_url_title(title: Any, url: Any) -> bool:
        value = str(title or "").strip()
        return not value or value == str(url or "").strip() or value.lower().startswith(("http://", "https://"))

    @staticmethod
    def _is_public_web_url(value: str) -> bool:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not hostname or hostname == "localhost" or hostname.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)

    @staticmethod
    def _terminal_error_message(event: dict[str, Any]) -> str:
        event_type = str(event.get("type") or "error")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        details = response.get("incomplete_details") if isinstance(response.get("incomplete_details"), dict) else {}
        error = response.get("error") if isinstance(response.get("error"), dict) else event.get("error")
        if isinstance(error, dict):
            reason = error.get("message") or error.get("code") or error.get("type")
        else:
            reason = error
        reason = reason or details.get("reason") or event.get("message") or "未知原因"
        return f"DeepSeek 返回 {event_type}：{reason}"

    @staticmethod
    def _task_input(task: dict[str, Any]) -> str:
        context = [f"用户问题：{task['question']}"]
        if task.get("company") and task["company"] != "自动识别中":
            context.append(f"公司：{task['company']}")
        if task.get("symbol"):
            context.append(f"证券代码：{task['symbol']}")
        if task.get("market") and task["market"] != "AUTO":
            context.append(f"市场：{task['market']}")
        return "\n".join(context)

    @staticmethod
    def _extract_output(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        texts: list[str] = []
        annotations: list[dict[str, Any]] = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                if isinstance(part.get("text"), str):
                    texts.append(part["text"])
                for annotation in part.get("annotations", []) or []:
                    if isinstance(annotation, dict):
                        annotations.append(annotation)
        if not texts:
            raise DeepSeekError("DeepSeek 响应没有报告文本")
        return DeepSeekResearchClient._extract_final_json("".join(texts)), annotations

    @staticmethod
    def _extract_final_json(text: str) -> str:
        marker = "FINAL_JSON:"
        if marker in text:
            return text.split(marker, 1)[1].strip()
        stripped = text.strip()
        draft_marker = "REPORT_DRAFT:"
        if stripped.startswith(draft_marker):
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                return stripped[start : end + 1]
        return stripped

    @staticmethod
    def _parse_report(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines.pop()
            text = "\n".join(lines).strip()
        try:
            report = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeepSeekError("DeepSeek 返回的报告不是有效 JSON") from exc
        if not isinstance(report, dict) or not isinstance(report.get("sections"), list):
            raise DeepSeekError("DeepSeek 返回的报告缺少章节")
        return report

    @classmethod
    def _build_evidence(
        cls,
        annotations: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        sources: dict[str, dict[str, str]] = {}
        for annotation in annotations:
            if annotation.get("type") != "url_citation":
                continue
            citation = annotation.get("url_citation") if isinstance(annotation.get("url_citation"), dict) else annotation
            url = cls._clean_url(citation.get("url"))
            if url:
                sources.setdefault(
                    url,
                    {
                        "title": str(citation.get("title") or url),
                        "publisher": "",
                        "excerpt": str(citation.get("content") or citation.get("snippet") or "")[:1_200],
                    },
                )
        for raw_source in report.get("sources", []) or []:
            if not isinstance(raw_source, dict):
                continue
            url = cls._clean_url(raw_source.get("url"))
            if not url:
                continue
            source = sources.setdefault(url, {"title": url, "publisher": "", "excerpt": ""})
            title = str(raw_source.get("title") or "").strip()
            publisher = str(raw_source.get("publisher") or "").strip()
            excerpt = str(raw_source.get("excerpt") or "").strip()[:1_200]
            if title:
                source["title"] = title
            if publisher:
                source["publisher"] = publisher
            if excerpt:
                source["excerpt"] = excerpt
        for section in report.get("sections", []):
            if not isinstance(section, dict):
                continue
            for raw_url in section.get("source_urls", []) or []:
                url = cls._clean_url(raw_url)
                if url:
                    sources.setdefault(url, {"title": url, "publisher": "", "excerpt": ""})

        evidence = []
        url_to_citation = {}
        for number, (url, source) in enumerate(sources.items(), start=1):
            domain = urlparse(url).netloc.lower().removeprefix("www.") or "网页来源"
            url_to_citation[url] = number
            evidence.append(
                {
                    "citation_number": number,
                    "title": source["title"],
                    "publisher": source.get("publisher") or domain,
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
            points = []
            for point in section.get("points", []) or []:
                if not isinstance(point, dict):
                    continue
                label = str(point.get("label") or "").strip()
                text = str(point.get("text") or "").strip()
                if label or text:
                    points.append({"label": label, "text": text})
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
                    "points": points,
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
            "provider": "deepseek",
        }

    @staticmethod
    def _clean_url(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        url = value.strip()
        parsed = urlparse(url)
        return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""

    @staticmethod
    def _source_type(domain: str) -> str:
        primary_domains = ("sec.gov", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn")
        return "一手来源" if domain.endswith(primary_domains) else "网页来源"
