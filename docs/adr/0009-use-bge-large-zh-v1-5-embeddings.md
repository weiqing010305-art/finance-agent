# ADR-0009：使用 BGE Large 中文向量模型

- 状态：Accepted
- 日期：2026-08-12

## 背景

ADR-0008 已确定 Milvus BM25 sparse 与 dense embedding 的混合检索路线，但尚未固定稠密向量模型、维度和查询编码规则。金融研究语料以中文为主，需要稳定处理公司名称、会计科目和语义改写，同时必须保证 collection schema、查询向量和文档向量不会因模型隐式变化而失配。

## 决策

Phase 4 默认使用 `BAAI/bge-large-zh-v1.5`：

- 输出维度固定为 1024；
- 输出向量归一化，Milvus 使用内积相似度；
- query 使用 `为这个句子生成表示以用于检索相关文章：` 指令；
- document chunk 不添加 query instruction；
- 模型名称、revision、维度、指令和 normalize 配置共同组成不可变 `embedding_profile_id`；
- Milvus 记录 embedding profile 与 index version，profile 变化必须重建索引，禁止新旧向量混用；
- 模型延迟加载，普通单元测试使用明确标识为 test-only 的确定性替身，不下载模型权重，也不把替身指标当成 BGE 质量指标。

## 影响

选择 1024 维模型会增加内存、索引空间和本地推理成本，但能提供适合中文语义检索的明确基线。真实 Milvus 与真实 BGE 的质量和延迟必须单独评测；如果未来切换模型，只能创建新 profile/index version 并完成重建和回归评测，不能原地覆盖现有向量。

独立 reranker 仍不是默认组件。只有固定评测集证明其收益足以覆盖延迟和服务成本时，才通过后续 ADR 启用。

## 参考

- [BAAI/bge-large-zh-v1.5 model card](https://huggingface.co/BAAI/bge-large-zh-v1.5)
- [ADR-0008：使用 Milvus 混合检索](0008-use-milvus-hybrid-retrieval.md)
