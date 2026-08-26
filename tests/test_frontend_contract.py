from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "prototype-research-ui" / "unified-agents.html"


def frontend_source() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def test_opening_console_does_not_auto_start_paid_research() -> None:
    source = frontend_source()
    restore_body = source.split("async function restoreOrStart()", 1)[1].split(
        "async function stopResearch()", 1
    )[0]

    assert "await startResearch();" not in restore_body
    assert "showReadyState" in restore_body


def test_new_case_flow_only_asks_for_research_question() -> None:
    source = frontend_source()

    assert 'id="case-dialog"' not in source
    assert 'id="case-company"' not in source
    # 提交时用实时匹配的公司名，未匹配时才 fallback 到“自动识别中”
    assert "company: match ? match.company : '自动识别中'" in source
    assert "Agent 会自动识别公司、股票代码和市场" in source


def test_agent_work_trace_is_inline_collapsible_and_chronological() -> None:
    source = frontend_source()
    taskbar = source.split('<header class="taskbar">', 1)[1].split('</header>', 1)[0]
    document_scroll = source.split('id="document-scroll"', 1)[1].split(
        '<section class="composer"', 1
    )[0]

    assert 'id="research-question"' not in taskbar
    assert 'aria-label="Agent 工作轨迹"' not in taskbar
    assert 'class="research-turn"' in document_scroll
    assert document_scroll.index('id="research-question"') < document_scroll.index('class="case-head"')
    assert document_scroll.index('class="case-head"') < document_scroll.index('class="report"')
    assert document_scroll.index('class="report"') < document_scroll.index(
        'aria-label="Agent 工作轨迹"'
    )
    assert 'id="agent-trace-toggle"' in document_scroll
    assert 'aria-controls="agent-trace-list"' in document_scroll
    assert 'id="agent-trace-state"' in document_scroll
    assert "ui.traceList.append(traceRow)" in source
    assert "traceRow.scrollIntoView" in source
    assert "grid-template-rows:auto minmax(0,1fr) auto" in source


def test_trace_clock_ticks_independently_from_progress_events() -> None:
    source = frontend_source()

    assert "traceTimer: null" in source
    assert "function updateTraceClock()" in source
    assert "setInterval(updateTraceClock, 1000)" in source
    assert "clearInterval(runtime.traceTimer)" in source


def test_report_uses_structured_readable_content_instead_of_one_dense_paragraph() -> None:
    source = frontend_source()
    render_body = source.split("function renderReport", 1)[1].split(
        "function renderEvidence", 1
    )[0]

    assert "function formatResearchContent" in source
    assert 'class="research-points"' in source
    assert "section.points" in render_body
    assert '<p>${escapeHtml(section.content)}' not in render_body


def test_research_plan_row_is_not_rendered() -> None:
    source = frontend_source()

    assert 'aria-label="研究计划"' not in source
    assert 'id="plan-toggle"' not in source
    assert 'id="plan-status"' not in source
    assert "function updateProgress" not in source


def test_stop_action_moves_from_taskbar_to_composer_while_running() -> None:
    source = frontend_source()

    assert 'class="task-actions"' not in source
    assert 'id="scope"' not in source
    assert 'id="pause"' not in source
    assert 'id="stop"' not in source
    assert "function togglePause" not in source
    assert "async function handleComposerAction" in source
    assert "!TERMINAL.has(runtime.taskStatus)" in source
    assert "await stopResearch()" in source
    assert "ui.send.classList.toggle('is-stop', active && !paused)" in source
    assert "const label = paused ? '恢复研究' : (active ? '暂停研究' : '发送')" in source
    assert "runtime.taskId ? '继续研究' : '开始研究'" not in source


def test_search_results_are_an_on_demand_drawer_with_source_context() -> None:
    source = frontend_source()
    render_body = source.split("function renderEvidence(items)", 1)[1].split(
        "function applyTask", 1
    )[0]

    assert 'id="search-results-toggle"' in source
    assert 'id="search-results-drawer"' in source
    assert '<h2>搜索结果</h2>' in source
    assert '研究上下文' not in source
    assert '工作轨迹</button>' not in source
    assert '待确认</button>' not in source
    assert 'class="e-filters"' not in source
    assert '<a class="source"' in render_body
    assert 'href="${escapeHtml(safeUrl(item.url))}"' in render_body
    assert 'target="_blank"' in render_body
    assert 'rel="noreferrer"' in render_body
    assert "source-meta" in render_body
    assert "item.publisher" in render_body
    assert "item.excerpt" in render_body
    assert "setSearchResultsOpen" in source


def test_url_values_are_never_rendered_as_visible_source_titles() -> None:
    source = frontend_source()
    citation_body = source.split("function citationMarkup", 1)[1].split(
        "function renderReport", 1
    )[0]
    render_body = source.split("function renderEvidence(items)", 1)[1].split(
        "function applyTask", 1
    )[0]

    assert "function displaySourceTitle" in source
    assert "displaySourceTitle(evidence" in citation_body
    assert "displaySourceTitle(item" in render_body
    assert "escapeHtml(item.title)" not in render_body


def test_frontend_renders_quick_result_before_task_completion() -> None:
    source = frontend_source()
    stream_body = source.split("function openStream(taskId)", 1)[1].split(
        "async function startResearch", 1
    )[0]

    # EventSource 流式处理 SSE 事件：有 message 就追加 trace 行
    assert "if (event.message) appendLog(event);" in stream_body
    # 终态后仍会拉取最终任务
    assert "await api(`/research/${taskId}`)" in stream_body
    assert "applyTask(task)" in stream_body


def test_frontend_streams_report_draft_without_polluting_trace() -> None:
    source = frontend_source()
    stream_body = source.split("function openStream(taskId)", 1)[1].split(
        "async function startResearch", 1
    )[0]

    assert 'id="report-draft"' in source
    assert "function appendReportDelta(delta)" in source
    assert "function markdownDraftMarkup(text)" in source
    assert "event.kind === 'report.delta'" in stream_body
    assert "appendReportDelta(event.payload.delta)" in stream_body
    assert "if (event.message) appendLog(event);" in stream_body
    assert "resetStreamingDraft()" in source


def test_feedback_restarts_running_research_with_updated_question() -> None:
    source = frontend_source()
    feedback_body = source.split("async function submitFeedback()", 1)[1].split(
        "function bindPromptButtons", 1
    )[0]

    assert "正在安全暂停当前轮并应用反馈" in feedback_body
    assert "await api(`/research/${runtime.taskId}/pause`" in feedback_body
    assert "继续研究：${message}" in feedback_body


def test_frontend_labels_deepseek_results_and_resets_elapsed_time_per_task() -> None:
    source = frontend_source()
    apply_body = source.split("function applyTask(task)", 1)[1].split(
        "async function loadCases", 1
    )[0]

    assert "provider === 'deepseek' ? 'DeepSeek 联网研究'" in source
    assert "runtime.provider = health.provider || health.mode" in source
    assert "const switchedTask = runtime.taskId !== task.id" in apply_body
    assert "if (switchedTask) runtime.startedAt = Date.parse(task.created_at || '') || Date.now()" in apply_body
    assert "runtime.startedAt = runtime.startedAt ||" not in source


def test_frontend_understands_all_six_durable_run_states() -> None:
    source = frontend_source()

    for state in ("running", "pause_requested", "paused", "resuming", "failed", "completed"):
        assert state in source
    assert "正在安全暂停" in source
    assert "正在校验检查点并恢复" in source


def test_composer_is_unified_single_company_analysis_agent() -> None:
    """The composer no longer exposes three agent choices; a single company
    analysis agent handles prices, financials and filings together."""
    source = frontend_source()
    composer = source.split('<section class="composer"', 1)[1].split('</section>', 1)[0]
    rail = source.split('<aside class="rail"', 1)[1].split('</aside>', 1)[0]

    assert 'agent-picker-toggle' not in source
    assert 'agent-picker-menu' not in source
    assert '公司分析 Agent' in source
    assert '财报分析 Agent' not in source
    assert '市场分析 Agent' not in source
    assert '公司研究 Agent' not in source
    assert 'id="context-company"' not in composer
    assert 'id="send"' in composer


def test_composer_uses_a_compact_icon_send_action_without_company_chip() -> None:
    source = frontend_source()

    assert 'id="context-company"' not in source
    assert "contextCompany:" not in source
    assert '.send::before{content:"↑"' not in source
    assert 'class="send-icon"' in source
    assert 'd="M12 20V4m0 0-6.5 6.5M12 4l6.5 6.5"' in source
    assert '<span class="sr-only">发送</span>' in source
