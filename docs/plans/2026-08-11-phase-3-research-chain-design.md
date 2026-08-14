# Phase 3 Research Chain Design

## Requirements

Phase 3 接收已经判定为研究类的路由结果，完成公司/证券实体解析、必要确认、版本化 DAG 规划、权限与预算检查、注册工具执行、观察结果提交和最多一次重规划。任何节点都不能绕过 Durable Runner 直接修改六态；Router 也不能授予工具权限。

非功能要求：同 request ID 可安全重放；确认只能消费一次；Planner 输出必须可序列化、无环且成本有界；每个工具有严格输入输出和失败契约；执行结果在领取下一步骤前持久化；过期 lease、预算不足或未确认实体必须 fail closed；全部组件可使用 fake handler 做离线测试。

## Architecture

```text
Route ledger
  -> Research Intake
  -> Entity Resolver
  -> [confirmation when ambiguous]
  -> Durable Run + versioned Plan
  -> Policy/Budget Gate
  -> Tool Registry
  -> Executor
  -> atomic step/checkpoint commit
  -> observation / one replan
```

LangGraph 负责编排上述节点和条件边。Repository 是 intake、确认、计划、授权和执行事实源。Planner 只输出 `ResearchPlan`；Policy Gate 只输出结构化授权决定；Executor 只接收已授权 `ToolSpec`，工具看不到 Repository、lease 或状态迁移接口。

## Components

- `EntityResolver`：本地证券目录、别名、市场和代码匹配；返回 resolved/ambiguous/unresolved。
- `ResearchIntakeRepository`：持久化请求身份、路由事实、候选实体、确认状态和最终 run。
- `Planner`：根据问题和研究深度生成 DAG；统一验证唯一 ID、依赖存在、无环、预算和最大重试。
- `PolicyGate`：要求研究路由、实体已确认、工具已注册、风险允许、预算充足。
- `ToolRegistry`：保存版本化 ToolSpec 与 handler；拒绝未注册工具和 Schema 异常。`retrieve_documents` 的输出必须标记 sparse/dense/fused 分数、融合策略、索引与 embedding 版本；Phase 3 使用 fake adapter，Phase 4 接入 Milvus BM25+dense+RRF。
- `ResearchExecutor`：计算 frontier，运行无依赖步骤，逐个原子提交；观察不足时最多重规划一次。
- `ResearchOrchestrationGraph`：把 intake、resolve、confirm、plan、authorize 和 execute 串成显式节点。

## Failure and Security

实体歧义返回候选而不创建 run。Planner 非法时修复一次并回落标准计划。工具超时、429、500 和格式错误遵循 ToolSpec fallback；无安全 fallback 时任务失败。所有网络输入继续按不可信数据处理，URL 检查、秘密脱敏和输出上限在工具边界执行。预算在调用前预留、提交时记账；恢复时以已提交 tool ledger 和 checkpoint 为准。

## Verification

评测覆盖同名公司、多市场代码、别名和纠正；DAG 循环/缺失依赖/重复 ID/预算超限；未确认实体、Router 权限伪造、未知工具和高风险工具；并行 frontier、幂等提交、暂停、重启和一次重规划；工具正常、空结果、超时、429、500、畸形输出及 fallback。
