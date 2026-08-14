from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from backend.embeddings import EmbeddingProvider
from backend.retrieval import (
    HybridRetriever, IndexedChunk, RetrievalQuery, RetrievalResponse, RetrievalResult,
)


OUTPUT_FIELDS = [
    "chunk_id", "document_id", "document_version_id", "text", "title", "source_uri",
    "publisher", "source_type", "access_scope", "page", "section", "authority_tier",
    "published_at", "embedding_profile_id", "index_version",
]


class MilvusUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MilvusConfig:
    uri: str
    token: str | None
    collection: str = "finance_agent_chunks_v1"
    timeout_seconds: float = 10.0
    dimension: int = 1024


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_filter(request: RetrievalQuery, *, allowed_chunk_ids: list[str] | None = None) -> str:
    # Once PostgreSQL has produced a concrete authorization set, those IDs are
    # the access boundary and may intentionally include own-private + shared-public.
    terms = [] if allowed_chunk_ids is not None else [
        f"access_scope == {_quote(request.filters.access_scope)}"
    ]
    for name in ("company", "symbol", "market", "period"):
        value = getattr(request.filters, name)
        if value:
            terms.append(f"{name} == {_quote(value)}")
    if request.filters.document_types:
        values = ",".join(_quote(item) for item in request.filters.document_types)
        terms.append(f"source_type in [{values}]")
    terms.extend([
        f"embedding_profile_id == {_quote(request.embedding_profile_id)}",
        f"index_version == {_quote(request.index_version)}",
    ])
    if allowed_chunk_ids is not None:
        if not allowed_chunk_ids:
            terms.append("chunk_id in []")
        else:
            values = ",".join(_quote(item) for item in allowed_chunk_ids)
            terms.append(f"chunk_id in [{values}]")
    return " and ".join(terms)


class MilvusHybridRetriever:
    backend_name = "milvus"

    def __init__(
        self,
        config: MilvusConfig,
        embeddings: EmbeddingProvider,
        *,
        client: Any | None = None,
        sdk_factory: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.embeddings = embeddings
        self._client = client
        self._sdk_factory = sdk_factory

    def _sdk(self) -> dict[str, Any]:
        if self._sdk_factory is not None:
            return self._sdk_factory()
        try:
            from pymilvus import (
                AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker,
            )
        except ImportError as exc:
            raise MilvusUnavailable(
                "PyMilvus is not installed; install requirements-rag.txt"
            ) from exc
        return {
            "AnnSearchRequest": AnnSearchRequest, "DataType": DataType,
            "Function": Function, "FunctionType": FunctionType,
            "MilvusClient": MilvusClient, "RRFRanker": RRFRanker,
        }

    def _get_client(self):
        if self._client is None:
            sdk = self._sdk()
            try:
                self._client = sdk["MilvusClient"](
                    uri=self.config.uri,
                    token=self.config.token or None,
                    timeout=self.config.timeout_seconds,
                )
            except Exception as exc:
                raise MilvusUnavailable("Milvus connection failed") from exc
        return self._client

    def ensure_collection(self) -> None:
        client = self._get_client()
        if client.has_collection(collection_name=self.config.collection):
            try:
                description = client.describe_collection(collection_name=self.config.collection)
                fields = description.get("fields") or description.get("schema", {}).get("fields") or []
                by_name = {
                    str(field.get("name") or field.get("field_name")): field for field in fields
                }
                required = {
                    "chunk_id", "text", "sparse_vector", "dense_vector",
                    "embedding_profile_id", "index_version", "access_scope",
                }
                if not required <= set(by_name):
                    raise MilvusUnavailable("existing Milvus collection schema is incompatible")
                dense = by_name["dense_vector"]
                dim = dense.get("params", {}).get("dim", dense.get("dim"))
                if dim is not None and int(dim) != self.config.dimension:
                    raise MilvusUnavailable("existing Milvus dense dimension is incompatible")
                functions = description.get("functions") or description.get("schema", {}).get("functions") or []
                if not any(
                    "bm25" in str(fn.get("type") or fn.get("function_type") or fn.get("name", "")).lower()
                    for fn in functions
                ):
                    raise MilvusUnavailable("existing Milvus collection lacks BM25 function")
                indexes = client.list_indexes(collection_name=self.config.collection)
                index_names = {
                    str(item.get("field_name") if isinstance(item, dict) else item)
                    for item in indexes
                }
                if not {"dense_vector", "sparse_vector"} <= index_names:
                    raise MilvusUnavailable("existing Milvus collection lacks required indexes")
            except MilvusUnavailable:
                raise
            except Exception as exc:
                raise MilvusUnavailable("existing Milvus collection could not be validated") from exc
            return
        sdk = self._sdk()
        dtype = sdk["DataType"]
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        fields = (
            ("chunk_id", dtype.VARCHAR, {"is_primary": True, "max_length": 128}),
            ("document_id", dtype.VARCHAR, {"max_length": 128}),
            ("document_version_id", dtype.VARCHAR, {"max_length": 128}),
            ("text", dtype.VARCHAR, {"max_length": 8192, "enable_analyzer": True}),
            ("sparse_vector", dtype.SPARSE_FLOAT_VECTOR, {}),
            ("dense_vector", dtype.FLOAT_VECTOR, {"dim": self.config.dimension}),
        )
        for name, datatype, options in fields:
            schema.add_field(field_name=name, datatype=datatype, **options)
        for name, length in (
            ("title", 512), ("source_uri", 2048), ("publisher", 256),
            ("source_type", 64), ("access_scope", 128), ("company", 128),
            ("symbol", 32), ("market", 16), ("period", 32), ("section", 512),
            ("published_at", 64), ("embedding_profile_id", 128), ("index_version", 128),
        ):
            schema.add_field(field_name=name, datatype=dtype.VARCHAR, max_length=length)
        schema.add_field(field_name="page", datatype=dtype.INT64)
        schema.add_field(field_name="authority_tier", datatype=dtype.INT64)
        schema.add_function(sdk["Function"](
            name="text_bm25", input_field_names=["text"],
            output_field_names=["sparse_vector"], function_type=sdk["FunctionType"].BM25,
        ))
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="dense_vector", index_type="AUTOINDEX", metric_type="IP")
        indexes.add_index(
            field_name="sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25",
        )
        client.create_collection(
            collection_name=self.config.collection, schema=schema, index_params=indexes,
        )

    def upsert(self, chunks: list[IndexedChunk]) -> None:
        if not chunks:
            return
        self.ensure_collection()
        rows = []
        for chunk in chunks:
            row = chunk.model_dump(exclude={"embedding"})
            row["dense_vector"] = chunk.embedding
            row["page"] = chunk.page or 0
            for name in ("company", "symbol", "market", "period", "section", "published_at"):
                row[name] = row.get(name) or ""
            rows.append(row)
        try:
            self._get_client().upsert(collection_name=self.config.collection, data=rows)
        except Exception as exc:
            raise MilvusUnavailable("Milvus upsert failed") from exc

    def delete_version(self, document_version_id: str) -> None:
        self._get_client().delete(
            collection_name=self.config.collection,
            filter=f"document_version_id == {_quote(document_version_id)}",
        )

    def health(self) -> dict:
        try:
            exists = self._get_client().has_collection(collection_name=self.config.collection)
            return {"ok": True, "backend": "milvus", "collection_exists": bool(exists)}
        except Exception:
            return {"ok": False, "backend": "milvus", "collection_exists": False}

    @staticmethod
    def _entity(hit: Any) -> dict[str, Any]:
        if isinstance(hit, dict):
            entity = hit.get("entity", hit)
            return dict(entity)
        entity = getattr(hit, "entity", None)
        return dict(entity or {})

    @staticmethod
    def _score(hit: Any) -> float:
        if isinstance(hit, dict):
            return float(hit.get("distance", hit.get("score", 0)))
        return float(getattr(hit, "distance", getattr(hit, "score", 0)))

    def _result(
        self, entity: dict[str, Any], *, request: RetrievalQuery, rank: int,
        fused_score: float, dense_score: float = 0, sparse_score: float = 0,
        dense_rank: int | None = None, sparse_rank: int | None = None,
    ) -> RetrievalResult:
        passthrough = {
            field: entity.get(field)
            for field in OUTPUT_FIELDS
            if field not in {"page", "embedding_profile_id", "index_version", "authority_tier"}
        }
        return RetrievalResult(
            **passthrough, page=entity.get("page") or None, fused_score=fused_score,
            dense_score=dense_score, sparse_score=sparse_score,
            dense_rank=dense_rank, sparse_rank=sparse_rank, rank=rank,
            embedding_profile_id=request.embedding_profile_id,
            index_version=request.index_version,
            authority_tier=int(entity.get("authority_tier") or 0),
        )

    def _fallback_search(
        self, client: Any, request: RetrievalQuery, vector: list[float], expression: str,
    ) -> RetrievalResponse:
        route_rows: dict[str, list[Any]] = {}
        errors: dict[str, str] = {}
        for route, data, field, metric in (
            ("dense", [vector], "dense_vector", "IP"),
            ("sparse", [request.query], "sparse_vector", "BM25"),
        ):
            try:
                route_rows[route] = client.search(
                    collection_name=self.config.collection, data=data, anns_field=field,
                    filter=expression, limit=request.candidate_k,
                    search_params={"metric_type": metric}, output_fields=OUTPUT_FIELDS,
                    timeout=self.config.timeout_seconds,
                )[0]
            except Exception:
                errors[route] = f"{route}_route_failed"
        if not route_rows:
            raise MilvusUnavailable("Milvus dense and BM25 search both failed")
        if len(route_rows) == 1:
            route = next(iter(route_rows))
            rows = route_rows[route][:request.top_k]
            results = [
                self._result(
                    self._entity(hit), request=request, rank=index + 1,
                    fused_score=self._score(hit),
                    dense_score=self._score(hit) if route == "dense" else 0,
                    sparse_score=self._score(hit) if route == "sparse" else 0,
                    dense_rank=index + 1 if route == "dense" else None,
                    sparse_rank=index + 1 if route == "sparse" else None,
                )
                for index, hit in enumerate(rows)
            ]
            mode = "dense_only" if route == "dense" else "bm25_only"
            missing = "sparse" if route == "dense" else "dense"
            return RetrievalResponse(
                backend="milvus", mode=mode, results=results, degraded=True,
                degraded_reason=errors.get(missing, f"{missing}_route_failed"),
            )
        merged: dict[str, dict[str, Any]] = {}
        for route in ("dense", "sparse"):
            for index, hit in enumerate(route_rows[route]):
                entity = self._entity(hit)
                chunk_id = str(entity["chunk_id"])
                item = merged.setdefault(chunk_id, {"entity": entity, "rrf": 0.0})
                item["rrf"] += 1 / (60 + index + 1)
                item[f"{route}_score"] = self._score(hit)
                item[f"{route}_rank"] = index + 1
        ordered = sorted(merged.values(), key=lambda item: (-item["rrf"], item["entity"]["chunk_id"]))
        results = [
            self._result(
                item["entity"], request=request, rank=index + 1,
                fused_score=item["rrf"], dense_score=item.get("dense_score", 0),
                sparse_score=item.get("sparse_score", 0), dense_rank=item.get("dense_rank"),
                sparse_rank=item.get("sparse_rank"),
            )
            for index, item in enumerate(ordered[:request.top_k])
        ]
        return RetrievalResponse(backend="milvus", mode="hybrid", results=results)

    def search(self, request: RetrievalQuery) -> RetrievalResponse:
        return self._search(request, allowed_chunk_ids=None)

    def search_authorized(
        self, request: RetrievalQuery, *, allowed_chunk_ids: list[str],
    ) -> RetrievalResponse:
        if not allowed_chunk_ids:
            raise ValueError("authorized Milvus search requires non-empty allowed IDs")
        return self._search(request, allowed_chunk_ids=allowed_chunk_ids)

    def _search(
        self, request: RetrievalQuery, *, allowed_chunk_ids: list[str] | None,
    ) -> RetrievalResponse:
        client = self._get_client()
        expression = build_filter(request, allowed_chunk_ids=allowed_chunk_ids)
        try:
            vector = self.embeddings.embed_queries([request.query]).vectors[0]
        except Exception:
            try:
                rows = client.search(
                    collection_name=self.config.collection, data=[request.query],
                    anns_field="sparse_vector", filter=expression,
                    limit=request.top_k, search_params={"metric_type": "BM25"},
                    output_fields=OUTPUT_FIELDS, timeout=self.config.timeout_seconds,
                )[0]
            except Exception as exc:
                raise MilvusUnavailable("embedding and BM25 search both failed") from exc
            return RetrievalResponse(
                backend="milvus", mode="bm25_only", degraded=True,
                degraded_reason="embedding_failed",
                results=[
                    self._result(
                        self._entity(hit), request=request, rank=index + 1,
                        fused_score=self._score(hit), sparse_score=self._score(hit),
                        sparse_rank=index + 1,
                    )
                    for index, hit in enumerate(rows)
                ],
            )
        sdk = self._sdk()
        common = {"limit": request.candidate_k, "expr": expression}
        dense_request = sdk["AnnSearchRequest"](
            data=[vector], anns_field="dense_vector", param={"metric_type": "IP"}, **common,
        )
        sparse_request = sdk["AnnSearchRequest"](
            data=[request.query], anns_field="sparse_vector", param={"metric_type": "BM25"}, **common,
        )
        try:
            rows = client.hybrid_search(
                collection_name=self.config.collection,
                reqs=[dense_request, sparse_request], ranker=sdk["RRFRanker"](60),
                limit=request.top_k, output_fields=OUTPUT_FIELDS,
                timeout=self.config.timeout_seconds,
            )[0]
        except Exception:
            return self._fallback_search(client, request, vector, expression)
        results = []
        for index, hit in enumerate(rows):
            entity = self._entity(hit)
            results.append(self._result(
                entity, request=request, rank=index + 1,
                fused_score=self._score(hit),
            ))
        return RetrievalResponse(backend="milvus", mode="hybrid", results=results)
