# Phase 3 Verification

> 日期：2026-08-12
> 状态：独立 subagent 第五轮审查通过，用户已验收

## 已实现

- SQLite schema v9：`research_intakes`、`entity_confirmations`、`execution_authorizations`、`tool_execution_claims`、`execution_authorization_attempts`；
- 确定性证券目录 Resolver：唯一代码/别名解析、多市场歧义确认、未知和错别字 fail closed；
- LangGraph intake 图：`load_route -> resolve_entity -> persist_intake`；
- intake request identity、确认过期、并发单赢家和 intake/run 原子绑定；
- 严格 Pydantic `ResearchPlan` DAG：唯一 ID、依赖存在、无环、重试和成本边界；
- 动态 quick/standard/deep 计划，版本化安装并原子更新 frontier/checkpoint/event；
- 六个版本化 ToolSpec；`retrieve_documents` 固定 Milvus BM25 sparse + dense + RRF 契约；
- Router/实体/工具风险/预算 Policy Gate，allow/deny 均持久化；
- ready frontier 工具并发、整体预算预留、逐步原子提交、暂停安全点和恢复去重；
- 证据不足最多重规划一次，再不足则失败；
- FastAPI intake、查询、确认 API；旧 `/api/research` 保持兼容；
- Phase 3 run 重启后回到 Phase 3 Executor，不会错接旧 DeepSeek/mock worker；
- Phase 3 只执行到 95% `awaiting_report` 边界，Phase 4 才提交证据验证后的最终报告。

## 验证结果

```text
python -m pytest -q -p no:cacheprovider --basetemp .phase3-final-main
244 passed, 1 warning in 13.68s
```

唯一 warning 是现有 Starlette TestClient/httpx 兼容层弃用提醒。

```text
python -m compileall -q backend tests evals
git diff --check
```

两项通过。

## 核心安全门禁

- Router 的 `external_research_allowed` 始终 false；Policy Gate 独立决定逐步授权；
- 非研究路由不能创建 intake；未确认实体不能创建 run 或调用工具；
- 歧义证券不会静默选择；确认只有一个并发赢家；
- 未注册工具、低报工具成本、预算不足、`requires_confirmation` 或高风险未确认均 fail closed；允许决定会原子预留预算并签发只存哈希的能力令牌；
- 同 frontier 总成本先整体检查，避免多个步骤分别看见同一份预算；
- 工具没有 Repository、lease 或状态机引用；只返回受 Schema 约束的 observation；
- 计划历史不可覆盖，自动重规划最多一次；
- 调用前先持久化 claim，结果先持久化为 observation，再由当前 lease 持有者原子提交 tool call、step、预算、frontier 与 checkpoint；并发 executor 不能同时取得同一步的执行 token。
- 已落库 observation 在恢复时直接复用。外部 provider 若支持稳定幂等键，可覆盖“远端成功、本地 observation 未落库”的窗口；不支持时语义为受控 at-least-once，不承诺跨系统绝对 exactly-once。

## Milvus 边界

根据 ADR-0008，目标检索链为 Milvus BM25 sparse + dense embedding + RRF。Phase 3 已实现严格工具输入输出、融合/版本元数据与未配置降级；真实 Milvus collection、embedding、摄取、索引重建和检索评测属于 Phase 4。

## 已知限制

- 实体目录当前是本地小型基线，不是完整证券主数据；Phase 6 前需接入可版本化证券主数据源；
- Phase 3 默认 Tool Registry 使用可控未配置 handler，以验证编排和故障边界；真实 filings/web/Milvus adapter 尚待接入；
- Planner 当前为确定性 fallback；模型 Planner 只有在固定评测证明收益后才启用；
- 当前 Executor 并发调用工具、串行提交 SQLite checkpoint；多 Worker 并发调度留到 PostgreSQL 阶段；
- Phase 3 不生成最终报告，任务保持 running/95%，由 Phase 4 继续处理；
- Milvus 和 embedding 模型尚未加入运行依赖，避免 Phase 3 本地开发被外部服务阻塞。
