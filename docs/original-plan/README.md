# AI 金融三条项目线 MVP

从零起步、可离线运行的最小可用版本。三条线共享底座，主线 3 复用主线 1/2 的能力。

## 目录结构
```
common/                  共享工具（离线 embedding + 样本数据）
  embeddings.py          轻量中文向量检索（替代 sentence_transformers + Chroma）
  sample_data.py         样本年报 + 样本行情 + 新闻/评级
line1/                   主线1：财报 RAG 助手（用 Strands）
  financial_rag.py        解析->切片带页码->向量库->retrieve 工具->Agent+Hook
line2/                    主线2：股票分析平台（不用 Strands，无回测）
  stock_analysis.py      akshare/样本 -> MACD/RSI/KDJ/均线/布林带 -> 信号
  app.py                 Streamlit 看板（本地运行）
line3/                    主线3：股票研究 Agent（用 Strands 编排）
  research_agent.py       一句话分析 -> 调度四个专家 -> 汇总 -> Handoff 转人工
verify_all.py            离线验证脚本（跑通三条线核心逻辑）
```

## 概念对应（你之前学过的）
- 切片/Embedding/向量库/重排：主线1 的 RAG 底座
- @tool：主线1 retrieve、主线3 四个专家都是工具
- Hook：主线1 保存报告强制带页码
- Loop / Loop Engineering：Strands Agent 自带循环 + max_iterations 油门
- 多 Agent 编排 / 路由：主线3 总管调度四个专家
- Handoff（转人工）：主线3 含投资建议时转人工审核
- 记忆/Memory：多年对比 = 长期记忆（= RAG，把历年指标存库检索）

## 运行环境（重要，先看这节）
脚本用 Python 跑。最少依赖只有三个：**pandas / numpy / akshare**（verify_all 离线可跑只需它们）。

若你直接 `python verify_all.py` 报 `ModuleNotFoundError: No module named 'pandas'`，
说明你的默认 `python` 没装依赖。两种解决：
1. 用 WorkBuddy 自带的 managed Python（已预装 pandas/numpy/akshare）：
   ```
   C:\Users\sinz-\.workbuddy\binaries\python\versions\3.13.12\python.exe verify_all.py
   ```
2. 在自己常用的 Python 里安装： `pip install pandas numpy akshare`

## 怎么跑（离线，零额外安装）
```
python verify_all.py          # 或上面的 managed Python 长路径
```
会依次跑通：主线1 财报检索带页码、主线2 指标计算与信号、主线3 研究 Agent 复用演示。

单线运行：
```
python -m line1.financial_rag
python -m line2.stock_analysis
python -m line3.research_agent
```

## 联网时自动升级
- 主线2：akshare 能联网时 `analyze_stock(code, use_akshare=True)` 拉真实行情。
- 主线1：装 `pdfplumber` 后 `load_report("xxx.pdf")` 解析真实 PDF。
- 主线1/3：装 `strands-agents` + 本地 `ollama pull qwen2.5:7b` 后，Agent 层启用模型驱动
  （注意：跑 Agent 前必须本地已启动 Ollama 服务并拉好模型，否则会连接报错）。

## 安装进阶依赖（按需）
最小跑通： `pip install pandas numpy akshare`
真实能力（按需要装）：
```
pip install pdfplumber            # 主线1 解析真实 PDF
pip install chromadb sentence-transformers   # 主线1 真向量库 + 真 Embedding（bge-zh）
pip install strands-agents strands-agents-tools  # 主线1/3 模型驱动 Agent
pip install streamlit             # 主线2 看板
```
装好 strands + ollama 后：主线1 用真实 PDF + Chroma + 真 Embedding 跑 RAG；主线3 用 Strands 真正让模型自己编排。

## 沙箱注意事项
当前环境 pypi 可联网，但 akshare 的行情接口（eastmoney）被代理拦截，
所以主线2 默认用样本数据；你本地能联网时会自动切真实数据。

