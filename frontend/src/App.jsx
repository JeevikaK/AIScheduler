import { useEffect, useMemo, useRef, useState } from "react";
import AnimatedShaderBackground from "./components/ui/animated-shader-background.jsx";

const ACTIVE_THREAD_STORAGE_KEY = "ai_scheduler_active_threads_v2";

const QUICK_ACTIONS = [
  {
    id: "see-schedule",
    label: "See schedule",
    template: "Show me my schedule for ",
  },
  {
    id: "replan-activity",
    label: "Replan activity",
    template: "Replan ",
  },
];

const INITIAL_INSIGHTS = {
  best_productivity_window: "--",
  efficiency: {
    completion_rate_7d: 0,
    completion_rate_30d: 0,
    duration_accuracy: 0,
  },
  last_7_days: [],
  last_30_days: [],
  productivity_hours: [],
};

const INITIAL_SCHEDULE = {
  date: todayIso(),
  tasks: [],
  energy: "-",
  message: "No schedule loaded yet.",
};

export default function App() {
  const searchParams = typeof window !== "undefined"
    ? new URLSearchParams(window.location.search)
    : null;
  const sidebarOverride = searchParams?.get("sidebar") || null;
  const sidebarSectionOverride = searchParams?.get("sidebarSection") || null;
  const [view, setView] = useState("chat");
  const [threadDate] = useState(todayIso());
  const [threadId, setThreadId] = useState(null);
  const [threads, setThreads] = useState([]);
  const [messages, setMessages] = useState([]);
  const [options, setOptions] = useState([]);
  const [optionsAllowFreeText, setOptionsAllowFreeText] = useState(true);
  const [pendingIntentId, setPendingIntentId] = useState(null);
  const [scheduleToday, setScheduleToday] = useState(INITIAL_SCHEDULE);
  const [insights, setInsights] = useState(INITIAL_INSIGHTS);
  const [chatInput, setChatInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [threadsAvailable, setThreadsAvailable] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    sidebarOverride
      ? sidebarOverride === "collapsed"
      : localStorage.getItem("ai_scheduler_sidebar_collapsed") === "true",
  );
  const [openSidebarSection, setOpenSidebarSection] = useState(
    sidebarOverride === "collapsed" ? null : (sidebarSectionOverride || "upcoming"),
  );

  const chatMessagesRef = useRef(null);
  const inputRef = useRef(null);
  const upcomingSectionRef = useRef(null);
  const threadsSectionRef = useRef(null);
  const threadIdRef = useRef(null);
  const threadsRef = useRef([]);
  const messagesRef = useRef([]);
  const threadsAvailableRef = useRef(true);

  const hasConversation = messages.length > 0;
  const workspaceDate = formatWorkspaceDate(new Date());
  const upcomingTasks = getUpcomingTasks(scheduleToday.tasks || []).slice(0, 3);

  useEffect(() => {
    boot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (chatMessagesRef.current) {
      chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    threadIdRef.current = threadId;
  }, [threadId]);

  useEffect(() => {
    threadsRef.current = threads;
  }, [threads]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    threadsAvailableRef.current = threadsAvailable;
  }, [threadsAvailable]);

  async function boot() {
    await Promise.all([refreshInsights(), refreshTodaySchedule()]);
    const targetThreadId = await refreshThreadCatalog({ preserveActive: false });
    if (targetThreadId && messagesRef.current.length === 0) {
      await activateThread(targetThreadId);
    }
  }

  async function fetchJson(url, optionsValue) {
    const response = await fetch(url, optionsValue);
    if (!response.ok) {
      throw new Error(`${url} failed with ${response.status}`);
    }
    return response.json();
  }

  async function refreshInsights() {
    const data = await fetchJson("/dashboard/overview");
    setInsights(data);
  }

  async function refreshTodaySchedule() {
    const data = await fetchJson("/schedule/today");
    setScheduleToday(data);
  }

  async function refreshThreadCatalog({ preserveActive = true } = {}) {
    let items;
    try {
      items = await fetchJson(`/chat/threads?thread_date=${threadDate}`);
      setThreadsAvailable(true);
    } catch (error) {
      if (isThreadsEndpointError(error)) {
        setThreadsAvailable(false);
        setThreads([]);
        return null;
      }
      throw error;
    }
    const normalized = items.map(normalizeServerThread);
    setThreads(normalized);

    if (!normalized.length) {
      const created = await createNewThread({ activate: true, replaceThreads: true });
      return created;
    }

    const currentThreadId = threadIdRef.current;
    const activeThreadId = getStoredActiveThreadId(threadDate);
    const targetThreadId = normalized.some((thread) => thread.id === currentThreadId)
      ? currentThreadId
      : normalized.some((thread) => thread.id === activeThreadId)
        ? activeThreadId
        : normalized[0].id;

    if (preserveActive && currentThreadId && targetThreadId === currentThreadId && messagesRef.current.length > 0) {
      return targetThreadId;
    }

    await activateThread(targetThreadId, normalized);
    return targetThreadId;
  }

  async function activateThread(targetThreadId, threadList = threadsRef.current) {
    if (!targetThreadId) return;
    if (!threadsAvailable) return;
    const thread = threadList.find((item) => item.id === targetThreadId);
    if (!thread) return;

    const detail = await fetchJson(`/chat/threads/${encodeURIComponent(targetThreadId)}?thread_date=${threadDate}`);
    setThreadId(targetThreadId);
    setPendingIntentId(detail.latest_response?.meta?.pending_intent_id || detail.pending_intent_id || null);
    setMessages((detail.messages || []).map((message) => ({
      role: message.role,
      text: message.text,
      meta: message.meta || "",
      payload: message.payload || null,
    })));
    setOptions(getPendingOptionsFromResponse(detail.latest_response));
    setOptionsAllowFreeText(getPendingAllowFreeText(detail.latest_response));
    setStoredActiveThreadId(threadDate, targetThreadId);
  }

  async function createNewThread({ activate = true, replaceThreads = false } = {}) {
    if (!threadsAvailableRef.current) {
      if (activate) {
        setThreadId(null);
        setMessages([]);
        setOptions([]);
        setOptionsAllowFreeText(true);
        setPendingIntentId(null);
        setView("chat");
        inputRef.current?.focus();
      }
      return { id: null };
    }

    let createdResponse;
    try {
      createdResponse = await fetchJson(`/chat/threads?thread_date=${threadDate}`, {
        method: "POST",
      });
      setThreadsAvailable(true);
    } catch (error) {
      if (isThreadsEndpointError(error)) {
        setThreadsAvailable(false);
        if (activate) {
          setThreadId(null);
          setMessages([]);
          setOptions([]);
          setOptionsAllowFreeText(true);
          setPendingIntentId(null);
        }
        return { id: null };
      }
      throw error;
    }

    const created = normalizeServerThread(createdResponse);

    setThreads((current) => {
      const base = replaceThreads ? [] : current;
      return [created, ...base.filter((item) => item.id !== created.id)];
    });

    if (activate) {
      setThreadId(created.id);
      setMessages([]);
      setOptions([]);
      setOptionsAllowFreeText(true);
      setPendingIntentId(null);
      setStoredActiveThreadId(threadDate, created.id);
      await activateThread(created.id, [created, ...threadsRef.current.filter((item) => item.id !== created.id)]);
      setView("chat");
      inputRef.current?.focus();
    }

    return created;
  }

  async function ensureActiveThread() {
    if (!threadsAvailableRef.current) return null;

    const currentThreadId = threadIdRef.current;
    const currentThreads = threadsRef.current;

    if (currentThreadId && currentThreads.some((thread) => thread.id === currentThreadId)) {
      return currentThreadId;
    }

    if (!currentThreads.length) {
      const created = await createNewThread({ activate: true });
      return created.id;
    }

    const storedThreadId = getStoredActiveThreadId(threadDate);
    const targetThreadId = currentThreads.some((thread) => thread.id === storedThreadId)
      ? storedThreadId
      : currentThreads[0].id;

    await activateThread(targetThreadId, currentThreads);
    return targetThreadId;
  }

  function appendMessage(role, text, meta = "", payload = null) {
    setMessages((current) => [...current, { role, text, meta, payload }]);
    if (role === "user") {
      updateActiveThreadDetails({
        title: deriveThreadTitle(text),
        preview: text,
        updatedAt: new Date().toISOString(),
      });
    }
  }

  function updateActiveThreadDetails(updates) {
    setThreads((current) => current.map((thread) => {
      if (thread.id !== threadId) return thread;
      return {
        ...thread,
        title: updates.title || thread.title,
        preview: updates.preview || thread.preview,
        pendingIntentId: Object.prototype.hasOwnProperty.call(updates, "pendingIntentId")
          ? updates.pendingIntentId
          : thread.pendingIntentId,
        updatedAt: updates.updatedAt || new Date().toISOString(),
      };
    }));
  }

  async function sendChat({ message, pendingResponse = null }) {
    setIsSending(true);

    try {
      const activeId = await ensureActiveThread();
      const data = await fetchJson("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          ...(activeId ? { chat_thread_id: activeId } : {}),
          thread_date: threadDate,
          ...(pendingResponse ? { pending_response: pendingResponse } : {}),
        }),
      });

      setPendingIntentId(data.meta?.pending_intent_id || null);
      appendMessage("assistant", buildAssistantMessage(data), summarizeResponse(data), data);
      setOptions(getPendingOptionsFromResponse(data));
      setOptionsAllowFreeText(getPendingAllowFreeText(data));

      updateActiveThreadDetails({
        preview: message,
        updatedAt: new Date().toISOString(),
        pendingIntentId: data.meta?.pending_intent_id || null,
      });

      await refreshTodaySchedule();
      if (threadsAvailable) {
        await refreshThreadCatalog({ preserveActive: true });
      }
    } catch (error) {
      appendMessage("assistant", "I hit an error while sending that. Please try again.", String(error.message || error));
    } finally {
      setIsSending(false);
    }
  }

  async function handleSubmit(event) {
    if (event) event.preventDefault();
    const message = chatInput.trim();
    if (!message) return;
    appendMessage("user", message);
    setChatInput("");
    await sendChat({ message });
  }

  async function handleOptionSelect(index, option = null) {
    const value = String(index + 1);
    appendMessage("user", value);
    await sendChat({
      message: value,
      pendingResponse: {
        value,
        ...(option?.value ? { action: option.value } : {}),
      },
    });
  }

  async function handleDeleteTask(taskId) {
    const response = await fetch(`/tasks/${taskId}`, { method: "DELETE" });
    if (!response.ok) {
      appendMessage("assistant", "I couldn't delete that task.", `Task ${taskId}`);
      return;
    }
    appendMessage("assistant", `Deleted task ${taskId}.`);
    await Promise.all([refreshTodaySchedule(), refreshThreadCatalog(), refreshActiveThread()]);
  }

  async function handleCompleteTask(taskId) {
    const response = await fetch(`/tasks/${taskId}/complete`, { method: "PATCH" });
    if (!response.ok) {
      appendMessage("assistant", "I couldn't record completion for that task.", `Task ${taskId}`);
      return;
    }
    appendMessage("assistant", `Recorded completion for task ${taskId}.`);
    await Promise.all([refreshTodaySchedule(), refreshThreadCatalog(), refreshActiveThread()]);
  }

  async function handleDeleteThread(targetThreadId) {
    const response = await fetch(`/chat/threads/${encodeURIComponent(targetThreadId)}?thread_date=${threadDate}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      appendMessage("assistant", "I couldn't delete that chat thread.");
      return;
    }

    const remaining = threadsRef.current.filter((thread) => thread.id !== targetThreadId);
    setThreads(remaining);

    if (!remaining.length) {
      setThreadId(null);
      setMessages([]);
      setOptions([]);
      setOptionsAllowFreeText(true);
      setPendingIntentId(null);
      await createNewThread({ activate: true, replaceThreads: true });
      return;
    }

    if (threadId === targetThreadId) {
      await activateThread(remaining[0].id, remaining);
    }
  }

  async function refreshActiveThread() {
    if (!threadId) return;
    await activateThread(threadId);
  }

  function handleRescheduleTask(taskTitle) {
    setChatInput(`Reschedule ${taskTitle} to `);
    setView("chat");
    inputRef.current?.focus();
  }

  function handleQuickActionFill(template) {
    setView("chat");
    setChatInput(template);
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      const input = inputRef.current;
      if (input) {
        input.setSelectionRange(template.length, template.length);
      }
    });
  }

  function toggleSidebar() {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    localStorage.setItem("ai_scheduler_sidebar_collapsed", String(next));
    if (!next && !openSidebarSection) {
      setOpenSidebarSection("upcoming");
    }
  }

  function toggleSidebarSection(section) {
    if (sidebarCollapsed) {
      setSidebarCollapsed(false);
      localStorage.setItem("ai_scheduler_sidebar_collapsed", "false");
      setOpenSidebarSection(section);
    } else {
      setOpenSidebarSection((current) => (current === section ? null : section));
    }

    requestAnimationFrame(() => {
      const target = section === "upcoming" ? upcomingSectionRef.current : threadsSectionRef.current;
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const productivityMax = Math.max(...(insights.productivity_hours || []).map((item) => item.completed_tasks), 1);

  return (
    <>
      <AnimatedShaderBackground />
      <div className="workspace-shell">
        <header className="global-nav">
          <div className="global-nav-left">
            <button
              id="sidebar-toggle"
              className="sidebar-toggle nav-logo-button"
              type="button"
              aria-label={sidebarCollapsed ? "Expand menu" : "Collapse menu"}
              title={sidebarCollapsed ? "Expand menu" : "Collapse menu"}
              onClick={toggleSidebar}
            >
              <span className="sr-only">Toggle menu</span>
              <span className="nav-ai-mark" aria-hidden="true">AI</span>
            </button>
          </div>

          <div className="global-nav-actions">
            <nav className="workspace-nav-links" aria-label="Primary">
              <button type="button" className={`nav-link workspace-nav-link ${view === "chat" ? "active" : ""}`} onClick={() => setView("chat")}>Home</button>
            </nav>
            <div className="workspace-nav-date" id="workspace-date">{workspaceDate}</div>
          </div>
        </header>

        <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`} id="app-shell">
        <aside className="sidebar">
          <div className="sidebar-rail">
            <div className="sidebar-rail-top">
              <button
                type="button"
                className={`sidebar-rail-button ${view === "insights" ? "active" : ""}`}
                onClick={() => setView("insights")}
                aria-label="Insights"
                title="Insights"
              >
                <span className="sidebar-rail-button-main">
                  <IconInsights />
                  <span className="sidebar-rail-label">Insights</span>
                </span>
              </button>
              <button
                type="button"
                className={`sidebar-rail-button ${view === "today" ? "active" : ""}`}
                onClick={() => setView("today")}
                aria-label="Today's Tasks"
                title="Today's Tasks"
              >
                <span className="sidebar-rail-button-main">
                  <IconToday />
                  <span className="sidebar-rail-label">Today's Tasks</span>
                </span>
              </button>
              <button
                type="button"
                className="sidebar-rail-button active-accent"
                onClick={() => createNewThread({ activate: true })}
                aria-label="New Chat"
                title="New Chat"
              >
                <span className="sidebar-rail-button-main">
                  <IconNewChat />
                  <span className="sidebar-rail-label">New Chat</span>
                </span>
              </button>

              <div className={`sidebar-rail-group ${openSidebarSection === "upcoming" ? "open" : ""}`} ref={upcomingSectionRef}>
                <button
                  type="button"
                  className={`sidebar-rail-button sidebar-rail-toggle ${openSidebarSection === "upcoming" ? "active" : ""}`}
                  onClick={() => toggleSidebarSection("upcoming")}
                  aria-label="Upcoming Tasks"
                  title="Upcoming Tasks"
                >
                  <span className="sidebar-rail-button-main">
                    <IconUpcoming />
                    <span className="sidebar-rail-label">Upcoming Tasks</span>
                  </span>
                  <span className="sidebar-rail-chevron" aria-hidden="true">
                    <IconChevron />
                  </span>
                </button>
                {!sidebarCollapsed && openSidebarSection === "upcoming" ? (
                  <div className="sidebar-submenu">
                    {!upcomingTasks.length ? (
                      <article className="mini-card sidebar-inline-card">
                        <p className="eyebrow">Status</p>
                        <strong>{scheduleToday.tasks?.length ? "Nothing else today" : "No upcoming tasks"}</strong>
                      </article>
                    ) : upcomingTasks.map((task) => (
                      <article className="mini-card sidebar-inline-card" key={`upcoming-${task.id}`}>
                        <p className="eyebrow">{formatTaskWindow(task)}</p>
                        <strong>{task.title}</strong>
                        <p className="muted">{task.description || "No description"}</p>
                      </article>
                    ))}
                  </div>
                ) : null}
              </div>

              <div className={`sidebar-rail-group ${openSidebarSection === "threads" ? "open" : ""}`} ref={threadsSectionRef}>
                <button
                  type="button"
                  className={`sidebar-rail-button sidebar-rail-toggle ${openSidebarSection === "threads" ? "active" : ""}`}
                  onClick={() => toggleSidebarSection("threads")}
                  aria-label="Today's Chats"
                  title="Today's Chats"
                >
                  <span className="sidebar-rail-button-main">
                    <IconThreads />
                    <span className="sidebar-rail-label">Today's Chats</span>
                  </span>
                  <span className="sidebar-rail-chevron" aria-hidden="true">
                    <IconChevron />
                  </span>
                </button>
                {!sidebarCollapsed && openSidebarSection === "threads" ? (
                  <div className="sidebar-submenu thread-list">
                    {!threadsAvailable ? (
                      <p className="muted sidebar-inline-empty">Thread history is unavailable in the current backend session.</p>
                    ) : !threads.length ? (
                      <p className="muted sidebar-inline-empty">No chats yet for {formatShortDate(threadDate)}.</p>
                    ) : threads.map((thread) => (
                      <button
                        key={thread.id}
                        className={`thread-item ${thread.id === threadId ? "active" : ""}`}
                        type="button"
                        onClick={() => activateThread(thread.id)}
                      >
                        <span className="thread-item-top">
                          <span className="thread-item-title">{thread.title}</span>
                          <span
                            className="thread-delete-button"
                            role="button"
                            tabIndex={0}
                            aria-label="Delete chat"
                            title="Delete chat"
                            onClick={(event) => {
                              event.stopPropagation();
                              handleDeleteThread(thread.id);
                            }}
                            onKeyDown={(event) => {
                              if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                event.stopPropagation();
                                handleDeleteThread(thread.id);
                              }
                            }}
                          >
                            <svg viewBox="0 0 24 24" aria-hidden="true" className="thread-delete-icon">
                              <path d="M9 4h6" />
                              <path d="M5 7h14" />
                              <path d="M8 7v11a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2V7" />
                              <path d="M10 11v5" />
                              <path d="M14 11v5" />
                            </svg>
                          </span>
                        </span>
                        <span>{thread.preview || "No messages yet"}</span>
                        <small>{formatThreadTimestamp(thread.updatedAt)}</small>
                        {thread.pendingIntentId ? <span className="thread-status">Awaiting input</span> : null}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            <div className="sidebar-rail-profile" aria-hidden="true">
              <span>J</span>
            </div>
          </div>
        </aside>

        <main className="main-panel">
          <section className={`view ${view === "chat" ? "active" : ""}`} id="view-chat">
            <div className="chat-scene">
              <section className={`chat-stage ${hasConversation ? "chat-active" : ""}`}>
                {!hasConversation ? (
                  <div id="chat-empty-state" className="chat-empty-state">
                    <p className="eyebrow">Today's Planner</p>
                    <h2>What should we organize today?</h2>
                    <p className="muted">Ask to schedule, move, delete, or review your day, and I&apos;ll guide the next step with numbered options when needed.</p>
                  </div>
                ) : null}

                <div id="chat-messages" className="chat-messages" ref={chatMessagesRef}>
                  {messages.map((message, index) => (
                    <MessageBubble
                      key={`${message.role}-${index}`}
                      message={message}
                      isLatest={index === messages.length - 1}
                      hasPendingOptions={index === messages.length - 1 && options.length > 0}
                      pendingOptions={index === messages.length - 1 ? options : []}
                      allowFreeText={index === messages.length - 1 ? optionsAllowFreeText : false}
                      isSending={isSending}
                      onSelectOption={handleOptionSelect}
                      onCompleteTask={handleCompleteTask}
                      onDeleteTask={handleDeleteTask}
                      onRescheduleTask={handleRescheduleTask}
                    />
                  ))}
                </div>

                {options.length ? (
                  <div id="chat-options" className="chat-options">
                    {options.map((option, index) => (
                      <button
                        key={`${option.label}-${index}`}
                        className="option-button"
                        type="button"
                        disabled={isSending}
                        onClick={() => handleOptionSelect(index, option)}
                      >
                        {index + 1}. {option.label}
                      </button>
                    ))}
                  </div>
                ) : null}

                <form id="chat-form" className="chat-form chat-form-shell" onSubmit={handleSubmit}>
                  <label className="chat-input-shell" htmlFor="chat-input">
                    <span className="chat-input-label">Planner Input</span>
                    <input
                      id="chat-input"
                      ref={inputRef}
                      type="text"
                      placeholder="Ask anything about your schedule..."
                      autoComplete="off"
                      disabled={isSending}
                      value={chatInput}
                      onChange={(event) => setChatInput(event.target.value)}
                    />
                  </label>
                  <button type="button" className="send-button" disabled={isSending} onClick={handleSubmit}>
                    {isSending ? "Sending..." : "Send"}
                  </button>
                </form>

                {!hasConversation ? (
                  <div className="quick-actions" id="quick-actions">
                    {QUICK_ACTIONS.map((action) => (
                      <button key={action.id} className="quick-action" type="button" onClick={() => handleQuickActionFill(action.template)}>
                        {action.label}
                      </button>
                    ))}
                  </div>
                ) : null}
              </section>
            </div>
          </section>

          <section className={`view ${view === "insights" ? "active" : ""}`} id="view-insights">
            <div className="hero">
              <div>
                <p className="eyebrow">Behavior Dashboard</p>
                <h2>Mood, energy, and execution patterns at a glance.</h2>
                <p className="muted">Track the last 7 days, the last month, your strongest productivity windows, and how well tasks are actually getting completed.</p>
              </div>
            </div>

            <div className="stats-grid">
              <article className="panel stat-card">
                <p className="eyebrow">Best Productivity Time</p>
                <h3>{insights.best_productivity_window}</h3>
                <p className="muted">Based on completed task start times in the last 30 days.</p>
              </article>
              <article className="panel stat-card">
                <p className="eyebrow">7 Day Completion</p>
                <h3>{formatPercent(insights.efficiency.completion_rate_7d)}</h3>
                <p className="muted">Share of tasks completed in the last 7 days.</p>
              </article>
              <article className="panel stat-card">
                <p className="eyebrow">30 Day Completion</p>
                <h3>{formatPercent(insights.efficiency.completion_rate_30d)}</h3>
                <p className="muted">Longer-term completion efficiency trend.</p>
              </article>
              <article className="panel stat-card">
                <p className="eyebrow">Duration Accuracy</p>
                <h3>{formatPercent(insights.efficiency.duration_accuracy)}</h3>
                <p className="muted">How closely actual task duration matches planned time.</p>
              </article>
            </div>

            <div className="dashboard-grid">
              <section className="panel">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">Last 7 Days</p>
                    <h3>Mood and Energy</h3>
                  </div>
                </div>
                <div className="trend-grid">
                  {insights.last_7_days.map((point) => (
                    <article className="trend-card" key={point.date}>
                      <p className="eyebrow">{formatShortDate(point.date)}</p>
                      <strong>Mood {point.mood ?? "-"}</strong><br />
                      <strong>Energy {point.energy ?? "-"}</strong>
                      <div className="trend-ring"><span style={{ width: `${Math.max(6, (point.completion_rate || 0) * 100)}%` }} /></div>
                      <small>{point.completed_tasks}/{point.total_tasks} done</small>
                    </article>
                  ))}
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">Last 30 Days</p>
                    <h3>Completion Trend</h3>
                  </div>
                </div>
                <div className="bar-grid">
                  {insights.last_30_days.map((point) => (
                    <article className="bar-card" key={point.date}>
                      <p className="eyebrow">{formatMiniDate(point.date)}</p>
                      <div className="bar"><span style={{ height: `${Math.max(6, (point.completion_rate || 0) * 100)}%` }} /></div>
                      <small>{formatPercent(point.completion_rate)}</small>
                    </article>
                  ))}
                </div>
              </section>

              <section className="panel wide-panel">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">Productivity Rhythm</p>
                    <h3>Completed Tasks by Hour</h3>
                  </div>
                </div>
                <div className="hour-grid">
                  {insights.productivity_hours.map((item) => (
                    <article
                      key={item.label}
                      className={`hour-card ${String(insights.best_productivity_window || "").startsWith(item.label) ? "active" : ""}`}
                    >
                      <p className="eyebrow">{item.label}</p>
                      <strong>{item.completed_tasks}</strong>
                      <div className="trend-ring"><span style={{ width: `${(item.completed_tasks / productivityMax) * 100}%` }} /></div>
                    </article>
                  ))}
                </div>
              </section>
            </div>
          </section>

          <section className={`view ${view === "today" ? "active" : ""}`} id="view-today">
            <div className="hero">
              <div>
                <p className="eyebrow">Daily Operations</p>
                <h2>Your day, arranged as an active timeline.</h2>
                <p className="muted">See what is scheduled, what is pending, and which tasks still need a slot.</p>
              </div>
            </div>

            <div className="today-layout">
              <section className="panel">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">Today</p>
                    <h3>{formatLongDate(scheduleToday.date)} Schedule</h3>
                  </div>
                </div>
                <div className="timeline">
                  {scheduleToday.tasks.length ? scheduleToday.tasks.map((task) => (
                    <article className="timeline-item" key={task.id}>
                      <div className="timeline-time">{task.start_time ? task.start_time.slice(0, 5) : "--:--"}</div>
                      <div>
                        <strong>{task.title}</strong>
                        <p className="muted">{task.description || "No description"}</p>
                        <small>
                          <span className={`status-dot ${task.completed ? "status-complete" : task.start_time ? "status-pending" : "status-unscheduled"}`} />
                          {task.completed ? "Completed" : task.start_time ? "Scheduled" : "Needs time"}
                          {task.end_time ? ` until ${task.end_time.slice(0, 5)}` : ""}
                        </small>
                      </div>
                    </article>
                  )) : (
                    <article className="timeline-item">
                      <div className="timeline-time">Open</div>
                      <div>
                        <strong>No tasks scheduled yet.</strong>
                        <p className="muted">Use the chat to create your first block for today.</p>
                      </div>
                    </article>
                  )}
                </div>
              </section>

              <section className="panel">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">Daily Context</p>
                    <h3>Energy and Focus</h3>
                  </div>
                </div>
                <div className="mini-stack">
                  <article className="mini-card"><p className="eyebrow">Energy</p><strong>{scheduleToday.energy}</strong></article>
                  <article className="mini-card"><p className="eyebrow">Message</p><strong>{scheduleToday.message}</strong></article>
                  <article className="mini-card"><p className="eyebrow">Tasks</p><strong>{scheduleToday.tasks.length}</strong></article>
                </div>
              </section>
            </div>
          </section>
        </main>
        </div>
      </div>
    </>
  );
}

function MessageBubble({
  message,
  isLatest,
  hasPendingOptions,
  pendingOptions,
  allowFreeText,
  isSending,
  onSelectOption,
  onCompleteTask,
  onDeleteTask,
  onRescheduleTask,
}) {
  const sectionConfigs = useMemo(() => {
    if (!message.payload) return [];
    return [
      { title: message.payload.mode === "schedule" ? "Today's Schedule" : "", tasks: message.payload.unchanged_tasks || [] },
      { title: "Created", tasks: message.payload.created_tasks || [] },
      { title: "Updated", tasks: message.payload.updated_tasks || [] },
      { title: "Needs Time", tasks: message.payload.unscheduled_tasks || [] },
    ].filter((section) => section.tasks.length);
  }, [message.payload]);

  const payloadPendingOptions = useMemo(
    () => getPendingOptionsFromResponse(message.payload),
    [message.payload],
  );
  const looksLikeOptionPrompt = isOptionPromptText(message.text);
  const payloadMessageText = message.payload?.message || "";
  const payloadLooksLikeOptionPrompt =
    isOptionPromptText(payloadMessageText) || payloadPendingOptions.length > 0;

  return (
    <div className={`message ${message.role}`}>
      {message.role !== "assistant" ? (
        <div className="message-copy">{message.text}</div>
      ) : message.payload ? (
        <>
          {payloadLooksLikeOptionPrompt && !sectionConfigs.length ? (
            <PendingPromptCard
              text={payloadMessageText || message.text}
              options={isLatest ? (pendingOptions.length ? pendingOptions : payloadPendingOptions) : payloadPendingOptions}
              allowFreeText={isLatest ? allowFreeText : getPendingAllowFreeText(message.payload)}
              isSending={isSending}
              onSelectOption={onSelectOption}
            />
          ) : (
            <>
              {message.payload.message ? <div className="message-copy">{message.payload.message}</div> : null}
              {sectionConfigs.length ? sectionConfigs.map((section) => (
                <div className="message-task-group" key={`${section.title}-${section.tasks.length}`}>
                  {section.title ? <p className="message-section-title">{section.title}</p> : null}
                  <div className="message-task-grid">
                    {section.tasks.map((task) => (
                      <article className="message-task-card interactive" key={task.id}>
                        <div className="message-task-main">
                          <strong>{task.title}</strong>
                          <span>{formatShortDate(task.date)} · {task.completed ? "Completed" : task.start_time ? "Scheduled" : "Needs time"}</span>
                        </div>
                        <div className="message-task-time">{formatTaskTime(task)}</div>
                        <div className="message-card-actions">
                          <button className="message-action-button" type="button" disabled={task.completed} onClick={() => onCompleteTask(task.id)}>Completed</button>
                          <button className="message-action-button" type="button" onClick={() => onRescheduleTask(task.title)}>Reschedule</button>
                          <button className="message-action-button danger" type="button" onClick={() => onDeleteTask(task.id)}>Delete</button>
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              )) : null}
            </>
          )}
        </>
      ) : (hasPendingOptions && isLatest) || looksLikeOptionPrompt ? (
        <PendingPromptCard
          text={message.text}
          options={pendingOptions}
          allowFreeText={allowFreeText}
          isSending={isSending}
          onSelectOption={onSelectOption}
        />
      ) : (
        <div className="message-copy">{formatAssistantPlainText(message.text, false)}</div>
      )}
      {message.meta ? <small>{message.meta}</small> : null}
    </div>
  );
}

function PendingPromptCard({ text, options, allowFreeText, isSending, onSelectOption }) {
  const parsed = parsePendingPrompt(text);
  const fallbackOptions = options?.length ? options : parseOptionsFromText(text);

  return (
    <div className="pending-card">
      <p className="message-section-title">{parsed.eyebrow}</p>
      <div className="pending-card-copy">
        <strong>{parsed.title}</strong>
        {parsed.body ? <p>{parsed.body}</p> : null}
      </div>
      {parsed.highlights.length ? (
        <div className="pending-highlight-row">
          {parsed.highlights.map((item, index) => (
            <span key={`${item}-${index}`} className="pending-highlight-pill">{item}</span>
          ))}
        </div>
      ) : null}
      {fallbackOptions.length ? (
        <div className="pending-option-list">
          {fallbackOptions.map((option, index) => (
            <button
              key={`${option.label}-${index}`}
              type="button"
              className="pending-option-button"
              disabled={isSending}
              onClick={() => onSelectOption(index, option)}
            >
              <span className="pending-option-index">{index + 1}</span>
              <span className="pending-option-label">{option.label}</span>
            </button>
          ))}
        </div>
      ) : null}
      <p className="pending-card-footer">
        {allowFreeText ? "Choose an option or type a custom answer below." : "Choose one of the options below."}
      </p>
    </div>
  );
}

function formatAssistantPlainText(text, hasPendingOptions) {
  const value = String(text || "").trim();
  if (!hasPendingOptions) return value;

  const withoutOptions = value
    .replace(/\s*Options:\s*[\s\S]*$/i, "")
    .replace(/\s*Reply with the option number\.?\s*$/i, "")
    .trim();

  if (!withoutOptions) return "Choose one of the options below.";

  if (/choose an option\.?$/i.test(withoutOptions)) {
    return withoutOptions.replace(/choose an option\.?$/i, "Choose one of the options below.");
  }

  return `${withoutOptions}\n\nChoose one of the options below.`;
}

function parsePendingPrompt(text) {
  const raw = String(text || "").trim();
  const cleaned = raw
    .replace(/\s*Options:\s*[\s\S]*$/i, "")
    .replace(/\s*Reply with the option number\.?\s*$/i, "")
    .trim();

  const conflictMatch = cleaned.match(
    /I infer you want to move (.+?) to (.+?), but it conflicts with (.+?)\.(.*)/i,
  );

  if (conflictMatch) {
    const [, taskTitle, requestedTime, conflictingTask, remainder] = conflictMatch;
    return {
      eyebrow: "Scheduling Conflict",
      title: taskTitle.trim(),
      body: `Requested for ${requestedTime.trim()}. It conflicts with ${conflictingTask.trim()}.${remainder ? ` ${remainder.trim()}` : ""}`.trim(),
      highlights: [requestedTime.trim(), `Conflicts with ${conflictingTask.trim()}`],
    };
  }

  return {
    eyebrow: "Next Step",
    title: cleaned.split(". ")[0] || "Choose an option",
    body: cleaned.includes(". ") ? cleaned.split(". ").slice(1).join(". ").trim() : "",
    highlights: [],
  };
}

function isOptionPromptText(text) {
  const value = String(text || "");
  return /Options:\s*\d+\./i.test(value) || /Reply with the option number/i.test(value);
}

function parseOptionsFromText(text) {
  const value = String(text || "");
  const optionsMatch = value.match(/Options:\s*([\s\S]*?)Reply with the option number\.?/i);
  const source = optionsMatch ? optionsMatch[1] : value;
  const matches = [...source.matchAll(/(?:^|\s)(\d+)\.\s+(.+?)(?=(?:\s+\d+\.\s)|$)/g)];

  return matches.map((match) => ({
    id: match[1],
    label: match[2].trim(),
    value: match[1],
  }));
}

function getPendingOptionsFromResponse(response) {
  if (!response) return [];

  if (response.clarification?.options?.length) {
    return response.clarification.options.map((option) => ({
      id: option.id,
      label: option.label,
      value: option.value,
    }));
  }

  const slots = response.conflict_info?.suggested_slots || [];
  const actions = response.conflict_info?.actions || [];
  if (slots.length) {
    const slotOptions = slots.map((slot) => ({
      id: slot.slot_id,
      label: formatSlotOption(slot),
      value: `choose_slot:${slot.slot_id}`,
    }));
    if (actions.includes("cancel")) {
      slotOptions.push({
        id: "cancel",
        label: "Cancel",
        value: "cancel",
      });
    }
    if (actions.includes("keep_original_and_move_conflicts")) {
      slotOptions.push({
        id: "keep_original_and_move_conflicts",
        label: "Keep original time and move conflicts",
        value: "keep_original_and_move_conflicts",
      });
    }
    return slotOptions;
  }

  return [];
}

function getPendingAllowFreeText(response) {
  if (!response) return true;
  if (response.clarification) {
    return response.clarification.allow_free_text ?? true;
  }
  return false;
}

function formatSlotOption(slot) {
  const dateLabel = slot.date ? formatShortDate(slot.date) : "";
  const start = slot.start_time ? slot.start_time.slice(0, 5) : "";
  const end = slot.end_time ? slot.end_time.slice(0, 5) : "";
  return [dateLabel, `${start}-${end}`.replace(/^-|-$/g, "")]
    .filter(Boolean)
    .join(" ");
}

function IconInsights() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon">
      <path d="M5 18h14" />
      <path d="M7 15V9" />
      <path d="M12 15V6" />
      <path d="M17 15v-3" />
    </svg>
  );
}

function IconToday() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon">
      <rect x="4" y="5" width="16" height="15" rx="3" />
      <path d="M8 3v4M16 3v4M4 10h16" />
    </svg>
  );
}

function IconNewChat() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function IconUpcoming() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon">
      <path d="M12 4v8l4 2" />
      <circle cx="12" cy="12" r="8" />
    </svg>
  );
}

function IconThreads() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon">
      <path d="M7 8h10" />
      <path d="M7 12h7" />
      <path d="M7 16h5" />
      <rect x="4" y="5" width="16" height="14" rx="4" />
    </svg>
  );
}

function IconChevron() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="rail-icon sidebar-chevron-icon">
      <path d="M8 10l4 4 4-4" />
    </svg>
  );
}

function summarizeResponse(data) {
  const parts = [];
  if (data.created_tasks?.length) parts.push(`${data.created_tasks.length} created`);
  if (data.updated_tasks?.length) parts.push(`${data.updated_tasks.length} updated`);
  if (data.unscheduled_tasks?.length) parts.push(`${data.unscheduled_tasks.length} unscheduled`);
  return parts.join(" | ");
}

function buildAssistantMessage(data) {
  const sections = [data.message];
  const scheduleLines = buildTaskLines(data);
  if (scheduleLines.length) sections.push(scheduleLines.join("\n"));
  return sections.filter(Boolean).join("\n\n");
}

function buildTaskLines(data) {
  const lines = [];
  const unchangedTasks = data.unchanged_tasks || [];
  const createdTasks = data.created_tasks || [];
  const updatedTasks = data.updated_tasks || [];

  if (data.mode === "schedule" && unchangedTasks.length) {
    return unchangedTasks.map((task) => formatTaskLine(task));
  }

  if (createdTasks.length) {
    lines.push("Created:");
    lines.push(...createdTasks.map((task) => formatTaskLine(task)));
  }

  if (updatedTasks.length) {
    if (lines.length) lines.push("");
    lines.push("Updated:");
    lines.push(...updatedTasks.map((task) => formatTaskLine(task)));
  }

  return lines;
}

function formatTaskLine(task) {
  const day = task.date ? formatShortDate(task.date) : "";
  const start = task.start_time ? task.start_time.slice(0, 5) : "No time";
  const end = task.end_time ? task.end_time.slice(0, 5) : "";
  const timeLabel = end ? `${start}-${end}` : start;
  return `• ${task.title} (${day} ${timeLabel})`;
}

function normalizeServerThread(thread) {
  return {
    id: thread.chat_thread_id,
    threadDate: thread.thread_date,
    title: thread.title || "New chat",
    preview: thread.preview || "No messages yet",
    pendingIntentId: thread.pending_intent_id || null,
    createdAt: thread.created_at,
    updatedAt: thread.updated_at,
  };
}

function deriveThreadTitle(message) {
  const trimmed = String(message || "").trim();
  if (!trimmed) return "New chat";
  return trimmed.length > 42 ? `${trimmed.slice(0, 42)}...` : trimmed;
}

function getStoredActiveThreadId(threadDate) {
  const active = readJson(ACTIVE_THREAD_STORAGE_KEY, {});
  return active[threadDate] || null;
}

function setStoredActiveThreadId(threadDate, activeThreadId) {
  const active = readJson(ACTIVE_THREAD_STORAGE_KEY, {});
  active[threadDate] = activeThreadId;
  writeJson(ACTIVE_THREAD_STORAGE_KEY, active);
}

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function getUpcomingTasks(tasks) {
  const now = new Date();
  const today = todayIso();
  const currentMinutes = now.getHours() * 60 + now.getMinutes();

  return [...tasks]
    .filter((task) => task.date === today)
    .filter((task) => !task.completed)
    .map((task) => ({
      ...task,
      sortMinutes: task.start_time ? timeStringToMinutes(task.start_time) : Number.MAX_SAFE_INTEGER,
    }))
    .filter((task) => {
      if (!task.start_time) return true;
      const endMinutes = task.end_time ? timeStringToMinutes(task.end_time) : task.sortMinutes;
      return endMinutes >= currentMinutes;
    })
    .sort((a, b) => a.sortMinutes - b.sortMinutes);
}

function timeStringToMinutes(value) {
  const [hours, minutes] = String(value).split(":").map(Number);
  return (hours * 60) + minutes;
}

function formatTaskWindow(task) {
  if (!task.start_time) return "Flexible";
  const start = task.start_time.slice(0, 5);
  const end = task.end_time ? task.end_time.slice(0, 5) : "";
  return end ? `${start}-${end}` : start;
}

function formatTaskTime(task) {
  const start = task.start_time ? task.start_time.slice(0, 5) : "No time";
  const end = task.end_time ? task.end_time.slice(0, 5) : "";
  return end ? `${start}-${end}` : start;
}

function formatPercent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function formatShortDate(isoDate) {
  if (!isoDate) return "No date";
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatMiniDate(isoDate) {
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
}

function formatLongDate(isoDate) {
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

function formatThreadTimestamp(value) {
  return new Date(value).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatWorkspaceDate(date) {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  }).format(date);
}

function todayIso() {
  const now = new Date();
  const offset = now.getTimezoneOffset();
  const local = new Date(now.getTime() - offset * 60000);
  return local.toISOString().slice(0, 10);
}

function isThreadsEndpointError(error) {
  return String(error?.message || "").includes("/chat/threads");
}
