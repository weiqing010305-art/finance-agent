# Phase 4 RAG、证据验证与流式报告设计

## 1. 目标与验收边界

Phase 4 将 Phase 3 已完成的研究步骤连接到可重建文档索引、混合检索、证据验证和最终报告。正式检索后端使用 Milvus Standalone；离线单元测试使用实现同一接口的内存替身。内存实现只验证合同、排序和故障路径，不作为 Milvus 质量证明。

本阶段完成后，任务必须从 Phase 3 的 `running / awaiting_report / 95%` 安全推进到 `completed / 100%`，或在证据不足、索引不可用、生成失败时进入明确的 degraded/failed 路径。任何最终核心结论和数值都必须能定位到已持久化 Evidence；无法验证的内容不能包装成成功报告。

## 2. 已冻结的技术决策

- Vector DB：Milvus Standalone，默认地址 `http://127.0.0.1:19530`，也支持外部 Milvus URI/token。
- Dense embedding：`BAAI/bge-large-zh-v1.5`，1024 维，归一化向量，内积相似度。
- Query instruction：`为这个句子生成表示以用于检索相关文章：`；文档 chunk 不加 instruction。
- Sparse retrieval：Milvus BM25 Function。
- Fusion：默认 Reciprocal Rank Fusion；Weighted Ranker 与独立 reranker 只有评测证明收益后才启用。
- 关系数据库是文档版本、摄取任务、Evidence、Claim、Report 的事实源；Milvus 是可重建派生索引。
- 本机 Windows 不使用 Milvus Lite。真实集成通过 Docker Desktop + WSL2 的 Milvus Standalone 或外部服务完成。

## 3. 组件与数据流

```text
Document Source
  -> Source policy / SSRF guard
  -> immutable document version + sha256
  -> parser / normalized sections
  -> deterministic chunker
  -> BGE document embeddings
  -> Milvus upsert (dense + BM25 sparse + metadata)
  -> ingestion reconciliation

Research question
  -> metadata filter (company/market/period/type/access scope)
  -> BGE query embedding
  -> Milvus dense search + BM25 search
  -> RRF fusion
  -> authority/freshness/diversity policy
  -> Evidence Pack
  -> Claim extraction + deterministic/LLM verification
  -> citation-constrained report
  -> persisted report snapshot + report.delta events
  -> run.completed
```

### 3.1 Document Store

新增关系表保存 `documents`、`document_versions`、`document_chunks` 和 `ingestion_jobs`。文档版本以来源、版本标识和 SHA-256 去重；正文可以先保存在本地受控目录，数据库保存绝对归一化存储键、哈希、MIME、大小、获取时间、发布者、发布日期和访问作用域。索引写入成功后才把 ingestion job 标为 indexed；中间崩溃由 reconciler 根据关系库事实重放。

### 3.2 Embedding Provider

定义 `EmbeddingProvider` 协议，区分 `embed_queries()` 和 `embed_documents()`。默认 `BgeLargeZhEmbeddingProvider` 延迟加载模型，固定输出维度 1024，校验有限数值并进行归一化。模型标识、revision、query instruction、维度、normalize 和运行设备形成不可变 `embedding_profile_id`。测试使用确定性 Hash Embedding，不冒充 BGE。

### 3.3 Hybrid Retriever

`HybridRetriever` 提供 Milvus 与 InMemory 两个实现。统一请求必须携带 query、top-k、metadata filter、embedding profile 和 index version；统一响应必须含 dense/sparse/fused 分数、rank、document/version/chunk/source/page/section、模型与索引版本。Milvus 查询使用 dense ANN 与 BM25 两路召回，再用 RRF 融合。当前内存 oracle 验证同源多样性；Milvus 正式路径尚未实现来源权威性、时效性和同源去重后策略，因此这些属于 Phase 4 已知限制，不作为本阶段已完成能力。

## 4. Evidence、Claim 与验证

Evidence 是可引用事实单元，不等同于原始 chunk。规范化字段至少包括：`evidence_id`、来源/文档/版本/chunk、标题、发布者、URL、页码/章节、原文摘录、获取时间、发布日期、公司/期间、source_type、authority tier、content hash 和 access scope。

Claim 是报告准备陈述的原子命题。Claim Verifier 先执行确定性检查：

- 引用的 Evidence 必须存在且属于当前 run 可访问范围；
- 数值包含期间、单位、币种和来源；
- 引用摘录必须包含或可解析出相关实体/数值；
- 同一 Claim 的冲突来源不能静默合并；
- 过期、低权威或单一来源需要降低置信度；
- Evidence 不足时标记 `unsupported` 或 `conflicted`。

LLM 只能对已选 Evidence 做受限语义支持判断和报告组织，不能新增 URL、数值或来源。Verifier 输出 `supported / partially_supported / unsupported / conflicted`、理由码和 Evidence IDs。只有 supported 或明确披露限制的 partially_supported Claim 可进入报告。

## 5. 报告生成与流式恢复

报告先形成结构化 `ReportDraft`：公司身份、研究问题、摘要、章节、Claims、限制、方法和引用列表。每个章节的事实句携带 Claim ID，Claim 再关联 Evidence IDs。最终渲染器生成 Markdown/JSON 两种表示，并把引用编号映射为稳定 Evidence ID。

生成过程以小段 delta 输出，但 SSE 只发送已持久化事件。`report_generations` 保存 prompt/model/schema/version/status；`report_snapshots` 保存递增序号、完整累计文本或结构化快照。崩溃恢复从最后 snapshot 继续：如果模型不支持 continuation，则基于已验证 Claims 确定性重建完整报告，不能把未持久化 token 当成已完成内容。

完成事务一次性写入最终 Report、Evidence 引用映射、最终 checkpoint、`report.completed` 和 `run.completed`。若 Verifier 没有足够支持，生成“证据不足报告”或 fail closed，不写虚假 completed。

## 6. 失败、降级与安全

- Milvus 不可用：真实运行默认 fail closed；只有显式配置才允许使用已持久化 Evidence 生成 degraded 报告。内存检索只用于 test profile。
- Embedding 失败：可降级到 Milvus BM25-only，但必须写 `degraded=true`、原因和缺失 dense profile；不能宣称完成混合检索。
- BM25 失败：可用 dense-only，同样明确降级。
- 文档解析失败：保留原文版本，标记 parsing_failed；不索引空文本。
- 索引写成功但关系库提交失败：reconciler 依据 document/version/chunk/index key 幂等覆盖或清理孤儿记录。
- 网页/PDF 均视为不可信数据；解析文本中的提示词永不进入系统指令。
- 当前摄取内核只接受调用方已取得的 HTML/plain text，并实施大小、UTF-8 与统一脱敏。URL allowlist、DNS/IP 私网阻断、重定向、PDF 页数与 MIME 下载门禁尚无抓取入口，属于后续真实 search/read tool adapter 的前置安全要求。
- Milvus metadata filter 必须包含访问作用域；Phase 4 Demo 仅索引公开公司资料，用户私有文档留到多租户权限完成后启用。

## 7. 评测与验收

Phase 4 至少包含：

1. 固定中文金融 query/document 集的 sparse、dense、hybrid Recall@k、MRR 与 nDCG；
2. BM25-only、dense-only、RRF hybrid 消融；
3. metadata filter、同源去重、权威性/时效性排序；
4. 目标 chunk 召回率与端到端引用准确率；
5. 数值期间/单位/币种一致性与冲突来源；
6. 无证据、脏 PDF、重复文档、版本更新和索引重建；
7. Milvus/embedding/BM25/LLM 故障注入；
8. report.delta 断线续传、生成中崩溃和最终完成事务；
9. 真 Milvus 集成测试与离线替身测试分别标记，指标不得混用；
10. subagent 独立审查通过，并由用户验收后才进入 Phase 5。

## 8. 明确不在 Phase 4 内

- 长期记忆 candidate/verify/supersede（Phase 5）；
- PostgreSQL、多租户私有文档、对象存储和公开部署（Phase 6）；
- 默认独立 reranker、多 Agent、模型自动选择；
- 把 Milvus 作为运行状态或权限事实源。
