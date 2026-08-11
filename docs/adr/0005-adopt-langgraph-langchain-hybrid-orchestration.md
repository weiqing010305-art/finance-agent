# ADR-0005：采用 LangGraph + LangChain 混合编排

## Status

Accepted — 2026-08-11

## Context

FinScope 已在 Phase 1 实现六态 Durable Runner、lease fencing、CAS、原子 checkpoint、暂停恢复和审计事件。后续还需要多轮路由、短期记忆、动态 Planner DAG、条件分支、重新规划和工具调用。完全自研图编排会增加维护成本；完全用框架替换 Runner 又会丢失已验证的业务状态、并发和审计契约。

## Decision

采用混合架构：

- FastAPI 继续作为 HTTP/SSE、校验和认证入口；
- LangGraph `StateGraph` 负责 Agent 节点、条件边和执行图；
- LangChain 负责消息、模型、工具 Schema 和 middleware 集成；
- Durable Runner 继续作为业务运行生命周期的唯一事实源，负责六态、lease、预算、幂等和审计；
- Repository/SQLite 继续保存 case、turn、summary、run 和业务 checkpoint；Phase 6 再迁 PostgreSQL；
- Phase 2 不启用第二套 LangGraph 持久化数据库。Graph state 必须能从 Repository 的持久化上下文和 frontier 重建，避免双 checkpoint 漂移；
- `run_id` 预留为未来 LangGraph `thread_id`。若后续评测证明需要原生 checkpointer，必须新增 ADR，明确事务边界、恢复优先级和数据迁移。

职责边界：Durable Runner 决定“任务能否继续执行”，LangGraph 决定“下一节点是什么”，LangChain 决定“节点如何调用模型和工具”。节点不得直接更新 run 状态、预算或内部 lease。

## Consequences

### Positive

- 保留 Phase 1 已验证的可靠性能力；
- 使用 LangGraph 表达 Router、Planner、Verifier 和 replan 分支；
- 模型和工具可以通过 LangChain 标准接口替换；
- 面试时可以展示框架能力与自研可靠性边界。

### Negative

- 团队需要维护框架 state 与业务 projection 的映射；
- LangGraph 原生 persistence 暂不作为恢复事实源；
- 框架升级需要契约测试和固定版本。

### Neutral

- Phase 2 的路由仍是确定性优先；引入 LangGraph 不代表所有节点都调用 LLM；
- LangChain 的模型与真实 Tool 主链从 Phase 3 开始发挥主要作用。

## Alternatives Considered

- **LangGraph 完全替换 Durable Runner**：迁移风险高，会重复解决已经通过故障测试的问题；
- **只使用 LangChain `create_agent`**：抽象过高，不利于表达确认、预算、暂停和重新规划；
- **继续完全自研编排**：控制力强，但图分支、可视化和生态集成成本更高。

## References

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview)
- [Phase 1 验证记录](../reviews/phase-1-verification.md)
