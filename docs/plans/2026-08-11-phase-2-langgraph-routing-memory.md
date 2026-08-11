# Phase 2 LangGraph Routing and Short-Term Memory Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 使用 LangGraph 实现确定性优先的多轮意图路由，并以 Repository 持久化 case turn、summary 和待确认状态，保证社交消息与报告问答不会误触发研究工具。

**Architecture:** FastAPI 接收消息，Context Builder 从 Repository 构建最小上下文，LangGraph `StateGraph` 执行规则层和上下文层并产生严格 `RouteDecision`。LangChain 提供标准消息边界；Durable Runner 仍是六态与执行 checkpoint 的唯一事实源，Phase 2 不启用独立 LangGraph checkpointer。

**Tech Stack:** Python 3.10+、FastAPI、Pydantic、LangGraph 1.2.10、LangChain 1.3.14、SQLite、pytest。

---

### Task 1: Pin framework dependencies and freeze the integration contract

**Files:**
- Modify: `requirements.txt`
- Create: `tests/test_framework_contract.py`

1. 先写失败测试：导入 `langgraph.graph.StateGraph`、`langchain_core.messages.HumanMessage`，并断言 ADR 规定 Durable Runner 为生命周期事实源。
2. 运行 `python -m pytest tests/test_framework_contract.py -q`，确认因依赖缺失失败。
3. 在 `requirements.txt` 固定 `langgraph==1.2.10`、`langchain==1.3.14`；暂不安装 `langgraph-checkpoint-sqlite`。
4. 安装依赖并重新运行测试，预期通过；再运行现有全量测试，确认框架依赖没有改变 Phase 1 行为。

### Task 2: Add versioned short-term-memory storage

**Files:**
- Modify: `backend/migrations.py`
- Modify: `backend/database.py`
- Modify: `tests/test_database_migrations.py`
- Create: `tests/test_conversation_repository.py`

1. 先写 migration v5 失败测试，要求 fresh、v4 和 legacy 数据库都生成 `conversation_turns`、`case_summaries`、`pending_confirmations`。
2. 表中保存 `case_id`、递增 turn sequence、role、原始 content、route intent/reason codes、created_at；summary 使用版本号，确认项包含状态和失效时间。
3. 实现 Repository 的 append/list turn、get/replace versioned summary、put/resolve confirmation；所有写入使用事务，重复 turn id 幂等但不同内容冲突。
4. 增加 FK、唯一索引、case 隔离和迁移回滚测试。

### Task 3: Define strict routing schemas and deterministic rule layer

**Files:**
- Modify: `backend/schemas.py`
- Create: `backend/intent_router.py`
- Create: `tests/test_intent_router.py`

1. 定义九类 intent：`SOCIAL_ACK`、`CONTROL`、`CONFIRMATION`、`REPORT_QA`、`RESEARCH_FOLLOWUP`、`RESEARCH_NEW`、`CLARIFICATION`、`OUT_OF_SCOPE`、`AMBIGUOUS`。
2. `RouteDecision` 必须包含 confidence、case_id、requires_planner、external_research_allowed、response_policy 和稳定 reason codes。
3. 先写表驱动测试覆盖“好的谢谢”“暂停”“继续”“研究腾讯”“再看看现金流”“那现金流呢”以及空白/注入式输入。
4. 实现优先级：待确认回答 > 控制 > 显式研究 > 报告追问 > 社交 > 模糊；只有两类研究 intent 可以允许进入 Planner。

### Task 4: Build the minimal Context Builder

**Files:**
- Create: `backend/context_builder.py`
- Create: `tests/test_context_builder.py`

1. 先写测试验证 Context Builder 只读取当前 case 的最新摘要、有限最近 turns、pending confirmation、active run 和现有报告/证据可用性。
2. 实现字符/turn 上限和确定性裁剪，不拼接完整历史，不读取长期记忆，不包含 lease token、密钥或原始敏感工具输出。
3. 测试跨 case 隔离、超长消息裁剪、无 case、新旧 summary 版本和已过期 confirmation。

### Task 5: Implement the LangGraph routing graph

**Files:**
- Create: `backend/agent_graph.py`
- Create: `tests/test_agent_graph.py`

1. 使用 `TypedDict` 定义 `RoutingState`：message、case context、rule result、context result、final decision 和 response。
2. 创建节点 `load_context -> rule_route -> context_route -> finalize`，以条件边跳过不必要节点；节点保持纯函数或通过显式依赖访问 Repository。
3. 用 LangChain `HumanMessage` 作为输入适配边界，但不把整个历史直接发送给模型。
4. graph 不直接调用研究工具、不更新六态、不持有 lease；`thread_id` 使用 case/run 稳定标识但本阶段不配置第二套 checkpointer。
5. 测试每条条件边、状态可序列化、同输入确定性和 graph 失败时安全回落 `CLARIFICATION`。

### Task 6: Expose a non-destructive conversation routing API

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/schemas.py`
- Modify: `tests/test_api.py`

1. 新增 `POST /api/conversations/route`，输入 message 与可选 case_id，输出 RouteDecision、模板响应和关联 case。
2. 请求先持久化 user turn，graph 完成后持久化 route decision 和 assistant turn；使用 request id 保证重试幂等。
3. Phase 2 端点只分类和响应，不自动创建研究 run。研究 intent 返回 `requires_planner=true`，由 Phase 3 接入 Planner；保留现有 `/api/research` 兼容演示。
4. 测试未知 case、重复请求、并发 turn sequence、SOCIAL_ACK 零模型/零工具、CONTROL 不绕过 Durable Runner、路由异常安全澄清。

### Task 7: Add routing safety and quality evaluations

**Files:**
- Create: `evals/intent-routing-cases.json`
- Create: `evals/run_intent_routing.py`
- Create: `tests/test_intent_routing_evals.py`

1. 建立至少 40 条中文多轮样例，包含社交、控制、确认、研究新建、研究追问、报告问答、模糊表达和 prompt injection。
2. 输出 accuracy、research recall、错误工具触发率、澄清率、p95 latency；核心门禁是非研究请求的 `external_research_allowed` 错误开启率为 0。
3. 受限 LLM 分类器保持关闭；只记录规则/上下文层无法覆盖的样例，不能为了提高召回而默认研究。

### Task 8: Verify, document, and independently review Phase 2

**Files:**
- Modify: `docs/architecture/durable-research-agent.md`
- Create: `docs/reviews/phase-2-verification.md`

1. 运行新增聚焦测试、全量 `pytest -q`、`compileall` 和 `git diff --check`。
2. 记录 graph 节点/边、数据库迁移、API 契约、评测指标、已知限制和框架版本。
3. 让独立 subagent 只读检查路由误触发、case 隔离、幂等、迁移、框架边界和测试真实性。
4. 修复所有阻塞项、重新测试并请求 subagent 复核。
5. 只有 subagent 建议通过且用户明确验收后，才进入 Phase 3。
