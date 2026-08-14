from __future__ import annotations

import math

import pytest

from backend.embeddings import (
    BGE_LARGE_ZH_QUERY_INSTRUCTION,
    BgeLargeZhEmbeddingProvider,
    EmbeddingProfile,
)


class FakeModel:
    def __init__(self):
        self.calls: list[tuple[list[str], int]] = []
        self.device = "cpu"

    def encode(self, texts, *, batch_size, normalize_embeddings, show_progress_bar):
        self.calls.append((list(texts), batch_size))
        return [[3.0] + [4.0] + [0.0] * 1022 for _ in texts]


def test_profile_is_pinned_1024_dimension_and_stable():
    first = EmbeddingProfile()
    second = EmbeddingProfile()
    assert first.dimension == 1024
    assert first.revision != "main"
    assert first.profile_id == second.profile_id


def test_query_instruction_is_not_applied_to_documents():
    model = FakeModel()
    provider = BgeLargeZhEmbeddingProvider(model_factory=lambda *args, **kwargs: model)
    queries = provider.embed_queries(["盈利质量"])
    documents = provider.embed_documents(["盈利质量"])
    assert model.calls[0][0] == [BGE_LARGE_ZH_QUERY_INSTRUCTION + "盈利质量"]
    assert model.calls[1][0] == ["盈利质量"]
    assert len(queries.vectors[0]) == len(documents.vectors[0]) == 1024
    assert math.isclose(sum(v * v for v in queries.vectors[0]), 1.0)


def test_empty_batch_does_not_load_model_and_blank_text_fails():
    provider = BgeLargeZhEmbeddingProvider(
        model_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    assert provider.embed_documents([]).vectors == []
    with pytest.raises(ValueError):
        provider.embed_queries([" "])


def test_runtime_metadata_reports_pinned_identity_and_resolved_device():
    model = FakeModel()
    provider = BgeLargeZhEmbeddingProvider(
        device="cpu", model_factory=lambda *args, **kwargs: model
    )
    metadata = provider.runtime_metadata()
    assert metadata == {
        "model_name": provider.profile.model_name,
        "revision": provider.profile.revision,
        "profile_id": provider.profile.profile_id,
        "dimension": 1024,
        "normalize": True,
        "requested_device": "cpu",
        "resolved_device": "cpu",
    }


def test_invalid_dimension_and_model_load_failure_are_fail_closed():
    class BadModel(FakeModel):
        def encode(self, *args, **kwargs):
            return [[1.0, 2.0]]

    with pytest.raises(ValueError):
        BgeLargeZhEmbeddingProvider(
            model_factory=lambda *args, **kwargs: BadModel()
        ).embed_documents(["x"])
    with pytest.raises(RuntimeError, match="failed to load"):
        BgeLargeZhEmbeddingProvider(
            model_factory=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("secret"))
        ).embed_documents(["x"])
