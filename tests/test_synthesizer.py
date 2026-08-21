"""Tests for the LLM report synthesizer used by the controlled-tools pipeline."""

import asyncio
import json

import httpx
import pytest

from backend.synthesizer import (
    DeepSeekReportSynthesizer,
    SynthesizerConfig,
    SynthesizerError,
)


def _evidence(url="https://example.com/a"):
    return [
        {
            "url": url,
            "title": "来源 A",
            "publisher": "测试源",
            "excerpt": "营业收入 100 亿元",
        },
        {
            "url": "https://example.com/b",
            "title": "来源 B",
            "publisher": "测试源",
            "excerpt": "净利润 10 亿元",
        },
    ]


def _make_synthesizer(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        })

    return DeepSeekReportSynthesizer(
        SynthesizerConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )


def test_synthesize_returns_sanitized_report():
    synthesizer = _make_synthesizer({
        "title": "测试报告",
        "summary": "摘要",
        "sections": [
            {
                "title": "财务表现",
                "content": "导语",
                "points": [{"label": "营收", "text": "营收增长"}],
                "source_urls": ["https://example.com/a", "https://evil.com/x"],
            },
        ],
    })

    async def run():
        return await synthesizer.synthesize(
            company="测试公司", question="表现如何", evidence=_evidence(),
        )

    out = asyncio.run(run())
    assert out["title"] == "测试报告"
    assert out["summary"] == "摘要"
    assert len(out["sections"]) == 1
    section = out["sections"][0]
    assert section["source_urls"] == ["https://example.com/a"]  # 非法 URL 被剔除
    assert section["points"][0]["text"] == "营收增长"
    assert out["provider"] == "deepseek_synthesis"


def test_synthesize_raises_without_evidence():
    synthesizer = DeepSeekReportSynthesizer(SynthesizerConfig(api_key="test-key"))

    async def run():
        return await synthesizer.synthesize(
            company="测试公司", question="表现如何", evidence=[],
        )

    with pytest.raises(SynthesizerError, match="without evidence"):
        asyncio.run(run())


def test_synthesize_raises_without_api_key():
    synthesizer = DeepSeekReportSynthesizer(SynthesizerConfig(api_key=""))

    async def run():
        return await synthesizer.synthesize(
            company="测试公司", question="表现如何", evidence=_evidence(),
        )

    with pytest.raises(SynthesizerError, match="DEEPSEEK_API_KEY"):
        asyncio.run(run())


def test_synthesize_raises_on_invalid_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "not-json"}}],
        })

    synthesizer = DeepSeekReportSynthesizer(
        SynthesizerConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )

    async def run():
        return await synthesizer.synthesize(
            company="测试公司", question="表现如何", evidence=_evidence(),
        )

    with pytest.raises(SynthesizerError, match="invalid JSON"):
        asyncio.run(run())


def test_from_env_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert DeepSeekReportSynthesizer.from_env({}) is None


def test_from_env_builds_client_with_key():
    synthesizer = DeepSeekReportSynthesizer.from_env({"DEEPSEEK_API_KEY": "k"})
    assert synthesizer is not None
    assert synthesizer.config.api_key == "k"