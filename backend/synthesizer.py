"""LLM-backed report synthesis for the controlled-tools pipeline.

``ControlledToolsResearchProcessor`` gathers deterministic facts / quotes /
filings, runs them through ``ClaimVerifier``, and needs a final readable
research report. ``DeepSeekReportSynthesizer`` turns the verified evidence
bundle into a structured Markdown report through the DeepSeek Responses
API **without** granting it web access: the model may only reference URLs
that were already persisted by the tool chain.

Design rules (same as the rest of FinScope):

- Numbers / URLs / excerpts come from the evidence bundle, not from the
  model's memory. The prompt instructs the model to only cite evidence
  passed in.
- If the API key is missing or the call fails, the caller must fall back
  to the deterministic ``CitationConstrainedReporter``; this module never
  fabricates a report on failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from backend.deepseek_research import DEFAULT_DEEPSEEK_MODEL

SYNTHESIS_SYSTEM_PROMPT = """你是 FinScope 的受控综合报告 Agent。

你只能使用用户消息中提供的“证据”来撰写研究报告。不得使用你自身的记忆补全数字、日期或来源；不得调用任何工具；不得引用证据清单之外的 URL。

输出必须是 JSON 对象（不要 Markdown 代码围栏），结构如下：
{
  "title": "报告标题",
  "summary": "3-5 句执行摘要",
  "sections": [
    {
      "title": "章节标题",
      "content": "2-3 句导语，不要把多个发现挤在同一段落",
      "points": [
        {"label": "要点标题", "text": "基于证据的完整句子"}
      ],
      "source_urls": ["证据清单中存在的 URL"]
    }
  ]
}

要求：
1. 每个关键结论都必须引用证据中的 source_urls。
2. 证据不足时，在 summary 中显式写出“证据不足/未知”。
3. 保持客观，不输出买卖建议。
4. 至少覆盖核心结论、财务与业务驱动、风险与未知。
"""


@dataclass(frozen=True)
class SynthesizerConfig:
    api_key: str
    model: str = DEFAULT_DEEPSEEK_MODEL
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 90.0


class SynthesizerError(RuntimeError):
    pass


class DeepSeekReportSynthesizer:
    """Call DeepSeek once to turn verified evidence into a structured report."""

    def __init__(
        self,
        config: SynthesizerConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    @classmethod
    def from_tenant(
        cls,
        principal: Any,
        repository: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "DeepSeekReportSynthesizer | None":
        """Build a synthesizer from the tenant's stored LLM settings, falling
        back to the server-level environment variables when the tenant has not
        configured their own key.

        ``repository`` must expose ``get_llm_settings(principal)`` (the PG
        durable repository does). Returns ``None`` when neither a tenant key
        nor a server ``DEEPSEEK_API_KEY`` is available.
        """
        try:
            settings = repository.get_llm_settings(principal)
        except Exception:
            settings = None
        api_key = (settings or {}).get("api_key") or ""
        if not api_key:
            return cls.from_env(transport=transport)
        model = (settings or {}).get("model") or DEFAULT_DEEPSEEK_MODEL
        base_url = (settings or {}).get("base_url") or "https://api.deepseek.com"
        return cls(
            SynthesizerConfig(api_key=api_key, model=model, base_url=base_url),
            transport=transport,
        )

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "DeepSeekReportSynthesizer | None":
        import os

        values = os.environ if env is None else env
        key = (values.get("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            return None
        return cls(
            SynthesizerConfig(
                api_key=key,
                model=(values.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip()
                or DEFAULT_DEEPSEEK_MODEL,
                base_url=(values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip(),
            ),
            transport=transport,
        )

    async def synthesize(
        self,
        *,
        company: str,
        question: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a report JSON (title / summary / sections) or raise.

        ``evidence`` must be a list of dicts with at least ``url``,
        ``excerpt``, and ideally ``title`` / ``publisher``. The model may
        only cite URLs present in this list.
        """
        if not evidence:
            raise SynthesizerError("cannot synthesize a report without evidence")
        if not self.config.api_key:
            raise SynthesizerError("DEEPSEEK_API_KEY is not configured")

        evidence_block = "\n\n".join(
            self._format_evidence(i, item)
            for i, item in enumerate(evidence, start=1)
        )
        user_input = (
            f"公司：{company}\n"
            f"研究问题：{question}\n\n"
            f"证据清单（共 {len(evidence)} 条）：\n{evidence_block}\n\n"
            "请基于以上证据生成结构化研究报告 JSON。"
        )

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        transport = self.transport
        async with httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        ) as client:
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_input},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            try:
                response = await client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                raise SynthesizerError(f"DeepSeek synthesis request failed: {exc}") from exc
            if response.status_code != 200:
                raise SynthesizerError(
                    f"DeepSeek synthesis returned HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                parsed = json.loads(content)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise SynthesizerError(f"DeepSeek synthesis returned invalid JSON: {exc}") from exc

        allowed_urls = {str(item.get("url") or "").strip() for item in evidence}
        parsed = self._sanitize(parsed, allowed_urls)
        parsed["provider"] = "deepseek_synthesis"
        return parsed

    @staticmethod
    def _format_evidence(index: int, item: dict[str, Any]) -> str:
        url = str(item.get("url") or item.get("source_uri") or "").strip()
        title = str(item.get("title") or item.get("source_title") or "未命名来源").strip()
        publisher = str(item.get("publisher") or "").strip()
        excerpt = str(item.get("excerpt") or item.get("text") or "").strip()
        return (
            f"[{index}] {title}"
            + (f"（{publisher}）" if publisher else "")
            + f"\n    URL: {url}"
            + (f"\n    摘录: {excerpt}" if excerpt else "")
        )

    @staticmethod
    def _sanitize(parsed: Any, allowed_urls: set[str]) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            raise SynthesizerError("DeepSeek synthesis did not return a JSON object")
        sections: list[dict[str, Any]] = []
        raw_sections = parsed.get("sections")
        if isinstance(raw_sections, list):
            for section in raw_sections:
                if not isinstance(section, dict):
                    continue
                source_urls = [
                    url for url in section.get("source_urls") or []
                    if isinstance(url, str) and url.strip() in allowed_urls
                ]
                points = [
                    {
                        "label": str(point.get("label") or "")[:80],
                        "text": str(point.get("text") or "")[:2000],
                    }
                    for point in section.get("points") or []
                    if isinstance(point, dict) and (point.get("text") or "")
                ]
                sections.append({
                    "title": str(section.get("title") or "研究发现")[:200],
                    "content": str(section.get("content") or "")[:2000],
                    "points": points,
                    "source_urls": source_urls,
                })
        return {
            "title": str(parsed.get("title") or "公司研究报告")[:200],
            "summary": str(parsed.get("summary") or "")[:2000],
            "sections": sections,
        }