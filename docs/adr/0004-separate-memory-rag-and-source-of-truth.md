# ADR-0004：分离关系事实源、记忆、RAG 与对象存储

## Status

Accepted — 2026-08-10

## Context

Agent 同时需要恢复运行、理解后续追问、复用经验证公司事实和检索财报文档。这些数据的生命周期、可信度和访问方式不同。如果全部保存为聊天文本或向量，难以执行事务、删除用户数据、处理事实过期和复现报告。

## Decision

- 关系数据库是运行、计划、步骤、证据、报告和记忆状态的唯一事实源；
- Working Memory 存在 checkpoint，生命周期为单次 run；
- Short-term Memory 以 case 为范围，保存摘要、实体、纠正、已研究问题和待处理追问；
- Long-term Memory 分为经验证公司事实、任务经验、显式用户偏好和版本化程序记忆；
- 公开公司事实可以共享，用户偏好和私人 case 必须隔离；
- 长期事实经过 candidate、来源检查、去重、时效检查和 verifier 后才能成为 `verified`；
- 文档正文和大文件保存在文件系统/对象存储，数据库保留不可变版本与哈希；
- 本地 RAG 从 SQLite FTS5 和结构化过滤开始；向量、RRF、rerank 和 pgvector 必须由评测证明收益后再引入。

## Consequences

### Positive

- 状态恢复、隐私删除、事实取代和报告复现都有明确边界；
- 向量索引损坏时可以从关系库和文档快照重建；
- 避免网页提示注入直接污染长期记忆。

### Negative

- 需要维护文档摄取、记忆验证和索引同步流程；
- Context Builder 必须在多种数据类型中控制 token 和优先级；
- 公司事实共享需要严格区分公开信息与用户私有内容。

### Neutral

- Embedding 模型暂不选型；
- TTL 按数据类型制定，在公开部署前形成新的治理 ADR。

## Alternatives Considered

- **所有内容保存到向量数据库**：事务和治理能力不足，索引难以成为可靠事实源；
- **只保存完整聊天历史**：上下文持续膨胀，事实状态和来源难查询；
- **第一版直接混合向量检索**：可能提升语义召回，但没有基线评测证明值得增加复杂度。

## References

- [主架构文档：记忆系统](../architecture/durable-research-agent.md#10-记忆系统)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [pgvector](https://github.com/pgvector/pgvector)
