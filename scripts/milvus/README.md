# Milvus Standalone（Windows 本地开发）

Phase 4 的正式检索后端是 Milvus Standalone。内存检索器只用于单元测试，不会被生产配置自动选中。

## 前置条件

1. 安装并启动 Docker Desktop；
2. Docker Desktop 使用 WSL2 backend；
3. 运行仓库内的 `rag` Compose profile；
4. 确认 `http://127.0.0.1:9091/healthz` 返回 `OK`，再安装 `requirements-rag.txt`。

`rag` profile 使用独立的 etcd、MinIO 和 Milvus volume，不会复用或删除 core profile 的数据。
Milvus gRPC 与健康端口都只绑定 `127.0.0.1`。

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
Copy-Item scripts\milvus\standalone.env.example .env.milvus.local
docker compose --profile rag up -d milvus-etcd milvus-minio milvus
Invoke-WebRequest http://127.0.0.1:9091/healthz
.venv\Scripts\python.exe -m scripts.verify_real_rag
```

应用配置使用 `.env.example` 中的 `MILVUS_URI`、`MILVUS_TOKEN`、`MILVUS_COLLECTION` 和 `MILVUS_INDEX_VERSION`。测试真实 Milvus 时另设 `MILVUS_TEST_URI`；集成测试必须创建唯一 collection，并且只删除它自己创建的准确名称。

官方资料：

- [Install Milvus Standalone on Windows](https://milvus.io/docs/install_standalone-windows.md)
- [Milvus BM25 Function](https://milvus.io/docs/bm25-function.md)
- [Milvus Hybrid Search](https://milvus.io/docs/multi-vector-search.md)
