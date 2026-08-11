# Architecture Decision Records

本目录记录 FinScope Research 的关键架构决策。ADR 一经接受不原地改写历史；如果决策变化，新增 ADR 并将旧记录标记为 Superseded。

| ADR | 状态 | 决策 |
|---|---|---|
| [ADR-0001](0001-use-modular-monolith-and-sqlite-first.md) | Accepted | 模块化单体与 SQLite-first |
| [ADR-0002](0002-adopt-six-state-durable-runner.md) | Accepted | 六态 Durable Runner 与安全暂停 |
| [ADR-0003](0003-use-layered-intent-routing.md) | Accepted | 确定性优先的分层意图路由 |
| [ADR-0004](0004-separate-memory-rag-and-source-of-truth.md) | Accepted | 关系库、记忆、RAG 与对象存储边界 |
| [ADR-0005](0005-adopt-langgraph-langchain-hybrid-orchestration.md) | Accepted | LangGraph + LangChain 混合编排与 Durable Runner 边界 |
| [ADR-0006](0006-router-cannot-grant-external-tool-access.md) | Accepted | Router 不授予外部工具权限，授权延后到 Policy Gate |
| [ADR-0007](0007-separate-planning-authorization-and-execution.md) | Proposed | 分离 Planner、Policy Gate 与 Executor |

当前仍待决定：

- 是否增加独立的永久取消终态 `cancelled`；
- 受限 LLM 分类器达到什么量化门槛后启用；首版已决定默认关闭；
- 不同记忆类型的 TTL 和删除策略；
- FTS 基线达到什么量化条件后引入 embeddings。

这些决策均不阻塞 Phase 1；其 owner、最迟决策阶段和所阻塞能力记录在主架构文档第 23 节。
