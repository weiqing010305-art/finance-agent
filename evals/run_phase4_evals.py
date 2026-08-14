from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.embeddings import EmbeddingBatch, EmbeddingProfile
from backend.evidence import EvidenceBuilder
from backend.reporting import CitationConstrainedReporter, ReportValidationError
from backend.retrieval import IndexedChunk, InMemoryHybridRetriever, RetrievalFilters, RetrievalQuery
from backend.schemas import ClaimCandidate
from backend.verifier import ClaimVerifier


CASES = Path(__file__).with_name("rag-retrieval-cases.json")


class EvalHashEmbeddings:
    """Test-only deterministic vectors. These are not BGE quality metrics."""
    profile = EmbeddingProfile(model_name="test-only-hash", revision="eval-v1", dimension=256, query_instruction="")

    def _embed(self, texts):
        vectors = []
        for text in texts:
            values = [0.0] * self.profile.dimension
            for index in range(max(0, len(text) - 1)):
                token = text[index:index + 2]
                values[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % len(values)] += 1
            norm = math.sqrt(sum(value * value for value in values)) or 1
            vectors.append([value / norm for value in values])
        return EmbeddingBatch(profile_id=self.profile.profile_id, vectors=vectors)

    embed_queries = _embed
    embed_documents = _embed


def _documents(embedding):
    rows = [
        ("tx_cashflow", "腾讯", "经营现金流持续改善，现金转换效率提高", 5),
        ("tx_profit_quality", "腾讯", "利润增长但应收账款上升，盈利质量存在恶化风险", 4),
        ("mt_contract_liability", "茅台", "合同负债较上年末增长，反映预收款变化", 5),
        ("tx_capex_2024", "腾讯", "0700.HK 2024年资本开支增加，主要投入算力基础设施", 5),
        ("noise", "腾讯", "公司发布新游戏并更新用户活动数据", 2),
    ]
    return [
        IndexedChunk(
            chunk_id=chunk_id, document_id="doc_" + chunk_id,
            document_version_id="ver_" + chunk_id, text=text, title=chunk_id,
            source_uri=f"https://example.com/{chunk_id}", publisher="交易所",
            source_type="filing", access_scope="public",
            embedding=embedding.embed_documents([text]).vectors[0],
            embedding_profile_id=embedding.profile.profile_id, index_version="eval-v1",
            company=company, authority_tier=authority,
        )
        for chunk_id, company, text, authority in rows
    ]


def _retrieval_metrics(cases, retriever, profile_id):
    reciprocal = []; recall = []; dcg_values = []
    failures = []
    for case in cases:
        response = retriever.search(RetrievalQuery(
            query=case["query"], top_k=3, candidate_k=5,
            filters=RetrievalFilters(company=case["company"]),
            embedding_profile_id=profile_id, index_version="eval-v1",
        ))
        ranked = [item.chunk_id for item in response.results]
        positions = [ranked.index(item) + 1 for item in case["relevant"] if item in ranked]
        recall.append(len(positions) / len(case["relevant"]))
        reciprocal.append(1 / min(positions) if positions else 0)
        dcg = sum(1 / math.log2(position + 1) for position in positions)
        ideal = sum(1 / math.log2(index + 2) for index in range(min(len(case["relevant"]), 3)))
        dcg_values.append(dcg / ideal if ideal else 1)
        if not positions:
            failures.append({"id": case["id"], "ranked": ranked})
    return {
        "recall_at_3": sum(recall) / len(recall),
        "mrr_at_3": sum(reciprocal) / len(reciprocal),
        "ndcg_at_3": sum(dcg_values) / len(dcg_values),
        "failures": failures,
    }


def evaluate(cases_path: Path = CASES) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    embedding = EvalHashEmbeddings()
    retriever = InMemoryHybridRetriever(embedding)
    retriever.upsert(_documents(embedding))
    retrieval = _retrieval_metrics(cases, retriever, embedding.profile.profile_id)

    evidence = EvidenceBuilder().build_retrieval_items("eval", [{
        "text": "2024年收入增长10%。", "source_uri": "https://example.com/report",
        "title": "年报", "publisher": "交易所", "authority_tier": 5, "period": "2024",
    }])[0]
    verifier = ClaimVerifier()
    claims = verifier.verify([
        ClaimCandidate(
            id="supported", run_id="eval", text="2024年收入增长10%",
            evidence_ids=[evidence.id], period="2024", unit="%",
        ),
        ClaimCandidate(
            id="unsupported", run_id="eval", text="2024年收入增长99%",
            evidence_ids=[evidence.id], period="2024", unit="%",
        ),
    ], [evidence], allowed_access_scopes={"public"})
    reporter = CitationConstrainedReporter()
    draft = reporter.build_deterministic(
        company="示例公司", question="收入", claims=claims, evidence=[evidence]
    )
    _markdown, _json_report, citations = reporter.render(draft, claims, [evidence])
    reportable = [item for item in claims if item.status in {"supported", "partially_supported"}]
    cited_claims = {item["claim_id"] for item in citations}
    return {
        "profile": "in_memory_test_smoke",
        "case_count": len(cases),
        **retrieval,
        "citation_coverage": len(cited_claims) / len(reportable) if reportable else 1.0,
        "citation_integrity": float(all(
            item["claim_id"] in {claim.id for claim in reportable}
            and item["evidence_id"] == evidence.id for item in citations
        )),
        "numeric_provenance_rate": float(claims[0].status == "supported" and claims[1].status == "unsupported"),
        "real_milvus_executed": False,
        "model_profile": embedding.profile.profile_id,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
