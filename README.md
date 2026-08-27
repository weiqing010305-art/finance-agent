# FinScope — 持久化金融研究 Agent

FinScope 是一个面向个人研究者的深度研究（Deep Research）Agent。用户只需输入一句自然语言问题（例如「腾讯近三年利润增长由哪些业务驱动，这种增长是否可持续？」），系统就会自动识别公司、证券代码和市场，拆解研究问题，检索公开资料，交叉验证证据，并在目标 90 秒内输出一份**每个结论都带句子级来源引用**的可核查研究报告。

核心原则：**结论必须能回到原始证据**。证据冲突和数据缺口不会被模型隐藏，而是作为研究结果的一部分显式披露。

> 产品名称尚未最终确定，「FinScope」目前是原型工作名。

---

## 项目定位

| 维度 | 说明 |
|------|------|
| 用户 | 需要快速调查沪深 A 股 / 港股上市公司的个人研究者 |
| 输入 | 一句自然语言研究问题（无需填写公司/代码/市场表单） |
| 输出 | 带数字引用的研究报告，悬停可查看来源和摘录，点击可访问原文 |
| 能力 | 财报分析、市场分析、公司研究三个研究角色（首版为模块化单体实现） |
| 硬指标 | 首次可见结果 10–20 秒，完整报告 90 秒内、硬上限 2 分钟 |
| 边界 | 不输出买卖建议；证据不足时显式披露未知，绝不补猜 |

## 核心特性

- **分层意图路由**：先判定消息是「新研究」「追问」「澄清」还是「无关」，再决定是否启动研究链路；路由层不能授予外部工具权限。
- **确定性规划**：把研究问题拆成带依赖关系的步骤 DAG（理解问题 → 拆解子问题 → 搜索 → 筛选 → 阅读 → 提取 → 补查 → 交叉验证 → 生成报告）。
- **六状态持久化 Runner**：`running → pause_requested → paused → resuming → completed / failed`，支持暂停、恢复、崩溃后重建租约、幂等重放，研究过程随时可中断可继续。
- **证据核验**：每个「支持」的结论都必须能回到已持久化的证据原文；引用链、内容哈希、来源权威等级（authority tier 0–5）由产品代码控制。
- **混合 RAG 检索**：Milvus 2.6.2 的 BM25 + 稠密向量（BGE-large-zh-v1.5）原生混合检索 + RRF 融合，支持中文语料。
- **受控记忆**：长期记忆（用户偏好 / 公司事实 / 实体身份）有明确的生命周期、保留策略和过期清理。
- **生产级隔离**：PostgreSQL 行级安全（RLS）+ 邀请制认证，租户之间的研究记录与证据严格隔离。
- **端到端可观测**：OpenTelemetry + Prometheus + Loki + Grafana，研究轨迹可追踪但不暴露模型内部思维。

## 更新记录（2026-08 批次）

本轮（相对上一版）的主要更新，按主题分组：

### 受控工具真实化
- 5 个占位工具全部实现为真实能力：`calculate_financial_metrics`（10 个确定性指标，公式与输入追溯）、`search_web` / `search_filings`（DeepSeek 受控联网搜索 + 官方披露域名白名单）、`read_document`（文档库分节读取）、`extract_financial_facts`（确定性规则抽取，带期间/单位/币种/来源绑定）。
- 所有工具缺输入/缺配置时返回显式 `degraded`，不静默伪造结果。

### 研究链路端到端串联
- 执行器自动把已成功步骤的输出注入下游工具输入：搜索/检索文本 → `extract_financial_facts.texts`，抽取事实 → `calculate_financial_metrics.facts`；显式输入优先，账本仍记录计划原始输入（防篡改校验不变）。
- Planner 新增 `get_quote` 步骤：每次研究并行获取真实行情。

### 真实数据源
- **巨潮资讯公告 API**（A 股官方披露，无需 key）：`search_filings` 主路径；港股或巨潮失败时降级网页搜索（`degraded` + `fallback_used` 显式标记）。
- **腾讯免费行情**（A 股/港股/美股）：新增 `get_quote` 工具，GBK 字段解析为确定性数值（现价/涨跌/PE/PB/市值等）。
- 真实调用冒烟脚本：`scripts/verify_filings_source.py`、`scripts/verify_quote.py`。

### 工程与正确性修复
- **状态机规则单一事实源**：新增 `backend/run_states.py`，SQLite / PostgreSQL 的 CAS 守卫与迁移触发器全部由它生成（修复了 `database.py` 缺失 `running→completed` 的漂移）。
- **租约抢占宽限期**：`reconcile_expired_runs` 使用 lease TTL 1/3 宽限，心跳延迟的 worker 不会被立刻抢走；恢复失败不再拖垮启动。
- **迁移 SQL 切分修复**：按字符串/注释感知切分，不再被字面量里的分号破坏。

### 测试与评测可信度
- **真实 PostgreSQL 集成门**：`tests/test_postgres_real_integration.py` 在真实 PG 上执行 Alembic 迁移并验证 RLS 租户隔离（默认 skip，需 `FINSCOPE_TEST_PG_URL`；本机已用一次性容器实测通过）。
- **测试全离线化**：conftest 桩掉腾讯行情与巨潮 API，测试套件完全离线确定性。
- **评测去自证化**：Phase 4 证据核验评测改为混合质量输入（支持/伪造/无证据/冲突并存），质量属性恒真断言 + 诚实指标允许小于 1.0。

### 架构演进与安全
- **增量式巨类拆分**：新增 `DocumentRepository`（文档领域数据访问），老 `Repository` 冻结并开始瘦身。
- **意图路由注入防线补强**：覆盖中英文常见注入家族（忽略指令/泄露提示词/角色冒充/越狱），全部 fail-closed。
- **Docker 镜像卫生**：新增 `.dockerignore`（secrets/backups/.venv 不再进构建上下文），运行时非 root `appuser` 运行（实测镜像 484MB、import 正常）。

### A 股财报数据接入 + controlled-tools 计划升级
- **新增受控工具 `fetch_financial_statements`**：直连东方财富公开 JSON 接口 `RPT_F10_FINANCE_MAINFINADATA`（无需 key），返回 36 个 canonical 中文会计字段：营业总收入 / 营业收入 / 营业成本 / 毛利 / 归母净利 / 扣非净利 / 营业利润 / 总资产 / 总负债 / 股东权益 / 经营/投资/筹资现金流 / 总股本 / EPS / BPS / 每股经营现金流 / 销售毛利率 / 加权 ROE / ROIC / 销售净利率 / ROA / 资产负债率 / 流动比率 / 速动比率 / 现金比率 / 权益乘数 / 各项同比%。
- **可控覆盖**：A 股 6 家代表性标的实测稳定（茅台 / 五粮液 / 宁德 / 中国平安 / 平安银行）；港股 / 美股公开免费 API 全部不可用（腾讯 `web.ifzq` 已下线、雪球需登录、新浪无 endpoint），**显式降级**为 `coverage="unsupported" + fallback_used="filings_search"`，绝不伪造数字。
- **controlled_tools 计划升级**：在 `search_filings`/`get_quote` 之后插入 `fetch_statements` 步骤，与行情并行执行；预算下限从 10 提升至 12 容纳新增 cost。`extract_financial_facts` 现在接收结构化财报结果作为上游之一。
- **实测指标**：跑通 `fetch_financial_statements → calculate_financial_metrics` 链路后，茅台算出 `营收增速 +68.25%` / `ROE 16.96%`（基于净利润 / 股东权益）；五粮液、宁德、中国平安、平安银行同样稳定出数。
- **新增脚本**：`scripts/verify_financial_statements.py`（冒烟验证 6 个标的）、`scripts/demo_controlled_tools_flow.py`（无 PG 全栈的端到端驱动，演示真实数据 + 工具链路 + 引用溯源）。

### 前端案卷控制台完善
- **`/api/securities` 别名词典**：12 家公司（腾讯/阿里/比亚迪/小米/茅台/宁德/美团/苹果/英伟达/特斯拉等）的 `{alias, company, symbol, market}` 映射，页面加载时一次性拉取。
- **左侧公司名实时匹配**：用户输入"分析腾讯"时，`input` 事件立即从别名表匹配最长前缀，左侧公司 / ticker 即时更新（不等待后端响应）。`submitFeedback` 直接用匹配出的公司 / symbol / market 提交，避免后续 `applyTask` 被"自动识别中"占位。
- **SSE 流式输出**：用 `EventSource` 真接 `GET /api/research/{id}/events`，把每个事件按用途路由：
  - `appendLog` → 思维链时间线（搜索 / 阅读 / 分析 / 写作）
  - `appendReportDelta` → 报告草稿逐字符流出（DeepSeek `output_text.delta`）
  - `applyTask` → 状态 / 进度 / 按钮可用性
  - 终态后关闭流、调 `GET /api/research/{id}` + `/evidence` 拉最终报告
- **Trace 列表去重**：600+ 个 `report.delta` 事件原本会让"正在生成研究报告"思维链里出现 600+ 行重复行；`appendLog` 现在与最后一条同消息时跳过，让状态切换清晰可见（搜索 → 阅读 → 分析 → 写作 → 完成）。
- **旧浏览器兜底**：`EventSource` 不可用或服务端关闭（readyState === CLOSED）时回落到 1.5s 轮询。

### 契约对齐
- **`run_id` 字段别名**：后端 `get_research` / `pause_research` 在数据库主键 `id` 之外暴露 `run_id` 别名，前端契约统一。
- **`controlled_tools` profile 接线**：`SUPPORTED_EXECUTION_PROFILES` 加入该 profile；worker profile 白名单同步；handler 注册 `controlled_tools_research`。从此 controlled_tools 真正可启用（之前只有半成品代码）。

### controlled_tools 真全栈跑通（PostgreSQL/RLS + Dramatiq）
- **首次在 Docker Compose 全栈上端到端跑通 controlled_tools**：`fetch_financial_statements` → `get_quote` → `retrieve_documents` → `extract_financial_facts` → `calculate_financial_metrics` → 报告。
- 实测贵州茅台研究：`status=completed / progress=100`，报告含 **138 条 financial_metrics**（36 个东方财富 F10 指标 × 4 个报告期）。
- 新增 **Alembic migration 0014** `execution_authorizations_pg`（受控工具策略审计表），并授予 `finscope_app` / `finscope_worker` 权限。
- PG durable 补齐 `get_runtime_snapshot` / `record_execution_authorization` / `commit_step(kind=)`，policy 透传 principal 以兼容 RLS。
- deterministic 报告改为每个已验证结论一个 section，避免多数字结论在单一 section 中触发引用校验失败。
- 修复 Docker 初始化脚本 CRLF 行尾（`init-roles.sh` 等），容器可直接执行。

### LLM 综合报告（citation-constrained，多 Provider）
- 新增 `backend/synthesizer.py`：`DeepSeekReportSynthesizer` 通过 OpenAI-compatible `/chat/completions` 把已验证证据合成为结构化报告，支持 DeepSeek、OpenAI、Qwen、ModelScope、Kimi、智谱、Ollama、vLLM、自定义兼容端点。
- **模型只能引用已持久化的证据 URL**：prompt 约束 + `_sanitize` 强制剔除非法 URL；未配置 key 或调用失败时自动降级到确定性 `CitationConstrainedReporter`。
- worker 接线：`ControlledToolsResearchProcessor(synthesizer=tenant_synthesizer)`，每个租户可在前端左下角「设置」里保存自己的 provider / model / base_url / api_key，未保存时回落到服务端 `.env` 的 `DEEPSEEK_API_KEY`（或 `LLM_API_KEY`）。
- 单测覆盖证据净化 / 缺 key / 非法 JSON / from_env / 多 provider 解析。

### 前端财务指标表格
- `renderReport` 检测报告的 `financial_metrics` 字段后，在报告正文顶部渲染「财务指标摘要」表格（指标 / 数值 / 报告期）。
- 受控工具报告的真实 A 股数据直接在案卷控制台可见，而不只藏在证据抽屉里。

### 股价 K 线图（ECharts）
- 引入 ECharts 5（本地 `prototype-research-ui/echarts.min.js`，离线可用）。
- 报告 `price_bars` 字段携带完整 OHLCV（AkShare 日线来源）；前端用 `candlestick` + `bar` 双 grid 渲染蜡烛图与成交量副图。
- 设计参考了 k-line-replay 项目的实现（蜡烛图样式、grid 布局、涨跌色），但**不包含**回放、模拟交易、指标计算——FinScope 只做展示，不做训练/回测。

### 港股 / 美股财报（AkShare 优先）
- 引入 `backend/akshare_source.py`，把 A/H/US 日线与财报统一接到一个免费、无需 token 的数据源（替代了 Tushare Pro 这一被 token 权限卡住的方案）。
- A 股日线：`ak.stock_zh_a_daily`（新浪源）；港股日线：`ak.stock_hk_daily`（腾讯源）；美股日线：`ak.stock_us_daily`。
- 港股 / 美股财报：`ak.stock_financial_hk_analysis_indicator_em` / `us_analysis_indicator_em`（东方财富）。
- 实测：腾讯（00700.HK）最新报告期 9 期财务（营收 7517 亿、ROE 21.13%）；A 股茅台（600519）报告含 138 条财务指标 + 30 根 K 线。

### 前端统一 + 后端切到 PostgreSQL
- 唯一前端：`unified-agents.html`（案卷控制台）。`formal-console.html` / `invitation.html` 等杂志风原型页删除。
- 后端：`compose.yaml` 与 `Dockerfile` 切换 `api` 服务到 `backend.formal_app:create_formal_app`（PostgreSQL 版），研究数据进入 RLS 多租户的持久化存储。
- 前端补全鉴权（登录页 + api() 自动带 Bearer + SSE fetch 流式支持 Authorization header）+ 字段归一（`run_id` → `id`、`task.report.report` 内层读取、case 字段兼容 `latest_task_id/latest_status/title`）+ 缺失路由补齐（`/api/securities`、`/api/cases`、SSE `/events`、`/evidence/enrich`、`/feedback`、`DELETE`、`PATCH`）。
- 测试：558 通过 / 5 跳过 / 0 失败。

## 架构总览

FinScope 采用**模块化单体**（modular monolith）架构：一份代码、一组模块，通过 PostgreSQL 事务边界和 RLS 实现多租户隔离。模型服务只承担推理，联网搜索、来源筛选、证据关系都由产品代码控制。

```
用户输入
   │
   ▼
意图路由（Intent Router）── 非研究意图（社交/控制/澄清）
   │
   ▼
研究入口（Intake + 实体解析）── 唯一识别公司/代码/市场
   │
   ▼
确定性规划（Planner）── 步骤 DAG + 预算
   │
   ▼
六状态持久化 Runner ──┬── 检索工具（受控 Tool Registry）
                       ├── 证据核验（Verifier）
                       ├── 报告生成（Reporter）
                       └── 记忆读写（Memory）
   │
   ▼
带句级引用的研究报告 + 证据台账
```

### 关键技术栈

- **Web/编排**：FastAPI、LangGraph、LangChain、Pydantic
- **持久化**：PostgreSQL（RLS）、SQLAlchemy、Alembic、Redis、Dramatiq
- **检索**：Milvus 2.6.2、BAAI/bge-large-zh-v1.5（固定 revision）
- **对象存储/网关**：MinIO、Caddy
- **可观测**：OpenTelemetry Collector、Prometheus、Loki、Grafana
- **LLM**：OpenAI-compatible Chat Completions（DeepSeek `deepseek-v4-flash` 默认；也支持 OpenAI / Qwen / Kimi / 智谱 / Ollama / vLLM / 自定义端点，前端左下角设置）

## 阶段演进

项目按 8 个阶段迭代推进，每个阶段都有独立的设计文档和验证记录：

| 阶段 | 主题 | 关键交付 | 验证 |
|------|------|----------|------|
| P1 | 持久化 Runner | 六状态状态机、租约、崩溃恢复、幂等重放 | ✅ |
| P2 | 意图路由 + 记忆 | LangGraph 分层路由、受控记忆 | ✅ |
| P3 | 研究链路 | intake、实体解析、确定性规划、研究执行器 | ✅ |
| P4 | RAG 证据报告 | 证据抽取、引用链、报告生成 | ✅ |
| P5 | 记忆硬化 | 长期记忆生命周期、保留、冲突处理 | ✅ |
| P6 | 本地生产化 | PostgreSQL RLS、邀请认证、Redis/Dramatiq、MinIO、Caddy、可观测 | ✅ |
| P7 | 真实 Milvus/BGE | 真实混合检索 + 中文语料冒烟验证 | ✅ |
| P8 | formal 真实 RAG | 真实 RAG 接入持久化 worker + PostgreSQL 授权边界 | ✅ |

完整设计文档见 [`docs/plans/`](docs/plans/)，验证记录见 [`docs/reviews/`](docs/reviews/)，架构决策见 [`docs/adr/`](docs/adr/)。

## 快速开始

**单一前端 + PostgreSQL 后端**：项目当前只有一种运行形态——Docker Compose 全栈（Postgres + Redis + MinIO + Dramatiq + Caddy + Milvus），前端入口是 `prototype-research-ui/unified-agents.html`（案卷控制台）。`backend/app.py` 的 SQLite 旧原型仍保留在仓库，但**已不在产品路径**。

### Docker Compose 全栈启动

前置：Docker Desktop（Linux 引擎）、仓库的 Python 虚拟环境、`secrets/` 目录里已有本地开发凭据（或运行 `scripts/local.ps1 init` 生成）。

```powershell
# 启动全栈（默认 executor = synthetic_smoke）
.\scripts\local.ps1 up

# 启动 controlled_tools executor（A 股真实财报 + AkShare 股价）
$env:FINSCOPE_FORMAL_EXECUTOR = "controlled_tools"
.\scripts\local.ps1 up

# 初始化 owner / tenant（首次，或 wipe 数据卷后）
.\scripts\local.ps1 bootstrap -Email owner@example.com -TenantName "FinScope Local"
```

打开浏览器访问 `https://localhost:8443/`（接受 Caddy 的本地证书告警）。首页会自动跳转到统一前端 `unified-agents.html`。

### 登录

`unified-agents.html` 是受控研究台，需要登录后使用。登录凭据：

- **Tenant ID**：从 `bootstrap` 命令的输出或 `scripts/local.ps1 bootstrap` 的结果中获取。
- **Email**：创建时指定的邮箱（如 `owner@example.com`）。
- **Password**：创建时输入的密码。若忘记，可以在 Postgres 容器里重置密码哈希：

```powershell
# 在 api 容器内用 argon2 生成新哈希并写入 DB
docker exec -it finscope-local-api-1 python3 -c "
from backend.auth.passwords import hash_password
print(hash_password('YourNewPassword'))"
# 然后
docker exec -it finscope-local-postgres-1 psql -U finscope_admin -d finscope   -c "UPDATE users SET password_hash = '<hash>' WHERE email = 'owner@example.com';"
```

常用命令：

```powershell
.\scripts\local.ps1 status   # 查看服务状态
.\scripts\local.ps1 logs     # 查看日志
.\scripts\local.ps1 test     # 运行测试
.\scripts\local.ps1 down     # 停止（保留数据卷）
```

### 启用真实 LLM 综合报告（可选）

综合报告（`backend/synthesizer.py`）未配置租户 LLM 设置时默认走 deterministic 路径。两种启用方式：

1. **服务端全局**：在项目根目录 `.env` 写入（compose.yaml 已把该变量注入 worker）：

```env
# .env
DEEPSEEK_API_KEY=sk-your-key
```

2. **按租户设置**：打开前端（`https://localhost:8443`），点击左下角「设置」，选择 Provider、填写 Model / Base URL / API Key，点「保存」；下次研究自动使用该配置，无需改容器环境变量。

保存后重启 worker 或直接新建研究任务，新任务的报告就会通过已配置的 LLM 综合生成（更自然、可读）；未配置或调用失败时仍会安全降级到 deterministic reporter。

### 已废弃的形态

- **SQLite 旧原型（`backend/app.py`）**：当前未在任何路径使用，已不推荐启动。若需调试，单独跑 `uvicorn backend.app:app --port 8780`。
- **`formal-console.html` / `invitation.html` 等杂志风前端**：已删除，不再是产品前端。

## 运行时 Profile（Docker Compose）

| Profile | 内容 |
|---------|------|
| `core` | PostgreSQL、Redis、MinIO、Mailpit、API、worker、dispatcher、Caddy |
| `rag` | Milvus 2.6.2 + 独立 etcd / 对象存储 |
| `rag-admin` | 一次性 fixture 索引器（专用 CPU-only 镜像 + 共享 Hugging Face 缓存） |
| `observability` | OpenTelemetry Collector、Prometheus、Loki、Grafana |

```powershell
docker compose --profile core --profile rag --profile observability up -d --build
```

加密备份与隔离恢复演练：

```powershell
.\scripts\backup_formal.ps1 create -BundlePath backups\manual.fsbk
.\scripts\backup_formal.ps1 drill -BundlePath backups\manual.fsbk
.\scripts\install_backup_schedule.ps1
```

## 目录结构

```
finance agent/
├── backend/                 # 模块化单体后端（52 个模块）
│   ├── app.py               # 旧原型 FastAPI 入口（DeepSeek / Mock 研究）
│   ├── formal_app.py        # 生产形态 formal API 入口（PostgreSQL + RLS）
│   ├── auth/                # 认证（邀请制、JWT、密码、邮箱）
│   ├── db/                  # 数据库层（durable、artifacts、metadata、rag_catalog）
│   ├── jobs/                # Dramatiq 作业（ledger、worker、executor、dispatch）
│   ├── agent_graph.py       # 路由图 / 研究图编排
│   ├── deepseek_research.py # 旧原型 DeepSeek Responses API 客户端（联网搜索）
│   ├── durable_runner.py    # 六状态持久化 Runner
│   ├── milvus_retrieval.py  # Milvus 混合检索
│   ├── evidence.py          # 证据构建
│   ├── verifier.py          # 结论核验
│   └── ...                  # planner、retrieval、memory、redaction 等
├── alembic/                 # 数据库迁移（15 个版本，0001–0015）
├── docs/                    # 设计 / 计划 / 验证 / ADR 决策文档
├── evals/                   # 评测用例与打分器
├── infra/                   # Caddy / Grafana / Loki / OTel / Prometheus / PG 配置
├── prototype-research-ui/   # 前端原型（案卷控制台）
├── scripts/                 # 运维与验证脚本（local.ps1、backup、verify_*）
└── tests/                   # 79 个测试文件（含 integration）
```

## 测试与验证

- 全量 pytest：**558 通过、5 跳过**（跳过项为需要真实 Milvus/BGE 环境的外部集成门；无失败）。
- 测试覆盖：持久化 Runner 契约、RLS 租户隔离、证据/引用链完整性、迁移契约、幂等重放、跨租户隔离、备份恢复等。
- 离线评测：Phase 3/4/5 的意图路由、RAG、记忆评测用例均可离线运行。Phase 4 的证据核验评测已去自证化：在混合质量输入（支持/伪造数字/无证据/冲突并存）上断言「质量属性恒真」（伪造全捕获、无误报、冲突必披露），同时 `supported_rate` 等诚实指标反映真实输入分布、允许小于 1.0——任何把「恒等于 1.0」当成绩的回归都会被抓住。

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 当前状态与边界

**已经做到（单一产品形态）**：

- **统一前端 `prototype-research-ui/unified-agents.html`**：案卷控制台（左侧案卷导航 + 中间任务报告 + 搜索抽屉 + 底部输入框），是项目当前唯一的前端入口；登录鉴权（tenant + email + password → JWT）、SSE 流式输出（fetch + ReadableStream，支持 Bearer）、ECharts K 线 + 成交量图表（本地 `echarts.min.js`）、结构化报告（公司概览 / 股价表现 / 财务摘要 / 财务分析 / 证据来源）。
- **PostgreSQL 后端（formal_app）**：行级安全（RLS）、邀请制认证、Alembic 迁移（15 个版本）、Dramatiq 持久化 worker、Caddy 反代、Milvus 混合检索；研究链路（fetch_financial_statements + fetch_stock_prices + extract_financial_facts + calculate_financial_metrics）已在 Docker 全栈端到端跑通（A 股实测 138 条 financial_metrics + 30 根 K 线，**贵州茅台报告：最新收盘价 ¥1292.3、ROE 16.75%、毛利率 89.6%、资产负债率 15.2%**，全部数据为真）。
- **单一「公司分析 Agent」**：原 PRODUCT.md 里规划的三个 Agent（财报 / 市场 / 公司研究）已合并为一条 controlled_tools DAG；单次运行同时产出**股价 + 财报 + 公告 + 分析**。多 Agent 并行已确认**不做**（PRODUCT.md 要求"必须由固定评测证明收益后启用"，当前单 Agent 多工具路径已能满足产品需求且更快、更稳）。
- **数据源按需降级**：
  - A 股：东方财富 F10（`RPT_F10_FINANCE_MAINFINADATA`，36 字段，无需 key）+ 巨潮公告（无需 key）+ 腾讯行情（无需 key）。
  - 港股 / 美股：**AkShare** 优先（`stock_zh_a_daily` / `stock_hk_daily` / `stock_us_daily` 日线 + `stock_financial_hk/us_analysis_indicator_em` 财报，全部无需 key）。Tushare Pro（需 token）作为降级兜底，但实际从未启用（token 无权限）。
  - 全部失败时显式 `degraded=True, fallback_used="filings_search"`，**不伪造数字**。

**仍未做到（诚实声明）**：

- **LLM 综合报告依赖外部网络**：代码和 key 注入已就位（worker 容器现在能解析到 `DEEPSEEK_API_KEY`），但真实生成需要 worker 能访问对应模型端点；未配置 key、网络不可达或调用失败时自动降级到 deterministic reporter（绝不伪造）。
- **LLM 工具 `search_web` 未启用**：DeepSeek Responses `web_search` 集成已存在（`backend/web_search.py`），需要 `DEEPSEEK_API_KEY` + 配置；未启用时降级到 `search_filings`。
- **真实 PostgreSQL 集成门默认跳过**：`tests	est_postgres_real_integration.py` 在真实 PG 上执行 Alembic 迁移并验证 RLS 租户隔离（SQLite 契约测试无法覆盖），需设置 `FINSCOPE_TEST_PG_URL`；本机已用 Docker 一次性容器实测通过。
- **CSP 含 `'unsafe-inline'`（已知安全债）**：unified-agents.html 是内联脚本原型，Caddyfile 因此放行了 `'unsafe-inline'`；未来若把 CSS/JS 拆成外部文件，应收紧回 `'self'`。

## 架构文档索引

- [持久化 Agent 架构](docs/architecture/durable-research-agent.md)
- [架构决策记录 ADR](docs/adr/README.md)
- [Phase 6 本地生产化设计](docs/plans/2026-08-13-phase-6-local-production-design.md)
- [Phase 7 真实 Milvus/BGE 验证](docs/reviews/phase-7-verification.md)
- [Phase 8 formal 真实 RAG 验证](docs/reviews/phase-8-verification.md)

---

## 安全说明

- 所有密钥（`secrets/`、`backend/.env`、`backups/`、`*.db`）均已被 `.gitignore` 排除，**不要提交任何真实密钥**。
- 配置文件只提供 `.env.example` 模板，真实密钥永远只存在于本地的 `.env` 文件中。
