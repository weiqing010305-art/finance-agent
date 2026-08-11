# ADR-0006：Router 不授予外部工具权限

## Status

Accepted — 2026-08-11

## Context

Phase 2 的 Router 使用确定性规则和短期上下文识别用户意图。中文中的“研究生”“分析师”、报告追问和 Agent 能力问题可能包含研究动词。如果 Router 将研究意图直接等同于工具授权，分类误差就会立即变成外部调用。

## Decision

- Router 只输出是否需要进入 Planner；
- Phase 2 所有 `RouteDecision.external_research_allowed` 固定为 `false`；
- `RESEARCH_NEW` 和 `RESEARCH_FOLLOWUP` 可以设置 `requires_planner=true`，但不能调用工具；
- Phase 3 必须在实体确认、权限、预算和运行状态门禁通过后产生独立执行授权；
- Pydantic Schema 拒绝任何 Router `external_research_allowed=true`，防止未来 Graph 或模型绕过边界。

## Consequences

### Positive

- 意图分类误差不会直接触发外部研究；
- Router、Planner 和 Policy Gate 的职责清晰；
- 可以分别评估 Planner 误触发率和工具授权错误率。

### Negative

- 研究请求多一次 Policy Gate；
- Phase 3 接入前，路由端点只返回意图，不自动研究。

## Alternatives Considered

- **Router 对研究追问直接授权**：延迟低，但自然语言误判会直接产生工具副作用；
- **仅增加更多关键词排除**：可以改善基线，但无法从结构上消除未知表达的权限风险。

## References

- [ADR-0003：分层意图路由](0003-use-layered-intent-routing.md)
- [主架构文档：路由契约](../architecture/durable-research-agent.md#53-路由契约)
