# Phase 2 Verification

> 日期：2026-08-11
> 状态：用户验收通过（2026-08-11）

## 已实现

- 固定 `langgraph==1.2.10` 与 `langchain==1.3.14`；
- SQLite schema v5 新增 `conversation_turns`、`case_summaries`、`pending_confirmations`；
- SQLite schema v6 新增 `route_requests`，持久化 request identity、decision、response 与 trace；
- turn ID 幂等、case 内递增 sequence、并发写入、跨 case 隔离和 migration 回滚；
- Context Builder 只投影当前 case 的最新 summary、有限 turns、待确认项、active run 与报告/证据可用性；
- 上下文裁剪并脱敏 token、API key 和 `sk-*`，不输出 lease token 或确认 payload；
- 九类严格意图 Schema，以及“待确认 > 控制 > 显式研究 > 报告追问 > 社交 > 模糊”的确定性规则；
- LangGraph 节点 `load_context -> rule_route -> context_route -> finalize`，高置信规则跳过上下文节点；
- LangChain `HumanMessage` 作为输入边界；受限 LLM 分类器保持关闭；
- Graph 不直接改六态、不持有 lease、不调用研究工具，也未配置第二套 checkpointer；
- `POST /api/conversations/route` 支持 case 绑定、request ID 幂等、turn 持久化和安全失败回落；
- 现有 `/api/research` 保持兼容，Phase 2 路由端点不会自动创建研究 run。

## 验证结果

```text
python -m pytest -q
180 passed, 1 warning in 7.76s
```

唯一 warning 是 Starlette TestClient 对当前 httpx 兼容层的弃用提醒。

路由评测：

```json
{
  "case_count": 92,
  "accuracy": 1.0,
  "research_recall": 1.0,
  "false_research_permission_rate": 0.0,
  "false_planner_activation_rate": 0.0,
  "clarification_rate": 0.33695652173913043,
  "p95_latency_ms": 1.0653000008460367,
  "failures": []
}
```

评测覆盖社交、控制、待确认、新研究、研究追问、报告问答、空输入、模糊表达、越权工具指令、否定研究、对既有分析的感谢、元能力问题、中文同形词、混合意图、自然改写和带研究动词的非金融问题。核心门禁为：非研究请求的 Planner 误触发率与外部研究权限错误开启率都必须为 0。这里的 p95 只测内存中的路由图，不代表包含 FastAPI、SQLite 或冷启动的端到端延迟。

## 关键测试边界

- fresh、v4 与 legacy 数据库升级到 v5；畸形已有表导致整笔 migration 回滚；
- 同 turn ID 同内容幂等，不同内容冲突；12 个并发 turn 获得唯一连续 sequence；
- summary 版本追加，pending confirmation 解析、过期与替换；
- Context Builder 的 case 隔离、轮次/字符限制、秘密脱敏和终态证据投影；
- LangGraph 的条件边、确定性、JSON 可序列化、无 checkpointer 和异常安全回落；
- API 的未知 case、幂等重试、不同消息冲突、零 case 社交响应和 CONTROL 不旁路 Runner；
- Prompt injection 式“绕过权限调用工具”不会开放 Planner 或外部研究权限。

## 第一轮独立审查与修复

独立 subagent 第一轮结论为“不通过”。已完成：

- 否定研究、感谢既有调研、研究元问题和报告引用优先于显式研究关键词，避免“感谢你的调研”再次开放工具；
- `RESEARCH_NEW` 在实体解析和确认前保持 `external_research_allowed=false`；
- schema v6 保存完整路由结果，同 request ID 在上下文变化后仍原样回放；无 case 请求也具有全局 request identity 冲突保护；
- Context Builder 脱敏覆盖完整 Authorization/Bearer、JSON API key、password/secret/credential、query token 和 `sk-*`；
- confirmation 到期时间解析为带时区 datetime 并统一 UTC，查询/消费会原子标记 `expired`，并发 resolve 只有一个成功；
- confirmation 路由只声明等待处理器，不谎称已经消费；
- API 空白输入统一返回 `422`；summary 游标必须引用已持久化 turn 且单调前进；
- 评测从 47 条扩充到 60 条，并加入第一轮审查的自然反例。

## 第二轮独立审查与修复

第二轮复核继续发现报告问句和 Agent 能力问句的自然改写会被宽泛研究词接管，以及真实 v5 历史 confirmation 未规范化。已完成：

- 将研究识别从“包含研究词”收紧为明确动作表达，并补充报告 subject、Agent 元能力与财经上下文判断；
- “这份分析准确吗”“分析结果可靠吗”走 `REPORT_QA`；“你支持哪些研究类型”不开放外部研究；
- “谢谢，请分析现金流”仍能识别为研究追问；“分析天气对农业公司的影响”不会被简单天气词误杀；
- `RouteDecision` 增加跨字段权限不变量，Schema 直接拒绝 `SOCIAL_ACK + external=true` 和 `RESEARCH_NEW + external=true`；
- v6 migration 对已有 v5 confirmation 的 offset 时间统一 UTC；naive/非法时间采用 fail-closed 策略标记 expired；
- 评测由 60 条扩充到 68 条，覆盖第二轮全部自然改写。

## 第三轮独立审查与修复

第三轮发现“研究生”“分析师”、产品能力问题和更多报告问句仍可能命中研究动作。根因修复为：

- Phase 2 Router 不再拥有外部工具授权能力；所有 RouteDecision 的 `external_research_allowed` 固定为 false；
- `requires_planner` 只表示后续可进入实体、权限和预算链，Phase 3 Policy Gate 才能授权工具；
- Schema 层拒绝任何 Router external=true，即使未来 Graph 或模型返回字段合法但权限组合错误也会安全降级；
- 排除“研究生/分析师”同形词，增加能力、功能、收费和术语区别元问题；
- 补充“分析得靠谱吗/是否有遗漏”等报告追问；
- 评测扩充到 76 条，并新增非研究请求 Planner 误触发率门禁。

## 第四轮独立审查与修复

第四轮确认 Router 的外部权限边界已经可靠，但发现更多角色词、产品操作问句和报告评价问句会误进 Planner，同时 URL userinfo、部分查询密钥和 Bearer 错误文本仍可能泄漏。已完成：

- 研究动作必须带具体研究对象；缺失对象时返回澄清，不进入 Planner；
- 排除“分析员/研究人员”等角色词，并覆盖帮助文档、使用方式、页面入口等产品元问句；
- 有报告时，“分析完整吗/合理吗”等评价问句优先走 `REPORT_QA`；
- 新增统一 `redaction` 模块，移除 URL userinfo，遮盖 password/secret/auth/authorization 等查询参数，并完整消费 Bearer/Basic 凭据；
- Context Builder、provider error、事件 URL 和持久化 evidence 复用统一 URL/文本脱敏逻辑；
- 评测扩充到 85 条，第四轮全部自然反例进入固定回归集。

## 第五轮独立审查与修复

第五轮证明“动词后存在字符”仍不是可靠的 Planner 正向门禁，并发现 OAuth query/fragment 的扩展秘密类型。已完成：

- Planner 正向门禁现在必须命中财经对象、公司/证券实体、当前 case 公司名或明确 case 指代；
- 明显产品 UI 与非财经研究对象不会仅凭“研究/分析/看看”进入 Planner；
- Context Builder 将当前 case 公司名纳入最小上下文，支持确定性的公司延续判断；
- URL query 增加 `client_secret`、`refresh_token`、`id_token`、`session_token`，并统一移除 fragment；
- 事件、报告证据和 provider 错误均增加 OAuth URL 直接回归测试；
- 评测扩充到 92 条，第五轮 7 个 Planner 绕过样例均固化为门禁用例。

## 第六轮独立终验

独立 subagent 最终建议通过 Phase 2，未发现剩余阻塞项。复核确认 7 个 Planner 绕过反例、合法公司研究、当前 case 指代、Router 权限不变量、OAuth URL/Bearer 脱敏、route ledger 幂等、confirmation UTC/并发/审计均符合契约；复跑 92 条评测与文档指标一致，聚焦回归 134 条通过。

## 已知限制

- 92 条数据是工程基线集，不代表真实用户分布；后续需要从匿名化真实失败样例持续扩充；
- 受限 LLM 分类器仍关闭，规则/上下文无法判断时返回澄清；
- `/api/conversations/route` 在没有 `case_id` 时会保存幂等路由账本，但不创建 case/turn；Phase 3 的实体解析入口需要为 `RESEARCH_NEW` 创建 draft case 并关联初始请求；
- CONTROL 当前只分类，不直接执行 pause/resume，避免绕过 Durable Runner；前端仍调用已有控制 API；
- REPORT_QA 只完成路由判定，基于证据的实际回答属于 Phase 4；
- case summary 已版本化，但自动摘要策略尚未启用；
- Phase 2 没有 LangGraph 原生 checkpointer；恢复事实源仍是 Durable Runner/Repository。

## 阶段验收门禁

1. 独立 subagent 检查误触发、case 隔离、幂等、迁移、LangGraph/LangChain 使用边界和评测真实性；
2. 主 Agent 修复全部阻塞项并重跑验证；
3. subagent 复核建议通过；
4. 用户明确确认后才进入 Phase 3。
