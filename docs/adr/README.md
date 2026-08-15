# Architecture Decision Records

## Phase 6 accepted decisions

- [ADR-0011](0011-use-postgresql-rls-and-application-auth.md): PostgreSQL RLS, invitation authentication and fixed RBAC.
- [ADR-0012](0012-use-profiled-local-compose-and-s3-compatible-storage.md): profiled local Compose, Caddy, MinIO and shared Milvus.
- [ADR-0013](0013-use-dramatiq-ledger-jobs-and-opentelemetry.md): PostgreSQL job ledger, Dramatiq, observability and recovery.

本目录记录 FinScope Research 的关键架构决策。ADR 一经接受不原地改写历史；如果决策变化，新增 ADR 并将旧记录标记为 Superseded。

| ADR | 状态 | 决策 |
|---|---|---|
| [ADR-0001](0001-use-modular-monolith-and-sqlite-first.md) | Accepted | 模块化单体与 SQLite-first |
| [ADR-0002](0002-adopt-six-state-durable-runner.md) | Accepted | 六态 Durable Runner 与安全暂停 |
| [ADR-0003](0003-use-layered-intent-routing.md) | Accepted | 确定性优先的分层意图路由 |
| [ADR-0004](0004-separate-memory-rag-and-source-of-truth.md) | Accepted | 关系库、记忆、RAG 与对象存储边界 |
| [ADR-0005](0005-adopt-langgraph-langchain-hybrid-orchestration.md) | Accepted | LangGraph + LangChain 混合编排与 Durable Runner 边界 |
| [ADR-0006](0006-router-cannot-grant-external-tool-access.md) | Accepted | Router 不授予外部工具权限，授权延后到 Policy Gate |
| [ADR-0007](0007-separate-planning-authorization-and-execution.md) | Accepted | 分离 Planner、Policy Gate 与 Executor |
| [ADR-0008](0008-use-milvus-hybrid-retrieval.md) | Accepted | Milvus BM25 + dense embedding + RRF 混合检索 |
| [ADR-0009](0009-use-bge-large-zh-v1-5-embeddings.md) | Accepted | BGE Large 中文稠密向量模型与索引版本治理 |
| [ADR-0010](0010-govern-long-term-memory-lifecycle-and-retention.md) | Accepted | 长期记忆生命周期、作用域、TTL 与两阶段删除 |
| [ADR-0011](0011-use-postgresql-rls-and-application-auth.md) | Accepted | PostgreSQL RLS、应用认证与固定 RBAC |
| [ADR-0012](0012-use-profiled-local-compose-and-s3-compatible-storage.md) | Accepted | 分层本地 Compose 与 S3 兼容对象存储 |
| [ADR-0013](0013-use-dramatiq-ledger-jobs-and-opentelemetry.md) | Accepted | 持久任务账本、Dramatiq 与可观测性 |
| [ADR-0014](0014-run-authorized-rag-in-a-dedicated-worker-image.md) | Accepted | 专用 worker 执行 PostgreSQL 授权的真实本地 RAG |

当前仍待决定：

- 是否增加独立的永久取消终态 `cancelled`；
- 受限 LLM 分类器达到什么量化门槛后启用；首版已决定默认关闭；
- 记忆 TTL、作用域与删除策略已由 ADR-0010 确定；
- 是否启用独立 reranker；Milvus 混合检索与 BGE Large 中文 embedding 已确定。

这些决策均不阻塞 Phase 1；其 owner、最迟决策阶段和所阻塞能力记录在主架构文档第 23 节。
