# FinScope Research UI — Frontend MVP

当前目录承载 FinScope 的前端 MVP 基线。后续功能在已选定的案卷控制台结构上迭代，不再继续更换整体视觉方向。

## 已选方向

`unified-agents.html` 是已确认的前端 MVP，采用三 Agent 统一案卷控制台并支持切换：

- 财报分析 Agent
- 市场分析 Agent
- 公司研究 Agent

三个 Agent 共享同一公司案卷和统一证据台账。当前页面是前端 MVP 与目标态交互演示，数据与结论均为合成内容。

直接用浏览器打开 `index.html`，页面会进入已选定的 `unified-agents.html` 案卷控制台。

`case-console-prototype.html` 保留为本轮样式原型参考；正式前端入口以 `unified-agents.html` 为准。

页面中的公司结论和来源摘录均为合成演示内容，不代表真实研究结果。

## 本地运行

后端使用项目内 Python 虚拟环境和 SQLite：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8780
```

前端入口仍为 `http://127.0.0.1:8770/`。启动后端后，页面会创建或恢复模拟财报研究任务，并通过 Server-Sent Events 显示进度；暂停、继续、停止和发送反馈均调用真实 API。

当前执行器仍是模拟研究链路，下一阶段才接入真实搜索、网页与在线 PDF 阅读。
