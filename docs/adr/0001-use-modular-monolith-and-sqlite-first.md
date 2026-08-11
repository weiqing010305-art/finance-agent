# ADR-0001：采用模块化单体与 SQLite-first

## Status

Accepted — 2026-08-10

## Context

当前项目是单用户本地 Demo，需要优先证明研究链、持久化恢复、证据验证和评测能力。现阶段没有真实多用户并发或独立扩缩容需求。过早引入微服务、消息队列和独立向量数据库会增加启动、调试、部署和面试复现成本。

运行状态、步骤、检查点、证据、报告和记忆之间存在强事务关系，要求数据库提交成功后才能继续执行。

## Decision

- 采用模块化单体，通过明确的 Python 模块、接口、Repository 和事务划分边界；
- SQLite 是本地阶段的关系型事实源，开启 foreign keys 和 WAL；
- 大型 PDF、HTML 与解析产物保存在文件系统，数据库仅保存 URI、哈希、版本和元数据；
- 从第一版预留 `user_id`、`tenant_id` 和版本字段；
- 公开部署或真实多用户并发出现后，迁移 PostgreSQL；
- 不在当前阶段引入消息队列、微服务或独立向量数据库。

## Consequences

### Positive

- 可用单命令启动和测试，适合作品演示；
- ACID 事务可以保证步骤、事件和检查点的一致性；
- 降低运维复杂度，把时间投入到 Agent 核心能力。

### Negative

- SQLite 写并发有限，不适合多实例公开服务；
- 迁移 PostgreSQL 时需要处理 SQL 方言和并发语义差异；
- 后台摄取与在线执行目前共享进程资源。

### Neutral

- Repository 层必须避免把 SQLite 特有行为泄漏到业务逻辑；
- 后续是否拆服务由指标和故障隔离需求决定。

## Alternatives Considered

- **直接使用 PostgreSQL**：能力足够，但本地配置和运维成本高于当前收益；
- **微服务 + 队列**：可独立扩缩容，但没有当前规模证据，调试和恢复链更复杂；
- **纯内存状态**：实现简单，但无法满足暂停、崩溃恢复和审计要求。

## References

- [主架构文档](../architecture/durable-research-agent.md)
- [SQLite WAL](https://www.sqlite.org/wal.html)
