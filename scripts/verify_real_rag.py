from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from statistics import mean
from uuid import uuid4

from backend.embeddings import BgeLargeZhEmbeddingProvider
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
from backend.retrieval import IndexedChunk, RetrievalFilters, RetrievalQuery


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "real-rag-cases.json"
DEFAULT_OUTPUT = ROOT / ".phase7-check" / "real-rag-result.json"


def _metrics(ranked: list[str], relevant: set[str], *, k: int) -> dict[str, float]:
    top = ranked[:k]
    hits = [1 if item in relevant else 0 for item in top]
    recall = sum(hits) / len(relevant)
    reciprocal_rank = next((1 / (index + 1) for index, hit in enumerate(hits) if hit), 0.0)
    dcg = sum(hit / math.log2(index + 2) for index, hit in enumerate(hits))
    ideal = sum(1 / math.log2(index + 2) for index in range(min(k, len(relevant))))
    return {"recall_at_3": recall, "mrr_at_3": reciprocal_rank, "ndcg_at_3": dcg / ideal}


def run_gate(*, uri: str, token: str | None, cases_path: Path) -> dict:
    from pymilvus import MilvusClient, __version__ as pymilvus_version
    import sentence_transformers
    import torch

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    collection = "finance_agent_real_gate_" + uuid4().hex
    device = "cuda" if torch.cuda.is_available() else "cpu"
    embeddings = BgeLargeZhEmbeddingProvider(device=device, batch_size=8)
    client = MilvusClient(uri=uri, token=token or None)
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri=uri, token=token or None, collection=collection),
        embeddings,
        client=client,
    )
    cleanup = False
    started = time.perf_counter()
    try:
        document_started = time.perf_counter()
        batch = embeddings.embed_documents([row["text"] for row in cases["corpus"]])
        document_seconds = time.perf_counter() - document_started
        chunks = []
        for row, vector in zip(cases["corpus"], batch.vectors, strict=True):
            chunk_id = row["chunk_id"]
            chunks.append(IndexedChunk(
                chunk_id=chunk_id,
                document_id="doc-" + chunk_id,
                document_version_id="version-" + chunk_id,
                text=row["text"], title=row["title"],
                source_uri=f"https://phase7.invalid/{chunk_id}",
                publisher="Phase 7 synthetic evaluation corpus",
                source_type="evaluation", access_scope=row["access_scope"],
                embedding=vector, embedding_profile_id=batch.profile_id,
                index_version=cases["index_version"], company=row["company"],
                authority_tier=1,
            ))
        upsert_started = time.perf_counter()
        retriever.upsert(chunks)
        client.flush(collection_name=collection)
        upsert_seconds = time.perf_counter() - upsert_started

        query_rows = []
        for case in cases["queries"]:
            query_started = time.perf_counter()
            response = retriever.search(RetrievalQuery(
                query=case["query"], top_k=3, candidate_k=8,
                filters=RetrievalFilters(company=case["company"], access_scope="public"),
                embedding_profile_id=batch.profile_id,
                index_version=cases["index_version"],
            ))
            ids = [item.chunk_id for item in response.results]
            if response.mode != "hybrid" or response.degraded:
                raise AssertionError(f"query did not use healthy hybrid retrieval: {case['query']}")
            if any(item.access_scope != "public" for item in response.results):
                raise AssertionError("access-scope filter leaked a private row")
            if any(item.chunk_id == "tencent-private" for item in response.results):
                raise AssertionError("private evaluation row leaked")
            score = _metrics(ids, set(case["relevant"]), k=3)
            query_rows.append({
                "query": case["query"], "company": case["company"],
                "result_ids": ids, "latency_ms": round((time.perf_counter() - query_started) * 1000, 3),
                **score,
            })

        wrong_profile = retriever.search(RetrievalQuery(
            query="现金流", top_k=3, candidate_k=8,
            filters=RetrievalFilters(company="腾讯", access_scope="public"),
            embedding_profile_id="wrong-profile", index_version=cases["index_version"],
        ))
        wrong_index = retriever.search(RetrievalQuery(
            query="现金流", top_k=3, candidate_k=8,
            filters=RetrievalFilters(company="腾讯", access_scope="public"),
            embedding_profile_id=batch.profile_id, index_version="wrong-index",
        ))
        wrong_company = retriever.search(RetrievalQuery(
            query="现金流", top_k=3, candidate_k=8,
            filters=RetrievalFilters(company="不存在的公司", access_scope="public"),
            embedding_profile_id=batch.profile_id, index_version=cases["index_version"],
        ))
        if wrong_profile.results or wrong_index.results or wrong_company.results:
            raise AssertionError("company/profile/index filter returned incompatible rows")

        averages = {
            name: mean(row[name] for row in query_rows)
            for name in ("recall_at_3", "mrr_at_3", "ndcg_at_3")
        }
        if averages["recall_at_3"] < 1.0 or averages["mrr_at_3"] < 0.75:
            raise AssertionError(f"real retrieval quality gate failed: {averages}")
        result = {
            "passed": True,
            "milvus_uri": uri,
            "collection": collection,
            "pymilvus_version": pymilvus_version,
            "sentence_transformers_version": sentence_transformers.__version__,
            "torch_version": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "embedding": embeddings.runtime_metadata(),
            "corpus_size": len(chunks),
            "document_embedding_seconds": round(document_seconds, 3),
            "upsert_and_flush_seconds": round(upsert_seconds, 3),
            "total_before_cleanup_seconds": round(time.perf_counter() - started, 3),
            "metrics": averages,
            "queries": query_rows,
            "filter_checks": {
                "company": True, "access_scope": True,
                "profile": True, "index_version": True,
            },
        }
    finally:
        if client.has_collection(collection_name=collection):
            client.drop_collection(collection_name=collection)
        cleanup = not client.has_collection(collection_name=collection)
    result["cleanup_verified"] = cleanup
    if not cleanup:
        raise AssertionError("gate collection cleanup was not verified")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real BGE + Milvus hybrid gate")
    parser.add_argument("--uri", default="http://127.0.0.1:19530")
    parser.add_argument("--token", default=None)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_gate(uri=args.uri, token=args.token, cases_path=args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
