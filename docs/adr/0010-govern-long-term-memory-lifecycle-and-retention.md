# ADR-0010：治理长期记忆生命周期、作用域与保留期

- 状态：Accepted
- 日期：2026-08-13

## 背景

长期记忆能让 Agent 跨 case 复用用户偏好和经验证公司事实，但也会放大错误事实、过期数据、提示注入和跨用户泄漏。记忆不能等同于聊天历史或向量检索结果，也不能绕过本次研究所需的新证据。

## 决策

采用白名单验证写入和版本化生命周期：

```text
candidate -> verified -> active -> superseded / expired / deleted
                  \-> rejected
candidate -> conflicted
```

- 只有 `active` 且未过期、未 tombstone 的记忆可以进入 Context Builder；
- 公司研究事实必须关联已持久化 Evidence，默认 TTL 为 90 天；
- 用户偏好只接受用户明确表达，持续到用户修改或删除；
- 公司身份映射 TTL 为 180 天；会话摘要为 30 天；待验证或冲突候选为 7 天；
- 公开公司事实按 `company/symbol/market` 共享；用户偏好按 `tenant_id/user_id` 隔离；私人 case 按 `tenant_id/user_id/case_id` 隔离；
- 本地 Demo 使用固定 `tenant_id=local`、`user_id=default`，Repository 仍强制所有访问携带作用域；
- 新事实不原地覆盖旧事实。一致事实增加证据并刷新 TTL；冲突时停止旧事实注入；更新期间或更高权威证据验证通过后，新版本 active，旧版本 superseded；
- 记忆检索先执行关系库状态、TTL、权限和实体过滤，Milvus 混合检索只对合格候选排序，不能授予权限或真实性；
- 两阶段删除先写 tombstone 并立即停止读取，再异步清理私人正文、Milvus 派生向量和缓存；审计仅保留匿名哈希和操作元数据；
- 记忆只辅助理解与规划，涉及最新事实仍必须重新获取工具证据。

## 影响

关系数据库成为记忆状态、版本、证据关系和删除队列的唯一事实源。Milvus 可以保存可重建的 memory retrieval index，但索引中不得存在已过期、已删除或越权记忆的可读结果。

该方案增加状态迁移、后台过期和删除协调成本，但支持可审计纠正、事实演进、用户控制和未来多租户迁移。

## 备选方案

- 所有报告自动写入：错误和提示注入污染风险过高，拒绝；
- 新事实原地覆盖：无法审计事实演进和恢复，拒绝；
- 仅向量相似度检索：无法保证权限、有效期与真实性，拒绝；
- 到期立即物理删除所有记录：会破坏审计与共享事实引用，拒绝。

## 参考

- [ADR-0004：分离关系事实源、记忆、RAG 与对象存储](0004-separate-memory-rag-and-source-of-truth.md)
- [主架构：记忆系统](../architecture/durable-research-agent.md#10-记忆系统)
