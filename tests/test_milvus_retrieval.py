from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.embeddings import EmbeddingBatch, EmbeddingProfile
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever, build_filter
from backend.retrieval import IndexedChunk, RetrievalFilters, RetrievalQuery


class FakeEmbeddings:
    profile = EmbeddingProfile()
    def embed_queries(self, texts):
        return EmbeddingBatch(profile_id=self.profile.profile_id, vectors=[[1.0] + [0.0] * 1023])
    embed_documents = embed_queries


class FakeSchema:
    def __init__(self): self.fields = []; self.functions = []
    def add_field(self, **kwargs): self.fields.append(kwargs)
    def add_function(self, value): self.functions.append(value)


class FakeIndexes:
    def __init__(self): self.items = []
    def add_index(self, **kwargs): self.items.append(kwargs)


class FakeClient:
    def __init__(self):
        self.schema = FakeSchema(); self.indexes = FakeIndexes(); self.created = None
        self.upserts = []; self.hybrid = None
    def has_collection(self, **kwargs): return False
    def create_schema(self, **kwargs): return self.schema
    def prepare_index_params(self): return self.indexes
    def create_collection(self, **kwargs): self.created = kwargs
    def upsert(self, **kwargs): self.upserts.append(kwargs)
    def hybrid_search(self, **kwargs):
        self.hybrid = kwargs
        return [[{"distance": 0.9, "entity": {
            "chunk_id": "c1", "document_id": "d1", "document_version_id": "v1",
            "text": "现金流改善", "title": "报告", "source_uri": "https://example.com/a",
            "publisher": "交易所", "source_type": "filing", "access_scope": "public",
            "page": 2, "section": "现金流", "authority_tier": 5, "published_at": "2025",
            "embedding_profile_id": FakeEmbeddings.profile.profile_id, "index_version": "idx-v1",
        }}]]


class ExistingClient(FakeClient):
    def __init__(self, *, dim=1024, bm25=True):
        super().__init__(); self.dim = dim; self.bm25 = bm25
    def has_collection(self, **kwargs): return True
    def describe_collection(self, **kwargs):
        return {
            "fields": [
                {"name": "chunk_id"}, {"name": "text"}, {"name": "sparse_vector"},
                {"name": "dense_vector", "params": {"dim": self.dim}},
                {"name": "embedding_profile_id"}, {"name": "index_version"},
                {"name": "access_scope"},
            ],
            "functions": [{"name": "text_bm25", "type": "BM25"}] if self.bm25 else [],
        }
    def list_indexes(self, **kwargs): return ["dense_vector", "sparse_vector"]


class FallbackClient(FakeClient):
    def __init__(self, failed_route):
        super().__init__(); self.failed_route = failed_route
    def hybrid_search(self, **kwargs): raise RuntimeError("hybrid unavailable")
    def search(self, **kwargs):
        route = "dense" if kwargs["anns_field"] == "dense_vector" else "sparse"
        if route == self.failed_route or self.failed_route == "both":
            raise RuntimeError("route failed")
        return [[{"distance": 0.7, "entity": {
            "chunk_id": "c1", "document_id": "d1", "document_version_id": "v1",
            "text": "现金流改善", "title": "报告", "source_uri": "https://example.com/a",
            "publisher": "交易所", "source_type": "filing", "access_scope": "public",
            "page": 1, "section": "现金流", "authority_tier": 5, "published_at": "2025",
            "embedding_profile_id": FakeEmbeddings.profile.profile_id, "index_version": "idx-v1",
        }}]]


def _sdk():
    dtype = SimpleNamespace(VARCHAR="VARCHAR", SPARSE_FLOAT_VECTOR="SPARSE", FLOAT_VECTOR="FLOAT", INT64="INT64")
    return {
        "DataType": dtype,
        "FunctionType": SimpleNamespace(BM25="BM25"),
        "Function": lambda **kwargs: kwargs,
        "AnnSearchRequest": lambda **kwargs: kwargs,
        "RRFRanker": lambda k: {"rrf_k": k},
    }


def test_collection_has_bm25_dense_1024_and_versioned_metadata():
    client = FakeClient()
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri="http://milvus:19530", token=None), FakeEmbeddings(),
        client=client, sdk_factory=_sdk,
    )
    chunk = IndexedChunk(
        chunk_id="c1", document_id="d1", document_version_id="v1", text="现金流",
        title="报告", source_uri="https://example.com", publisher="交易所",
        source_type="filing", access_scope="public", embedding=[1.0] + [0.0] * 1023,
        embedding_profile_id=FakeEmbeddings.profile.profile_id, index_version="idx-v1",
    )
    retriever.upsert([chunk])
    dense = next(field for field in client.schema.fields if field["field_name"] == "dense_vector")
    text = next(field for field in client.schema.fields if field["field_name"] == "text")
    assert dense["dim"] == 1024 and text["enable_analyzer"] is True
    assert client.schema.functions[0]["function_type"] == "BM25"
    assert {item["metric_type"] for item in client.indexes.items} == {"IP", "BM25"}
    assert client.upserts[0]["data"][0]["embedding_profile_id"] == FakeEmbeddings.profile.profile_id


def test_hybrid_search_uses_two_requests_rrf_and_fail_closed_filter():
    client = FakeClient()
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri="http://milvus:19530", token=None), FakeEmbeddings(),
        client=client, sdk_factory=_sdk,
    )
    request = RetrievalQuery(
        query="现金流", top_k=5, candidate_k=20,
        filters=RetrievalFilters(company='腾讯" or true', market="HK", access_scope="public"),
        embedding_profile_id=FakeEmbeddings.profile.profile_id, index_version="idx-v1",
    )
    response = retriever.search(request)
    assert len(client.hybrid["reqs"]) == 2 and client.hybrid["ranker"] == {"rrf_k": 60}
    assert '腾讯\\" or true' in client.hybrid["reqs"][0]["expr"]
    assert response.results[0].chunk_id == "c1"
    assert response.results[0].embedding_profile_id == FakeEmbeddings.profile.profile_id
    assert "access_scope" in build_filter(request)


def test_result_preserves_milvus_profile_and_index_for_identity_fencing():
    class DriftedClient(FakeClient):
        def hybrid_search(self, **kwargs):
            rows = super().hybrid_search(**kwargs)
            rows[0][0]["entity"]["embedding_profile_id"] = "drifted-profile"
            rows[0][0]["entity"]["index_version"] = "drifted-index"
            return rows

    response = MilvusHybridRetriever(
        MilvusConfig(uri="x", token=None), FakeEmbeddings(),
        client=DriftedClient(), sdk_factory=_sdk,
    ).search(RetrievalQuery(
        query="现金流", top_k=5, candidate_k=20,
        embedding_profile_id=FakeEmbeddings.profile.profile_id, index_version="idx-v1",
    ))
    assert response.results[0].embedding_profile_id == "drifted-profile"
    assert response.results[0].index_version == "drifted-index"


def test_one_route_failure_is_explicitly_degraded_and_both_fail_closed():
    import pytest
    from backend.milvus_retrieval import MilvusUnavailable
    request = RetrievalQuery(
        query="现金流", top_k=5, candidate_k=20,
        embedding_profile_id=FakeEmbeddings.profile.profile_id, index_version="idx-v1",
    )
    dense_only = MilvusHybridRetriever(
        MilvusConfig(uri="x", token=None), FakeEmbeddings(),
        client=FallbackClient("sparse"), sdk_factory=_sdk,
    ).search(request)
    assert dense_only.degraded and dense_only.mode == "dense_only"
    with pytest.raises(MilvusUnavailable):
        MilvusHybridRetriever(
            MilvusConfig(uri="x", token=None), FakeEmbeddings(),
            client=FallbackClient("both"), sdk_factory=_sdk,
        ).search(request)


def test_embedding_failure_uses_explicit_bm25_only_fallback():
    class BrokenEmbeddings(FakeEmbeddings):
        def embed_queries(self, texts): raise RuntimeError("model down")
    request = RetrievalQuery(
        query="现金流", top_k=5, candidate_k=20,
        embedding_profile_id=FakeEmbeddings.profile.profile_id, index_version="idx-v1",
    )
    response = MilvusHybridRetriever(
        MilvusConfig(uri="x", token=None), BrokenEmbeddings(),
        client=FallbackClient("dense"), sdk_factory=_sdk,
    ).search(request)
    assert response.mode == "bm25_only" and response.degraded
    assert response.degraded_reason == "embedding_failed"


def test_existing_collection_schema_is_validated_fail_closed():
    from backend.milvus_retrieval import MilvusUnavailable
    valid = MilvusHybridRetriever(
        MilvusConfig(uri="x", token=None), FakeEmbeddings(),
        client=ExistingClient(), sdk_factory=_sdk,
    )
    valid.ensure_collection()
    with pytest.raises(MilvusUnavailable, match="dimension"):
        MilvusHybridRetriever(
            MilvusConfig(uri="x", token=None), FakeEmbeddings(),
            client=ExistingClient(dim=768), sdk_factory=_sdk,
        ).ensure_collection()
