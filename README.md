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
├── backend/                 # 模块化单体后端（46 个模块）
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
├── alembic/                 # 数据库迁移（13 个版本，0001–0013）
├── docs/                    # 设计 / 计划 / 验证 / ADR 决策文档
├── evals/                   # 评测用例与打分器
├── infra/                   # Caddy / Grafana / Loki / OTel / Prometheus / PG 配置
├── prototype-research-ui/   # 前端原型（案卷控制台）
├── scripts/                 # 运维与验证脚本（local.ps1、backup、verify_*）
└── tests/                   # 68 个测试文件（含 integration）
```

## 测试与验证

- 全量 pytest：**425 通过、3 跳过**（跳过项为需要真实 Milvus/BGE 环境的外部集成门）。
- 测试覆盖：持久化 Runner 契约、RLS 租户隔离、证据/引用链完整性、迁移契约、幂等重放、跨租户隔离、备份恢复等。
- 离线评测：Phase 3/4/5 的意图路由、RAG、记忆评测用例均可离线运行。

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 当前状态与边界

**已经做到**：完整的持久化研究 Agent 骨架，生产级的多租户隔离与部署，真实的中文混合检索（Milvus + BGE），以及实测可用的 DeepSeek 联网搜索链路。

**仍未做到（诚实声明）**：

- **formal 生产路径仍是合成 fixture**：`real_rag_local` 跑的是真实 BGE 向量 + Milvus 检索，但语料是带标签的本地演示数据，**不调用实时金融源、也不调用 LLM**，因此不构成真实投资研究。
- **旧原型 DeepSeek 联网搜索是迁移期能力**：模型直连搜索，尚未接入受控 Tool Registry（长期架构要求「模型只能通过受控只读工具检索」）。
- 尚未接入实时行情 / 财报 / 监管披露等数据源 API。
- 前端仍是 HTML 原型，正式 API 的用户可见界面（参考文献端点、双账号隔离演示）刚补齐，多 Agent 并行尚未启用。

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
