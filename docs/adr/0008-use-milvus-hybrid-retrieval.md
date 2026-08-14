# ADR-0008：使用 Milvus 混合检索

## Status

Accepted

## Context

项目原计划以 SQLite FTS5 建立检索基线，只有评测证明收益后再引入向量检索。用户现已明确要求向量库使用 Milvus，并采用混合检索。金融研究既需要对公司名、证券代码、会计科目等精确关键词保持高召回，也需要理解“盈利质量恶化”“资本开支压力”等语义表达，因此单独使用关键词或稠密向量都不够稳健。

## Decision

Phase 4 使用 Milvus 作为可重建检索索引，默认检索链为：

```text
metadata filter
-> Milvus BM25 sparse retrieval
   + dense embedding retrieval
-> Reciprocal Rank Fusion
-> authority/freshness/diversity policy
-> Evidence Pack
```

Milvus collection 至少包含原始文本、稀疏向量字段、稠密向量字段，以及 document/version/chunk/company/market/period/document_type/page/section/source/hash 等过滤和溯源字段。默认融合使用 RRF，避免在缺少评测数据时主观指定权重；`WeightedRanker` 和独立 reranker 作为固定评测集上的候选。关系数据库和对象存储仍是文档、版本和权限的事实源，Milvus 索引损坏时必须可重建。

Phase 3 只冻结 `retrieve_documents` 的混合检索工具契约和 fake adapter；Milvus collection、摄取、embedding 和在线混合检索在 Phase 4 实现。Milvus 不用于六态运行状态、事务、用户权限或未经验证的长期记忆事实源。

## Consequences

### Positive

- 同时覆盖精确金融术语和语义改写；
- Milvus 原生支持 BM25 sparse、dense vector、hybrid search、RRF 和加权融合；
- 检索实现与关系事务隔离，索引可独立扩容和重建；
- 面试中可以展示完整的混合检索、消融评测和降级设计。

### Negative

- 本地启动增加 Milvus 服务、collection migration 和健康检查；
- embedding 模型、维度和版本必须被显式治理；
- 文档写库与索引存在最终一致性窗口，需要 ingestion job 和 reconciler；
- 需要单独测试 Milvus 不可用、稀疏失败和稠密失败的降级路径。

## Alternatives Considered

- SQLite FTS5 only：部署最简单，但不满足用户指定的向量库与混合检索目标。
- PostgreSQL FTS + pgvector：事务整合更紧，但用户已指定 Milvus，且独立检索层更便于扩展和重建。
- Dense-only Milvus：无法稳定处理代码、报表科目和专有名词，拒绝。
- WeightedRanker 默认：需要先有可靠权重评测，暂不作为默认。

## References

- [Milvus BM25 Function](https://milvus.io/docs/bm25-function.md)
- [Milvus Multi-Vector Hybrid Search](https://milvus.io/docs/multi-vector-search.md)
- [Milvus Reranking](https://milvus.io/docs/reranking.md)
- [ADR-0004](0004-separate-memory-rag-and-source-of-truth.md)
