# ADR-0002：采用六态 Durable Runner 与安全暂停

## Status

Accepted — 2026-08-10

## Context

用户明确要求六个运行状态：运行中、待暂停、已暂停、恢复中、执行失败、执行完成。每完成一个步骤，都要持久化任务状态、步骤输入输出、工具结果和下一执行指针。

当前代码使用 `queued / running / paused / completed / failed / cancelled`，暂停和恢复会直接修改任务状态，没有 `pause_requested`、`resuming`、步骤表、frontier 或 checkpoint，因此只是早期原型，不满足目标语义。

## Decision

运行主状态固定为：

```text
running
pause_requested
paused
resuming
failed
completed
```

- 创建请求经校验并落库后进入 `running`；“排队”只作为步骤调度属性，不作为 run 主状态；
- 暂停采用协作式安全暂停：请求先进入 `pause_requested`，不再领取新步骤，当前不可中断工具结束后原子保存结果和 checkpoint，再进入 `paused`；
- 恢复先进入 `resuming`，校验租约、计划版本、frontier 和未完成工具调用，再进入 `running`；
- 每个步骤完成后在同一事务中保存步骤、工具调用、预算、证据、事件和 checkpoint；事务失败则不得继续执行；
- 工具和步骤使用稳定幂等键；Runner 使用租约防止重复恢复；
- 永久取消暂不纳入这六态，另行决策。

状态迁移、CAS、lease 续租/抢占、重复控制请求与数据库不可写时的恢复契约，以[主架构文档第 8、9 节](../architecture/durable-research-agent.md#8-运行状态机)为规范来源，Phase 1 测试必须逐条覆盖。

## Consequences

### Positive

- 状态含义清晰，可展示真实的暂停和恢复过程；
- 崩溃后可以从持久化 frontier 恢复；
- 避免外部工具成功但执行指针未更新导致的重复调用。

### Negative

- 比直接修改一张 tasks 表复杂，需要迁移和并发测试；
- 不可中断工具会导致“待暂停”持续一段时间；
- 旧前端的“停止研究”按钮在取消语义确定前必须隐藏或暂映射为暂停，禁止向新 run 写入旧 `cancelled` 状态。

### Neutral

- 步骤状态独立于 run 状态；暂停 run 不表示步骤失败；
- `completed` 只有在报告、证据和最终 checkpoint 均提交后成立。

## Alternatives Considered

- **保留旧六态**：缺少待暂停和恢复中，无法表达安全边界；
- **立即采用七态并加入 cancelled**：语义合理，但会违反当前明确的六态要求；
- **事件溯源作为唯一存储模型**：审计强，但当前复杂度过高；本阶段使用关系表加 append-only events。

## References

- [主架构文档：运行状态机](../architecture/durable-research-agent.md#8-运行状态机)
- [Phase 1 验证记录](../reviews/phase-1-verification.md)

## Implementation Status

2026-08-11：六态状态机、CAS、lease、原子 checkpoint、启动恢复和 SSE 续传已实现；等待独立代码审查和用户阶段验收。旧 `/cancel` API 暂时映射为安全暂停，不会向新 run 写入 `cancelled`。
