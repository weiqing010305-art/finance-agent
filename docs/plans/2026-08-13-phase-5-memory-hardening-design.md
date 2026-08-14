# Phase 5 长期记忆与系统加固设计

## 1. 目标

Phase 5 建立可审计、可过期、可纠正、可删除的长期记忆系统，并补充故障注入、安全和成本评测。记忆用于跨 case 提供稳定偏好与经验证事实，但绝不替代当前研究证据。

完成后系统应支持：白名单候选写入、确定性验证、版本化取代、冲突隔离、TTL、作用域检索、Context Builder 注入、两阶段删除和后台协调。任何候选、冲突、过期或 tombstone 记忆都不能进入模型上下文。

## 2. 记忆类型与保留策略

| 类型 | 作用域 | 写入条件 | TTL |
|---|---|---|---|
| `company_fact` | 公开 `company/symbol/market` | Evidence 支持、期间/单位完整、Verifier 通过 | 90 天 |
| `entity_identity` | `tenant/user/case` 私有作用域 | 用户确认且与持久化 case 身份一致 | 180 天 |
| `user_preference` | `tenant/user` | 用户明确表达或确认 | 直到修改/删除 |
| `case_summary` | `tenant/user/case` | 已持久化 turn 的确定性摘要 | 30 天 |
| `task_experience` | `tenant/user` 或系统版本 | 只描述策略结果，不当公司事实 | 90 天 |
| `candidate/conflicted` | 原记忆作用域 | 等待验证或处理 | 7 天 |

程序记忆继续由 Git/配置版本管理，不允许线上 Agent 自动改写。

## 3. 数据模型与状态机

关系表包括：`memory_records`、`memory_versions`、`memory_evidence`、`memory_events`、`memory_deletion_jobs`。稳定 `memory_key` 表示同一事实槽，例如 `company:0700.HK:revenue_growth:2024`；每次内容变化创建新 version。

状态边：

```text
candidate -> verified -> active
candidate -> rejected / conflicted
active -> superseded / expired / deleted
conflicted -> verified / rejected / expired / deleted
```

禁止任意状态跳转和原地修改 active 内容。状态迁移、证据关系、旧版本 supersede 和事件必须在一个事务中提交。所有写入包含 idempotency key、内容哈希、scope hash、来源类型、置信度、valid_from、expires_at 和创建者类型。

## 4. 写入与冲突

`MemoryCandidateService` 只接受枚举类型。公司事实必须引用当前 run 的 supported/partially-supported Claim 与 Evidence；偏好必须带明确用户确认标志；摘要必须引用 case turn cursor。Verifier 在 Repository 持久化边界重新执行确定性检查，防止服务层被绕过。

同一 memory key：

- 内容哈希一致：幂等复用，附加新 Evidence，刷新允许刷新的 TTL；
- 不同期间：可并存为不同 key；
- 同期间不同值或互斥结论：新版本 conflicted，旧 active 同时变 conflicted 并停止注入；
- 新版本由更新期间或更可信证据验证通过：新 active，旧 superseded；
- 用户偏好纠正：新版本立即 active，旧版本 superseded，仍保留事件审计。

## 5. 检索与上下文注入

读取流程固定为：鉴权作用域 → `active` → tombstone=false → TTL → company/case/type 精确过滤 → 可选 Milvus hybrid 排序 → 新鲜度/置信度/证据质量排序 → 数量与 token 预算裁剪。

Context Builder 只注入结构化 envelope：`memory_id/type/content/evidence_ids/confidence/expires_at`，并标记为不可信参考数据。默认上限为 8 条、2000 字符；当前 run、最新 case 摘要和用户问题优先于长期记忆。最新数据问题即使命中长期事实，也必须保留 Planner/工具路径。

## 6. 删除与后台任务

删除请求单事务写 tombstone、把私人 active version 转 deleted、创建 deletion job 和匿名审计事件。读取查询首先过滤 tombstone，因此物理清理失败不会重新暴露数据。后台 job 使用 claim token、过期 fencing 和幂等删除。本阶段没有创建 memory 专用 Milvus/cache 派生索引，因此 worker 物理清理 SQLite 正文与关系；未来启用派生索引前必须增加 outbox/worker 精确 ID 清理合同。

公开公司事实的“用户删除”只删除该用户关联或隐藏设置，不删除共享事实。`clear my memory` 只覆盖 tenant/user 私人作用域。物理清理完成后审计表仅保存时间、操作类型、scope hash 和数量。

## 7. API 与可观察性

提供：记忆列表、创建显式偏好、纠正、删除单条、清空个人记忆、查看删除任务状态。普通研究事实由报告完成后的后台 consolidation 产生 candidate，不由客户端直接声明 verified。

指标至少包括 candidate acceptance、conflict、expired injection、cross-scope leakage、deletion latency、retrieval precision、context token cost 和 stale fact rate。所有读写事件带 reason code，不记录密钥或已删除正文。

## 8. 验收与范围边界

Phase 5 验收覆盖状态机、并发 CAS、幂等、TTL、冲突、跨 scope、上下文预算、untrusted-memory 标记、stale worker fencing 和固定评测。端到端 LLM prompt serializer、外部队列删除失败重试和 memory 派生索引清理由 Phase 6 在真实消费者/worker 存在时验收。真实 Milvus/BGE 门禁仍单独记录；离线 smoke 不能冒充生产质量。

Phase 5 不包含 PostgreSQL、多租户登录鉴权、对象存储和公网部署；这些属于 Phase 6。本阶段先用固定 local/default principal 验证所有 Repository 作用域不变量。
