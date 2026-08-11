# Phase 1 Verification

> 日期：2026-08-11
> 状态：独立 subagent 最终复核通过，用户已于 2026-08-11 验收

## 已实现

- 版本化 SQLite migration，fresh schema 和 legacy `tasks` schema 都可升级；迁移失败整体回滚；
- `agent_runs`、`plans`、`run_steps`、`tool_calls`、`checkpoints`、`run_leases`、`events` 和 `evidence`；
- 六个运行状态：`running`、`pause_requested`、`paused`、`resuming`、`failed`、`completed`；
- `state_version` CAS、稳定幂等键、初始 lease、续租、过期后抢占和 owner 隔离；
- run、plan、初始 checkpoint、lease 和 `run.started` 单事务创建；
- 步骤输入输出、工具结果、预算、frontier、checkpoint 和事件单事务提交；
- checkpoint 失败整笔回滚，执行指针不会前移；
- 安全暂停：请求先进入 `pause_requested`，当前步骤提交后进入 `paused`；
- 恢复：`paused -> resuming -> running`，事件完整持久化；
- startup reconciler 接管 lease 缺失/过期 run，保留尚未完成的暂停意图；
- Mock worker 从 checkpoint 的 `completed_step_ids` 继续，不重复已提交步骤；
- API `Idempotency-Key`，同请求返回原 run，不同请求返回 `409`；
- SSE `Last-Event-ID` 只回放游标之后的持久化事件；
- DeepSeek 长调用 lease heartbeat；报告 delta 改为架构统一事件 `report.delta`；
- DeepSeek 已提交的 provider 结果从步骤 checkpoint 复用，不会在最终报告提交前的恢复中重复调用；
- 旧 `/cancel` 兼容端点映射为安全暂停，不产生第七状态。

## 验证结果

```text
python -m pytest -q
74 passed, 1 warning in 4.66s
```

唯一 warning 来自 Starlette TestClient 对当前 httpx 兼容层的弃用提醒，不影响本 Phase 功能。

覆盖的关键故障与并发边界：

- stale `state_version`；
- 错误或过期 lease token；
- lease 未过期时禁止抢占；
- 重复暂停、恢复和创建；
- checkpoint 插入失败事务回滚；
- pause 与步骤完成竞争；
- pause 与 lease takeover 的真实双线程竞争（连续运行 10 次）；
- 进程重启后的过期 lease 接管；
- checkpoint 损坏后可审计失败；
- legacy migration 失败回滚；
- 完成后的幂等重试；
- SSE 断点回放。

## 第一轮独立审查与修复

独立 subagent 第一轮结论为“不通过”。已针对其发现完成：

- 跨进程暂停后恢复会重新创建 worker，并新增真实双 TestClient 重启测试；
- 所有运行期事件、实体和证据写入增加未过期 lease fencing；
- takeover 在同一 `BEGIN IMMEDIATE` 事务读取真实前态，避免暂停意图 TOCTOU；
- 删除可任意写状态的 `update_task`，限制合法迁移边，并用 SQLite trigger 拒绝六态外状态；
- legacy 活跃/暂停 run 以及早期 Phase 1 v1 数据回填 plan、frontier 和 checkpoint；
- DeepSeek 恢复优先读取已提交 provider step output；
- paused、running、completed 三种幂等创建重试均可返回原 run；
- 步骤幂等指纹、progress、预算、frontier 与 plan version 增加校验；
- 非法 SSE cursor 返回 `400`；证据 enrich 形成 checkpoint 与审计事件；
- provider 错误与 URL 敏感参数在持久化前脱敏；
- 前端只展示六态，并将“停止”交互改为安全暂停/恢复。

## 第二轮独立审查与修复

第二轮复核继续发现了恢复校验、幂等完整性与安全边界问题。现已完成：

- API 手动恢复与 startup reconciler 共用同一个 checkpoint、plan、frontier、预算、步骤和工具账本校验器；校验失败进入可审计的 `failed`，不会启动 worker；
- provider 事件的 URL 同时清洗结构化 payload 和可见 message，覆盖常见云厂商签名、credential 与 token 参数；
- 步骤幂等指纹覆盖 step id、输入输出、frontier、progress、预算增量和工具调用，复用同一 key 但载荷不同会明确冲突；
- SQLite schema v3 在数据库层约束六态合法迁移、完成/失败不变量，并为带 `recovery_required` 的过期 lease 接管保留唯一恢复边；
- 完成后的证据元数据补全保留原 checkpoint state，以 before/after hash 和新 checkpoint 审计，不重开终态执行；
- evidence enrich 在任何外部请求前验证 run 已完成；页面元数据抓取增加 DNS 解析后的私网/环回/保留地址拒绝；
- 旧 provider step 输出若不符合新契约，会使用新版 step/key 安全重跑，避免既崩溃又误当成新结果。
- 恢复时会依次识别旧版与 v2 provider checkpoint；v2 已提交而最终报告尚未提交时直接复用，不再次调用外部 provider；
- SQLite schema v4 禁止终态 run 的 result、progress 和 error 被绕过 Runner 直接改写。

## 已知限制

- SQLite 目标仍是单机/低写并发，不支持多实例生产部署；
- DeepSeek 外部调用若在“远端已完成、provider 步骤尚未提交”窗口崩溃，恢复后仍可能重新发起该调用；已提交的 provider 步骤不会重复。Phase 3 需要以更细粒度工具调用账本和 provider 幂等能力进一步缩小窗口；
- `resuming` 会被持久化并产生事件，但当前 API 在校验成功后立即转回 `running`，通常只在事件时间线中可见；
- 永久取消仍待产品 ADR；当前停止操作只执行安全暂停；
- Provider 进度事件会持久化，但 run 的进度投影只在 checkpoint 时更新；
- 元数据抓取已阻止显式私网地址和解析到非公网的域名；更强的 DNS rebinding 防护需要后续将解析结果绑定到实际连接或统一经过受控抓取代理；
- 本阶段不包含短期记忆、意图路由、Planner DAG 或真实研究工具注册，它们属于后续 Phase。

## 阶段验收门禁

1. 独立 subagent 检查迁移安全、状态机、并发、恢复、API 和测试缺口；
2. 主 Agent 修复问题并重新运行全量测试；
3. subagent 复核通过；
4. 用户明确确认后才进入 Phase 2。

最终独立验收结论（2026-08-11）：建议 Phase 1 通过，无剩余阻塞项。非阻塞残余仅为已记录的 DNS rebinding 与 provider 远端完成但本地步骤尚未提交窗口。
