# ADR-0007：分离 Planner、Policy Gate 与 Executor

## Status

Proposed

## Context

Phase 3 要把研究意图连接到实体解析、动态计划和工具执行。如果 Planner 可以直接调用工具、修改状态或决定预算，模型输出错误会绕过 Phase 1 的持久化和租约边界，也会破坏 ADR-0006 的延迟授权原则。

## Decision

采用三段式边界：Planner 只产生版本化、严格 Schema 的 DAG；Policy/Budget Gate 根据已确认实体、路由事实、工具风险和预算产生逐步授权；Executor 只执行已授权的注册工具，并通过 Durable Runner 原子提交结果。实体解析采用确定性证券目录和别名优先，歧义必须持久化并由用户确认。Planner 可替换为模型实现，但输出必须通过同一 DAG 校验器；非法输出只允许修复一次，之后回落到确定性计划。自动重规划最多一次。

## Consequences

### Positive

- 模型不能绕过权限、预算、lease 和 checkpoint；
- Planner、工具和执行器可分别评测和替换；
- 同一计划和授权决策可审计、回放和恢复；
- 无模型或模型失败时仍有确定性降级路径。

### Negative

- 需要额外的 intake、确认、计划安装和授权账本；
- 执行链比直接调用模型更复杂；
- Phase 3 需要为并发、幂等和失败矩阵增加更多测试。

## Alternatives Considered

- Planner 直接调用工具：实现快，但无法可靠实施权限、预算和恢复，拒绝。
- 把所有逻辑放进单个 LangGraph 节点：状态边界模糊且难以独立评测，拒绝。
- 一开始拆成多个微服务：对本地 Demo 运维成本过高，继续采用模块化单体。

## References

- [ADR-0002](0002-adopt-six-state-durable-runner.md)
- [ADR-0005](0005-adopt-langgraph-langchain-hybrid-orchestration.md)
- [ADR-0006](0006-router-cannot-grant-external-tool-access.md)
