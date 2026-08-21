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

### DeepSeek LLM 综合报告（citation-constrained）
- 新增 `backend/synthesizer.py`：`DeepSeekReportSynthesizer` 用 DeepSeek Responses API 把已验证证据合成为结构化报告。
- **模型只能引用已持久化的证据 URL**：prompt 约束 + `_sanitize` 强制剔除非法 URL；无 `DEEPSEEK_API_KEY` 或调用失败时自动降级到确定性 `CitationConstrainedReporter`。
- worker 接线：`ControlledToolsResearchProcessor(synthesizer=DeepSeekReportSynthesizer.from_env())`。
- 6 个单测覆盖证据净化 / 缺 key / 非法 JSON / from_env。

### 前端财务指标表格
- `renderReport` 检测报告的 `financial_metrics` 字段后，在报告正文顶部渲染「财务指标摘要」表格（指标 / 数值 / 报告期）。
- 受控工具报告的真实 A 股数据直接在案卷控制台可见，而不只藏在证据抽屉里。

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
- **LLM**：DeepSeek Responses API（`deepseek-v4-flash`，旧原型联网搜索）

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

项目有两套可运行的形态，用途不同：

### A. 生产形态部署（Docker Compose，推荐用于验证架构）

前置：Docker Desktop（Linux 引擎）、PowerShell、仓库的 Python 虚拟环境。

```powershell
.\scripts\local.ps1 init
.\scripts\local.ps1 up
.\scripts\local.ps1 bootstrap -Email owner@example.com -TenantName "FinScope Local"
```

`bootstrap` 会提示设置密码并打印生成的租户 ID。打开 `https://localhost:8443/`（接受 Caddy 的本地证书告警），用租户 ID + 邮箱 + 密码登录。Mailpit 邮件预览在 `http://127.0.0.1:8025/`。

常用命令：

```powershell
.\scripts\local.ps1 status   # 查看服务状态
.\scripts\local.ps1 logs     # 查看日志
.\scripts\local.ps1 test     # 运行测试
.\scripts\local.ps1 down     # 停止（保留数据卷）
```

运行真实 RAG 演示（首次会下载固定的 BGE 模型到持久化卷）：

```powershell
.\scripts\local.ps1 up-rag
.\scripts\local.ps1 seed-rag -TenantId <tenant-id> -UserId <user-id>
```

### B. 旧原型：DeepSeek 联网搜索（真实 Deep Research）

旧版 FastAPI 原型（`backend/app.py`）支持通过 DeepSeek Responses API 的 `web_search` 工具做**真实联网搜索**，能端到端跑通「搜索 → 阅读 → 分析 → 生成带引用报告」的完整链路。这是迁移期能力（模型直连搜索，尚未走受控 Tool Registry），但已经实测可用。

在 `backend/.env` 中配置（该文件已被 gitignore，不会上传）：

```
FINSCOPE_RESEARCH_MODE=deepseek
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_MODEL=deepseek-v4-flash
```

一条命令验证整条链路：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_deepseek_research
```

或启动原型 API（端口 8780）后，用浏览器直接打开 `prototype-research-ui/unified-agents.html`：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --port 8780
```

> 前端以 file:// 方式打开（origin 为 `null`）才能通过 CORS 连接本地 API。

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
│   ├── deepseek_research.py # DeepSeek Responses API 客户端（联网搜索）
│   ├── durable_runner.py    # 六状态持久化 Runner
│   ├── milvus_retrieval.py  # Milvus 混合检索
│   ├── evidence.py          # 证据构建
│   ├── verifier.py          # 结论核验
│   └── ...                  # planner、retrieval、memory、redaction 等
├── alembic/                 # 数据库迁移（14 个版本，0001–0014）
├── docs/                    # 设计 / 计划 / 验证 / ADR 决策文档
├── evals/                   # 评测用例与打分器
├── infra/                   # Caddy / Grafana / Loki / OTel / Prometheus / PG 配置
├── prototype-research-ui/   # 前端原型（案卷控制台）
├── scripts/                 # 运维与验证脚本（local.ps1、backup、verify_*）
└── tests/                   # 78 个测试文件（含 integration）
```

## 测试与验证

- 全量 pytest：**543 通过、5 跳过**（跳过项为需要真实 Milvus/BGE 环境的外部集成门；无失败）。
- 测试覆盖：持久化 Runner 契约、RLS 租户隔离、证据/引用链完整性、迁移契约、幂等重放、跨租户隔离、备份恢复等。
- 离线评测：Phase 3/4/5 的意图路由、RAG、记忆评测用例均可离线运行。Phase 4 的证据核验评测已去自证化：在混合质量输入（支持/伪造数字/无证据/冲突并存）上断言「质量属性恒真」（伪造全捕获、无误报、冲突必披露），同时 `supported_rate` 等诚实指标反映真实输入分布、允许小于 1.0——任何把「恒等于 1.0」当成绩的回归都会被抓住。

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 当前状态与边界

**已经做到**：完整的持久化研究 Agent 骨架，生产级的多租户隔离与部署，真实的中文混合检索（Milvus + BGE），以及实测可用的 DeepSeek 联网搜索链路。

**仍未做到（诚实声明）**：

- **formal 生产路径仍是合成 fixture**：`real_rag_local` 跑的是真实 BGE 向量 + Milvus 检索，但语料是带标签的本地演示数据，**不调用实时金融源、也不调用 LLM**，因此不构成真实投资研究。
- **旧原型 DeepSeek 联网搜索是迁移期能力**：模型直连搜索，尚未接入受控 Tool Registry（长期架构要求「模型只能通过受控只读工具检索」）。
- **受控工具已全部接线为真实实现（能力取决于运行时配置）**：`search_web` 通过 DeepSeek Responses 的 `web_search` 执行（需 `DEEPSEEK_API_KEY`，未配置时显式降级）；`search_filings` 对 A 股公司**直连巨潮资讯公告 API**（官方披露平台、无需 key、返回结构化公告），港股或巨潮失败时降级为官方域名白名单网页搜索（`degraded` + `fallback_used=web_search` 显式标记）；`get_quote` 直连腾讯免费行情（A 股/港股/美股，GBK 字段解析为确定性数值：现价、涨跌、PE/PB、市值等），且已由 Planner 纳入研究计划（与财报检索并行执行，实测返回真实行情）；`read_document` 从持久化文档库读取分节内容（需注入 repository，默认降级）；`extract_financial_facts` 用确定性规则从文本抽取带期间/单位/来源的财务事实；`calculate_financial_metrics` 用纯 Python 公式计算指标并回链输入科目；`retrieve_documents` 接入真实 Milvus 混合检索。所有工具在缺输入/缺配置时返回显式 `degraded`，不静默伪造结果。
- **数据源均为非官方授权公开接口，存在限流与格式变化风险**：巨潮公告 API、腾讯免费行情、东方财富 F10 财务摘要均按失败降级处理，可用 `scripts\verify_filings_source.py` / `scripts\verify_quote.py` / `scripts\verify_financial_statements.py` 做真实调用冒烟验证；港股 / 美股的财报数据公开免费源全部不可用，已显式降级到 `search_filings` 兜底（不在受控工具中伪造数字）。
- **真实 PostgreSQL 集成门默认跳过**：`tests\test_postgres_real_integration.py` 在真实 PG 上执行 Alembic 迁移并验证 RLS 租户隔离（SQLite 契约测试无法覆盖），需设置 `FINSCOPE_TEST_PG_URL`（测试文件头部有一次性容器启动命令）；本机已用 Docker 一次性容器实测通过。
- 前端仍是 HTML 原型，正式 API 的用户可见界面（`/api/securities` 别名词典、参考文献端点、双账号隔离演示、SSE 流式渲染、左侧公司实时匹配）已补齐；多 Agent 并行尚未启用。

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
