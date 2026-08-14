from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


BGE_LARGE_ZH_MODEL = "BAAI/bge-large-zh-v1.5"
BGE_LARGE_ZH_REVISION = "79e7739b6ab944e86d6171e44d24c997fc1e0116"
BGE_LARGE_ZH_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_name: str = BGE_LARGE_ZH_MODEL
    revision: str = BGE_LARGE_ZH_REVISION
    dimension: int = Field(default=1024, gt=0)
    query_instruction: str = BGE_LARGE_ZH_QUERY_INSTRUCTION
    normalize: bool = True

    @property
    def profile_id(self) -> str:
        value = self.model_dump_json().encode("utf-8")
        return "emb_" + hashlib.sha256(value).hexdigest()[:24]


class EmbeddingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    vectors: list[list[float]]


class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile

    def embed_queries(self, texts: Sequence[str]) -> EmbeddingBatch: ...

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch: ...


def _validate_and_normalize(
    vectors: Sequence[Sequence[float]], *, dimension: int, normalize: bool
) -> list[list[float]]:
    result: list[list[float]] = []
    for vector in vectors:
        converted = [float(value) for value in vector]
        if len(converted) != dimension or not all(math.isfinite(value) for value in converted):
            raise ValueError("embedding vector has invalid dimension or non-finite values")
        norm = math.sqrt(sum(value * value for value in converted))
        if norm == 0:
            raise ValueError("embedding vector cannot be zero")
        if normalize:
            converted = [value / norm for value in converted]
        result.append(converted)
    return result


class BgeLargeZhEmbeddingProvider:
    def __init__(
        self,
        *,
        profile: EmbeddingProfile | None = None,
        device: str | None = None,
        batch_size: int = 16,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        self.profile = profile or EmbeddingProfile()
        self.device = device
        self.batch_size = batch_size
        self._model_factory = model_factory
        self._model: object | None = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        factory = self._model_factory
        if factory is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "BGE embedding dependency is not installed; install requirements-rag.txt"
                ) from exc
            factory = SentenceTransformer
        try:
            self._model = factory(
                self.profile.model_name,
                revision=self.profile.revision,
                device=self.device,
            )
        except Exception as exc:
            raise RuntimeError("BGE embedding model failed to load") from exc
        return self._model

    def _embed(self, texts: Sequence[str], *, query: bool) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(profile_id=self.profile.profile_id, vectors=[])
        cleaned = [str(text).strip() for text in texts]
        if any(not text for text in cleaned):
            raise ValueError("embedding text cannot be empty")
        encoded = [
            f"{self.profile.query_instruction}{text}" if query else text
            for text in cleaned
        ]
        model = self._load_model()
        vectors = model.encode(
            encoded,
            batch_size=self.batch_size,
            normalize_embeddings=False,
            show_progress_bar=False,
        )
        return EmbeddingBatch(
            profile_id=self.profile.profile_id,
            vectors=_validate_and_normalize(
                vectors,
                dimension=self.profile.dimension,
                normalize=self.profile.normalize,
            ),
        )

    def embed_queries(self, texts: Sequence[str]) -> EmbeddingBatch:
        return self._embed(texts, query=True)

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return self._embed(texts, query=False)

    def runtime_metadata(self) -> dict[str, str | int | bool]:
        """Return the loaded runtime identity used by operational RAG gates."""
        model = self._load_model()
        resolved_device = getattr(model, "device", None)
        return {
            "model_name": self.profile.model_name,
            "revision": self.profile.revision,
            "profile_id": self.profile.profile_id,
            "dimension": self.profile.dimension,
            "normalize": self.profile.normalize,
            "requested_device": self.device or "auto",
            "resolved_device": str(resolved_device or self.device or "unknown"),
        }
