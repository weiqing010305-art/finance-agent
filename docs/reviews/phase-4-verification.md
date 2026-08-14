# Phase 4 Verification

> 日期：2026-08-12
> 状态：独立 subagent 审查通过（代码/离线范围）；真实 Milvus/BGE 门禁未执行，等待用户验收

## 已实现

- SQLite schema v11：不可变 documents/versions/chunks、带 fencing lease 的 ingestion jobs、Evidence/Claim 图、report generations/snapshots/reports/citations；
- HTML/UTF-8 归一化、确定性 heading-aware chunk、稳定 ID、内容哈希和幂等摄取；
- `BAAI/bge-large-zh-v1.5` embedding profile：固定 revision、1024 维、归一化、query instruction 与文档侧无指令；
- Milvus Standalone collection facade：原生 BM25 sparse、1024 维 dense、IP、RRF、严格 metadata/access filter 和显式版本；
- test-only 内存 BM25+dense+RRF oracle；生产配置不会静默切换到它；
- `retrieve_documents` 真实 adapter：只能收窄到 run 已确认的公司/代码/市场/public scope；dense 或 sparse 单路失败可显式降级，两路失败 fail closed；
- Evidence Builder、数字/期间/单位/access/权威性/冲突 Claim Verifier；
- 引用约束确定性报告：只接收 supported/partially supported Claim 和已知 Evidence ID，拒绝未知 ID 与文案新增数字；
- report snapshot 先落库后发布 `report.delta`，事件含累计快照；最终 report/citations/checkpoint/report.completed/run.completed 单事务提交；
- Phase 3 研究链完成后自动进入 Phase 4，成功或证据不足报告均推进到 completed/100%，后者明确 degraded。

## 验证命令与结果

```text
.venv\Scripts\python.exe -m pytest --basetemp=.test-tmp-phase4-final -p no:cacheprovider -q
287 passed, 1 skipped, 1 warning in 16.95s

.venv\Scripts\python.exe -m compileall -q backend tests evals
git diff --check
两项通过；diff-check 仅输出工作区 LF→CRLF 提示，无 whitespace error。
```

Phase 4 离线 smoke：

```json
{
  "profile": "in_memory_test_smoke",
  "case_count": 4,
  "recall_at_3": 1.0,
  "mrr_at_3": 1.0,
  "ndcg_at_3": 1.0,
  "citation_coverage": 1.0,
  "citation_integrity": 1.0,
  "numeric_provenance_rate": 1.0,
  "real_milvus_executed": false
}
```

这些数值只证明离线合同、排序和引用安全 smoke，不代表真实 BGE/Milvus 质量。真实集成测试只有设置 `MILVUS_TEST_URI` 后才执行，本轮本机 Docker daemon 未运行，因此明确 skipped。

独立 subagent 最终结论为：`PASS (offline/code scope), NOT EXECUTED (real Milvus gate)`。当前真实 integration test 已实现 UUID 唯一 collection 的 create/upsert/flush/hybrid search/filter/finally 精确清理，不是占位测试；本轮只是由于未配置 `MILVUS_TEST_URI` 而没有执行。

历史评测同轮复跑：Phase 2 路由 92 cases，accuracy/research recall 均 1.0，false permission/planner activation 均 0；Phase 3 实体 7 cases 与 3 planner smoke 均 1.0。唯一 warning 是既有 Starlette TestClient/httpx 弃用提醒。

## 恢复与安全不变量

- 关系库是文档版本、摄取状态、Evidence、Claim、Report 的事实源；Milvus 是可删除重建索引；
- 模型名/revision/dimension/instruction/normalize 共同形成 embedding profile，索引版本不匹配的 chunk 不参与检索；
- 文档中的提示词始终按不可信数据处理，不进入系统指令；URL、Evidence、snapshot 和最终报告落库前统一脱敏；
- Evidence/Claim ID 重试必须保持同一身份，不同内容复用 ID 会回滚；
- SSE 只能发送已落库事件，Last-Event-ID 可以重放累计报告快照；
- 未经 verifier 支持的 Claim、未知引用、越权 Evidence 和新增数字不能提交最终 Report；
- 最终写入失败时事务回滚，run 保持最后已提交状态；终态 trigger 继续保护 completed 不变量。

## 已知限制

- 真实 BGE 模型权重、PyMilvus 和 Milvus Standalone 未在本轮机器上运行，真实 Recall/延迟/索引容量尚未测量；
- 当前正式工具只有 `retrieve_documents` 接入真实 adapter，其余 filings/web/read/fact tools 仍保持显式 unconfigured/degraded；
- 当前报告默认使用确定性 renderer；受限 LLM 报告 adapter 尚未启用，但其输入输出门禁已经由 Report schema/validator 定义；
- 原始文件对象存储、PDF/OCR/表格专用解析器与完整摄取管理 API 仍未实现；当前完成的是受控 HTML/plain-text 摄取内核；
- 当前 Milvus 返回结果尚未执行权威性、时效性和同源多样性后排序；工具输出中的 publisher 不被信任，extractive baseline 统一按低权威处理；
- 当前没有 URL 抓取入口，因此 URL allowlist、DNS/IP 私网阻断和重定向门禁尚未实现；接入 search/read 下载器前必须补齐；
- ingestion claim 使用 5 分钟 fencing lease 且没有 heartbeat；超长 embedding/upsert 可能被重放，Milvus upsert 依赖稳定 chunk ID 保持幂等；
- Milvus 单路降级基于客户端分别搜索后本地 RRF；真实服务兼容性仍需集成环境验证；
- 长期记忆、candidate/verify/supersede 和隐私删除属于 Phase 5；PostgreSQL、多租户私有文档和公开部署属于 Phase 6。
