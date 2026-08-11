from pathlib import Path


def test_langgraph_and_langchain_imports_are_available():
    from langchain_core.messages import HumanMessage
    from langgraph.graph import StateGraph

    assert HumanMessage(content="hello").content == "hello"
    assert StateGraph is not None


def test_adr_keeps_durable_runner_as_lifecycle_source_of_truth():
    adr = Path("docs/adr/0005-adopt-langgraph-langchain-hybrid-orchestration.md")
    content = adr.read_text(encoding="utf-8")

    assert "Durable Runner 继续作为业务运行生命周期的唯一事实源" in content
    assert "Phase 2 不启用第二套 LangGraph 持久化数据库" in content
