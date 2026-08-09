import { ChangeEvent, FormEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  apiRequest,
  Attachment,
  Citation,
  Conversation,
  EvidenceCheck,
  Message,
  Memory,
  McpTool,
  EffectiveSkill,
  Skill,
  Workspace,
  WorkspaceDocument,
} from "./api";
import { RunEventStore, streamRunEvents, useRunEvents } from "./runEventStore";

type ConversationList = { items: Conversation[] };
type MessageList = { items: Message[] };
type CitationList = { items: Citation[] };
type DocumentList = { items: WorkspaceDocument[] };
type RunCreated = { run_id: string; assistant_message_id: string; status: string };
type RunReference = { run_id: string; status: string };
type ResearchTask = { ordinal: number; title: string; status: string; failure_impact: string | null };
type RunDetail = { run_id: string; status: string; tasks: ResearchTask[] };
type AgentTodo = {
  id: string;
  ordinal: number;
  title: string;
  purpose: string;
  kind: string;
  status: string;
  result_summary: string | null;
  failure_reason: string | null;
  sandbox_execution_id: string | null;
  sandbox_code: string | null;
  sandbox_input_attachment_ids: string[];
  sandbox_artifacts: SandboxArtifact[];
};
type AgentTodoList = { items: AgentTodo[] };
type ToolApproval = {
  id: string;
  run_id: string;
  tool_call_id: string;
  tool_name: string;
  risk_level: string;
  parameters_hash: string;
  safe_summary: string;
  status: string;
  expires_at: string;
};
type ToolApprovalList = { items: ToolApproval[] };
type MemoryList = { items: Memory[] };
type SkillList = { items: Skill[] };
type EffectiveSkillList = { items: EffectiveSkill[] };
type EffectiveMcpToolList = { workspace_enabled: boolean; items: McpTool[] };
type EvidenceSelection = {
  messageId: string;
  version: number;
  text: string;
  startChar: number;
  endChar: number;
};
type SandboxArtifact = {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
};

export function WorkspaceView({
  workspace,
  token,
  onWorkspaceUpdated,
}: {
  workspace: Workspace;
  token: string;
  onWorkspaceUpdated: (workspace: Workspace) => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [showConversationForm, setShowConversationForm] = useState(false);
  const [conversationTitle, setConversationTitle] = useState("新会话");
  const [composer, setComposer] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [eventStore, setEventStore] = useState<RunEventStore | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [latestRunId, setLatestRunId] = useState<string | null>(null);
  const [persistedTasks, setPersistedTasks] = useState<ResearchTask[]>([]);
  const [toolApprovals, setToolApprovals] = useState<ToolApproval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [citation, setCitation] = useState<Citation | null>(null);
  const [evidenceSelection, setEvidenceSelection] = useState<EvidenceSelection | null>(null);
  const [evidenceCheck, setEvidenceCheck] = useState<EvidenceCheck | null>(null);
  const [documents, setDocuments] = useState<WorkspaceDocument[]>([]);
  const [showDocuments, setShowDocuments] = useState(false);
  const [showMemories, setShowMemories] = useState(false);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryDraft, setMemoryDraft] = useState("");
  const [showSkills, setShowSkills] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [effectiveSkills, setEffectiveSkills] = useState<EffectiveSkill[]>([]);
  const [mcpWorkspaceEnabled, setMcpWorkspaceEnabled] = useState(false);
  const [mcpTools, setMcpTools] = useState<McpTool[]>([]);
  const [showSandbox, setShowSandbox] = useState(false);
  const [sandboxTodos, setSandboxTodos] = useState<AgentTodo[]>([]);
  const streamAbort = useRef<AbortController | null>(null);
  const events = useRunEvents(eventStore);

  useEffect(() => {
    // 切换 Workspace 时清除所有空间级和会话级临时展示状态
    streamAbort.current?.abort();
    setConversation(null);
    setMessages([]);
    setToolApprovals([]);
    setAttachments([]);
    setFiles([]);
    setEventStore(null);
    setActiveRunId(null);
    setLatestRunId(null);
    setPersistedTasks([]);
    setShowConversationForm(false);
    setShowDocuments(false);
    setDocuments([]);
    setShowMemories(false);
    setMemories([]);
    setShowSkills(false);
    setSkills([]);
    setEffectiveSkills([]);
    setMcpWorkspaceEnabled(false);
    setMcpTools([]);
    setShowSandbox(false);
    setSandboxTodos([]);
    setCitation(null);
    setEvidenceCheck(null);
    void apiRequest<ConversationList>(
      `/api/v1/workspaces/${workspace.id}/conversations`,
      {},
      token,
    ).then((result) => setConversations(result.items)).catch(reportError);
  }, [workspace.id, token]);

  useEffect(() => {
    if (!conversation) return;
    // 切换会话时清除仅属于上一会话的临时展示状态
    streamAbort.current?.abort();
    setAttachments([]);
    setFiles([]);
    setToolApprovals([]);
    setEventStore(null);
    setActiveRunId(null);
    void loadConversationState(conversation.id);
  }, [conversation?.id]);

  useEffect(() => () => streamAbort.current?.abort(), []);

  const streamedAnswer = useMemo(
    () => events
      .filter((event) => event.event === "assistant_delta")
      .map((event) => String(event.data.content ?? ""))
      .join(""),
    [events],
  );
  const eventPlan = events.find((event) => event.event === "plan_created")?.data.tasks;
  const plan = Array.isArray(eventPlan) ? eventPlan : persistedTasks;
  const liveStatus = events.some((event) => event.event === "run_started")
    ? "正在规划研究…"
    : "正在排队等待研究 Worker…";

  function reportError(reason: unknown) {
    setError(reason instanceof Error ? reason.message : "操作失败");
  }

  async function loadMessages(conversationId: string) {
    try {
      const result = await apiRequest<MessageList>(
        `/api/v1/conversations/${conversationId}/messages`,
        {},
        token,
      );
      setMessages(result.items);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function loadConversationState(conversationId: string) {
    await loadMessages(conversationId);
    try {
      const latest = await apiRequest<RunDetail | null>(
        `/api/v1/conversations/${conversationId}/latest-run`,
        {},
        token,
      );
      setPersistedTasks(latest?.tasks ?? []);
      setLatestRunId(latest?.run_id ?? null);
      if (latest?.run_id) await loadSandboxTodos(latest.run_id);
      const active = await apiRequest<RunCreated | null>(
        `/api/v1/conversations/${conversationId}/active-run`,
        {},
        token,
      );
      if (active) await consumeRun(active, conversationId);
    } catch (reason) {
      reportError(reason);
    }
  }

  // 读取 Agent 自动创建的 Todo 与 Sandbox 状态
  async function loadSandboxTodos(runId: string) {
    try {
      const result = await apiRequest<AgentTodoList>(`/api/v1/runs/${runId}/todos`, {}, token);
      setSandboxTodos(result.items.filter((item) => item.kind === "python_sandbox"));
    } catch {
      setSandboxTodos([]);
    }
  }

  // 读取运行中当前用户可处理的工具审批
  async function loadToolApprovals(runId: string) {
    const result = await apiRequest<ToolApprovalList>(
      `/api/v1/runs/${runId}/tool-approvals`,
      {},
      token,
    );
    setToolApprovals(result.items);
  }

  // 消费同一运行的事件流并在等待审批时保留活动态
  async function consumeRun(run: RunReference, conversationId: string) {
    const store = new RunEventStore();
    setEventStore(store);
    setActiveRunId(run.run_id);
    const controller = new AbortController();
    streamAbort.current?.abort();
    streamAbort.current = controller;
    const outcome = await streamRunEvents(run.run_id, token, store, controller.signal);
    if (outcome === "waiting_approval") {
      await loadToolApprovals(run.run_id);
      return;
    }
    await loadMessages(conversationId);
    const latest = await apiRequest<RunDetail | null>(
      `/api/v1/conversations/${conversationId}/latest-run`,
      {},
      token,
    );
    setPersistedTasks(latest?.tasks ?? []);
    setLatestRunId(latest?.run_id ?? run.run_id);
    await loadSandboxTodos(latest?.run_id ?? run.run_id);
    setToolApprovals([]);
    setActiveRunId(null);
  }

  // 提交一次性审批决定并恢复原研究运行
  async function decideToolApproval(approval: ToolApproval, decision: "approve" | "reject") {
    if (!conversation) return;
    setBusy(true);
    setError(null);
    try {
      await apiRequest(
        `/api/v1/tool-approvals/${approval.id}/${decision}`,
        { method: "POST" },
        token,
      );
      setToolApprovals((current) => current.map((item) => (
        item.id === approval.id
          ? { ...item, status: decision === "approve" ? "approved" : "rejected" }
          : item
      )));
      await consumeRun({ run_id: approval.run_id, status: "queued" }, conversation.id);
    } catch (reason) {
      reportError(reason);
      await loadToolApprovals(approval.run_id).catch(reportError);
    } finally {
      setBusy(false);
    }
  }

  async function createConversation(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await apiRequest<Conversation>(
        `/api/v1/workspaces/${workspace.id}/conversations`,
        { method: "POST", body: JSON.stringify({ title: conversationTitle }) },
        token,
      );
      setConversations((current) => [...current, created]);
      setConversation(created);
      setConversationTitle("新会话");
      setShowConversationForm(false);
    } catch (reason) {
      reportError(reason);
    } finally {
      setBusy(false);
    }
  }

  function selectFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []).slice(0, 20);
    setFiles(selected);
  }

  async function waitForAttachment(id: string): Promise<Attachment> {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const current = await apiRequest<Attachment>(`/api/v1/attachments/${id}`, {}, token);
      if (current.status !== "processing") return current;
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("附件仍在处理中，请稍后重试");
  }

  async function uploadSelectedFiles(): Promise<Attachment[]> {
    if (!conversation) return [];
    const uploaded: Attachment[] = [];
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      const accepted = await apiRequest<Attachment>(
        `/api/v1/conversations/${conversation.id}/attachments`,
        { method: "POST", body: form },
        token,
      );
      setAttachments((current) => [...current, accepted]);
      const ready = await waitForAttachment(accepted.id);
      setAttachments((current) => current.map((item) => item.id === ready.id ? ready : item));
      if (ready.status === "failed") throw new Error(ready.failure_reason ?? "附件解析失败");
      uploaded.push(ready);
    }
    return uploaded;
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    if (!conversation || !composer.trim()) return;
    const question = composer.trim();
    setBusy(true);
    setError(null);
    try {
      const uploaded = await uploadSelectedFiles();
      const run = await apiRequest<RunCreated>(
        `/api/v1/conversations/${conversation.id}/messages`,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            content: question,
            attachment_ids: uploaded.map((item) => item.id),
          }),
        },
        token,
      );
      setMessages((current) => [
        ...current,
        { id: `user-${run.run_id}`, role: "user", content: question, version: 1 },
        { id: run.assistant_message_id, role: "assistant", content: "", version: 1 },
      ]);
      setComposer("");
      setFiles([]);
      await consumeRun(run, conversation.id);
    } catch (reason) {
      reportError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function stopRun() {
    if (!activeRunId) return;
    try {
      await apiRequest(`/api/v1/runs/${activeRunId}/cancel`, { method: "POST" }, token);
      if (toolApprovals.some((approval) => approval.status === "pending") && conversation) {
        await loadMessages(conversation.id);
        await loadToolApprovals(activeRunId);
        setActiveRunId(null);
      }
    } catch (reason) {
      reportError(reason);
    }
  }

  async function showCitation(messageId: string) {
    try {
      const result = await apiRequest<CitationList>(
        `/api/v1/messages/${messageId}/citations`,
        {},
        token,
      );
      setCitation(result.items[0] ?? null);
    } catch (reason) {
      reportError(reason);
    }
  }

  function captureEvidenceSelection(
    message: Message,
    event: MouseEvent<HTMLParagraphElement>,
  ) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || !selection.toString().trim()) return;
    const range = selection.getRangeAt(0);
    const target = event.currentTarget;
    if (!target.contains(range.commonAncestorContainer)) return;
    const before = range.cloneRange();
    before.selectNodeContents(target);
    before.setEnd(range.startContainer, range.startOffset);
    const startChar = before.toString().length;
    const text = selection.toString();
    setEvidenceSelection({
      messageId: message.id,
      version: message.version,
      text,
      startChar,
      endChar: startChar + text.length,
    });
    setEvidenceCheck(null);
  }

  async function createEvidenceCheck() {
    if (!evidenceSelection) return;
    try {
      const checked = await apiRequest<EvidenceCheck>(
        `/api/v1/messages/${evidenceSelection.messageId}/evidence-checks`,
        {
          method: "POST",
          body: JSON.stringify({
            message_version: evidenceSelection.version,
            start_char: evidenceSelection.startChar,
            end_char: evidenceSelection.endChar,
            text: evidenceSelection.text,
          }),
        },
        token,
      );
      setEvidenceCheck(checked);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function promoteAttachment(attachmentId: string) {
    try {
      await apiRequest(`/api/v1/attachments/${attachmentId}/promote`, { method: "POST" }, token);
      setAttachments((current) => current.map((item) => (
        item.id === attachmentId ? { ...item, filename: `${item.filename} · 已加入空间` } : item
      )));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function loadDocuments() {
    try {
      const result = await apiRequest<DocumentList>(
        `/api/v1/workspaces/${workspace.id}/documents`,
        {},
        token,
      );
      setDocuments(result.items);
      setShowDocuments(true);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function loadMemories() {
    try {
      const result = await apiRequest<MemoryList>(
        `/api/v1/workspaces/${workspace.id}/memories`,
        {},
        token,
      );
      setMemories(result.items);
      setShowMemories(true);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function createMemory(event: FormEvent) {
    event.preventDefault();
    if (!memoryDraft.trim()) return;
    try {
      const created = await apiRequest<Memory>(
        `/api/v1/workspaces/${workspace.id}/memories`,
        {
          method: "POST",
          body: JSON.stringify({
            content: memoryDraft.trim(),
            scope: "workspace",
            category: "constraint",
            risk_level: "low",
          }),
        },
        token,
      );
      setMemories((current) => [created, ...current]);
      setMemoryDraft("");
    } catch (reason) {
      reportError(reason);
    }
  }

  async function updateMemoryAutoApply(enabled: boolean) {
    try {
      const updated = await apiRequest<Workspace>(
        `/api/v1/workspaces/${workspace.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            name: workspace.name,
            description: workspace.description,
            instructions: workspace.instructions,
            memory_auto_apply: enabled,
          }),
        },
        token,
      );
      onWorkspaceUpdated(updated);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function loadSkills() {
    if (!conversation) return;
    try {
      const [catalog, effective, mcp] = await Promise.all([
        apiRequest<SkillList>("/api/v1/skills/catalog", {}, token),
        apiRequest<EffectiveSkillList>(`/api/v1/conversations/${conversation.id}/skills`, {}, token),
        apiRequest<EffectiveMcpToolList>(
          `/api/v1/conversations/${conversation.id}/mcp/local-trusted/tools`,
          {},
          token,
        ),
      ]);
      setSkills(catalog.items);
      setEffectiveSkills(effective.items);
      setMcpWorkspaceEnabled(mcp.workspace_enabled);
      setMcpTools(mcp.items);
      setShowSkills(true);
    } catch (reason) {
      reportError(reason);
    }
  }

  // 切换当前 Workspace 的本地受信 MCP grant
  async function setWorkspaceMcpEnabled(enabled: boolean) {
    try {
      await apiRequest(
        `/api/v1/workspaces/${workspace.id}/mcp/local-trusted/${enabled ? "enable" : "disable"}`,
        { method: "POST" },
        token,
      );
      await loadSkills();
    } catch (reason) {
      reportError(reason);
    }
  }

  async function installSkill(skill: Skill) {
    try {
      await apiRequest(`/api/v1/skills/${skill.slug}/install`, { method: "POST" }, token);
      await loadSkills();
    } catch (reason) {
      reportError(reason);
    }
  }

  async function setWorkspaceSkillEnabled(skill: Skill, enabled: boolean) {
    try {
      await apiRequest(
        `/api/v1/workspaces/${workspace.id}/skills/${skill.slug}/${enabled ? "enable" : "disable"}`,
        { method: "POST" },
        token,
      );
      await loadSkills();
    } catch (reason) {
      reportError(reason);
    }
  }

  async function setConversationSkillOverride(skill: Skill, enabled: boolean) {
    if (!conversation) return;
    try {
      await apiRequest(
        `/api/v1/conversations/${conversation.id}/skills/${skill.slug}/override`,
        { method: "PUT", body: JSON.stringify({ enabled }) },
        token,
      );
      await loadSkills();
    } catch (reason) {
      reportError(reason);
    }
  }

  async function transitionMemory(memory: Memory, action: "confirm" | "deactivate") {
    try {
      const updated = await apiRequest<Memory>(
        `/api/v1/memories/${memory.id}/${action}`,
        { method: "POST" },
        token,
      );
      setMemories((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function resolveMemoryConflict(
    memory: Memory,
    action: "retain" | "replace" | "coexist",
  ) {
    try {
      const updated = await apiRequest<Memory>(
        `/api/v1/memories/${memory.id}/resolve-conflict`,
        { method: "POST", body: JSON.stringify({ action }) },
        token,
      );
      await loadMemories();
      setMemories((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function editMemory(memory: Memory) {
    const content = window.prompt("编辑长期记忆", memory.content)?.trim();
    if (!content) return;
    try {
      const updated = await apiRequest<Memory>(
        `/api/v1/memories/${memory.id}`,
        { method: "PATCH", body: JSON.stringify({ content, expires_at: memory.expires_at }) },
        token,
      );
      setMemories((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function deleteMemory(memory: Memory) {
    if (!window.confirm("删除这条长期记忆？")) return;
    try {
      await apiRequest(`/api/v1/memories/${memory.id}`, { method: "DELETE" }, token);
      setMemories((current) => current.filter((item) => item.id !== memory.id));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function downloadArtifact(artifact: SandboxArtifact) {
    try {
      const response = await fetch(`/api/v1/artifacts/${artifact.id}/download`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("研究产物下载失败");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function replaceDocument(documentId: string, file: File) {
    const form = new FormData();
    form.append("file", file);
    try {
      const updated = await apiRequest<WorkspaceDocument>(
        `/api/v1/documents/${documentId}/versions`,
        { method: "POST", body: form },
        token,
      );
      setDocuments((current) => current.map((item) => item.id === documentId ? updated : item));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function renameConversation() {
    if (!conversation) return;
    const title = window.prompt("新的会话标题", conversation.title)?.trim();
    if (!title) return;
    try {
      const updated = await apiRequest<Conversation>(
        `/api/v1/conversations/${conversation.id}`,
        { method: "PATCH", body: JSON.stringify({ title }) },
        token,
      );
      setConversation(updated);
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      reportError(reason);
    }
  }

  async function archiveConversation() {
    if (!conversation) return;
    try {
      await apiRequest(`/api/v1/conversations/${conversation.id}/archive`, { method: "POST" }, token);
      setConversations((current) => current.filter((item) => item.id !== conversation.id));
      setConversation(null);
    } catch (reason) {
      reportError(reason);
    }
  }

  async function deleteConversation() {
    if (!conversation || !window.confirm(`删除会话“${conversation.title}”？`)) return;
    try {
      await apiRequest(`/api/v1/conversations/${conversation.id}`, { method: "DELETE" }, token);
      setConversations((current) => current.filter((item) => item.id !== conversation.id));
      setConversation(null);
    } catch (reason) {
      reportError(reason);
    }
  }

  if (conversations.length === 0 && !showConversationForm) {
    return (
      <div className="empty-state">
        <div className="empty-orbit"><span>+</span></div>
        <h2>还没有会话，创建一个会话开始研究。</h2>
        <p>会话里的临时讨论保持私有；资料只有在你明确加入空间后才会跨会话使用。</p>
        <button className="primary-button" onClick={() => setShowConversationForm(true)}>
          创建第一个会话
        </button>
      </div>
    );
  }

  if (showConversationForm) {
    return (
      <form className="conversation-create" onSubmit={createConversation}>
        <span className="eyebrow">NEW CONVERSATION</span>
        <h2>这次准备研究什么？</h2>
        <label>会话标题<input value={conversationTitle} onChange={(event) => setConversationTitle(event.target.value)} required /></label>
        <small>保留“新会话”时，首条研究问题会自动生成标题</small>
        <div className="form-actions">
          <button type="button" onClick={() => setShowConversationForm(false)}>取消</button>
          <button className="primary-button" type="submit" disabled={busy}>创建会话</button>
        </div>
      </form>
    );
  }

  return (
    <div className="conversation-workbench">
      <aside className="conversation-list">
        <div className="panel-title"><span>会话</span><button onClick={() => setShowConversationForm(true)}>+</button></div>
        {conversations.map((item) => (
          <button
            className={conversation?.id === item.id ? "conversation-link active" : "conversation-link"}
            key={item.id}
            onClick={() => setConversation(item)}
          >
            <span>{item.title}</span><small>持续研究</small>
          </button>
        ))}
      </aside>
      <section className="chat-panel">
        {conversation ? (
          <>
            <header className="chat-header">
              <div><span className="eyebrow">CONVERSATION</span><h2>{conversation.title}</h2></div>
              <div className="chat-actions">
                <button onClick={loadDocuments}>空间资料</button>
                <button onClick={loadMemories}>长期记忆</button>
                <button onClick={loadSkills}>扩展</button>
                {latestRunId && <button onClick={() => setShowSandbox(true)}>Python 沙箱</button>}
                <button onClick={renameConversation}>重命名</button>
                <button onClick={archiveConversation}>归档</button>
                <button className="danger-link" onClick={deleteConversation}>删除</button>
                {activeRunId && <button className="stop-button" onClick={stopRun}>停止研究</button>}
              </div>
            </header>
            {error && <p className="error-banner" role="alert">{error}</p>}
            <div className="message-list" aria-live="polite">
              {messages.map((message, index) => {
                const isLiveAssistant = activeRunId && message.role === "assistant" && index === messages.length - 1;
                const content = isLiveAssistant ? streamedAnswer || liveStatus : message.content;
                return (
                  <article className={`message ${message.role}`} key={message.id}>
                    <span className="message-role">{message.role === "user" ? "你" : "研究助手"}</span>
                    <p onMouseUp={(event) => captureEvidenceSelection(message, event)}>{content}</p>
                    {evidenceSelection?.messageId === message.id && (
                      <button className="citation-button" onClick={() => void createEvidenceCheck()}>
                        核验选区来源
                      </button>
                    )}
                    {message.role === "assistant" && message.content.includes("[1]") && (
                      <button className="citation-button" onClick={() => showCitation(message.id)}>查看引用 [1]</button>
                    )}
                  </article>
                );
              })}
              {Array.isArray(plan) && (
                <section className="run-plan"><strong>研究计划</strong>{plan.map((task, index) => <span key={index}>{String((task as { title?: string }).title ?? "研究任务")}</span>)}</section>
              )}
            </div>
            {toolApprovals.filter((approval) => approval.status === "pending").map((approval) => (
              <section className="tool-approval" aria-labelledby={`tool-approval-${approval.id}`} key={approval.id}>
                <div>
                  <span className="eyebrow">TOOL APPROVAL</span>
                  <h3 id={`tool-approval-${approval.id}`}>工具调用审批</h3>
                  <p>{approval.safe_summary}</p>
                </div>
                <dl>
                  <div><dt>风险</dt><dd>{approval.risk_level === "write" ? "写入外部系统" : approval.risk_level}</dd></div>
                  <div><dt>工具</dt><dd>{approval.tool_name}</dd></div>
                  <div><dt>参数哈希</dt><dd title={approval.parameters_hash}>{approval.parameters_hash.slice(0, 16)}…</dd></div>
                  <div><dt>过期时间</dt><dd>{new Date(approval.expires_at).toLocaleString()}</dd></div>
                </dl>
                <div className="tool-approval-actions">
                  <button type="button" onClick={() => void decideToolApproval(approval, "reject")} disabled={busy}>拒绝调用</button>
                  <button className="primary-button" type="button" onClick={() => void decideToolApproval(approval, "approve")} disabled={busy}>批准调用</button>
                </div>
              </section>
            ))}
            <form className="composer" onSubmit={sendMessage}>
              {attachments.length > 0 && (
                <div className="attachment-row">
                  {attachments.map((item) => (
                    <span className={`attachment-chip ${item.status}`} key={item.id}>
                      {item.filename} · {item.status === "ready" ? "可用" : item.status === "failed" ? "失败" : "处理中"}
                      {item.status === "ready" && !item.filename.includes("已加入空间") && (
                        <button type="button" onClick={() => promoteAttachment(item.id)}>加入空间</button>
                      )}
                    </span>
                  ))}
                </div>
              )}
              {files.length > 0 && <p className="selected-files">待上传：{files.map((file) => file.name).join("、")}</p>}
              <textarea value={composer} onChange={(event) => setComposer(event.target.value)} placeholder="提出一个研究问题…" rows={3} />
              <div className="composer-actions">
                <label className="file-button">添加文件<input type="file" multiple onChange={selectFiles} /></label>
                <span>单文件 ≤ 50 MB，最多 20 个；默认仅当前会话可见</span>
                <button className="primary-button" type="submit" disabled={busy || Boolean(activeRunId) || !composer.trim()}>{busy ? "研究中…" : "发送并研究"}</button>
              </div>
            </form>
          </>
        ) : (
          <div className="conversation-placeholder"><h2>选择一个会话</h2><p>继续已有研究，或创建一个新会话。</p></div>
        )}
      </section>
      {citation && (
        <aside className="citation-drawer">
          <button className="drawer-close" onClick={() => setCitation(null)}>关闭</button>
          <span className="eyebrow">EVIDENCE SPAN</span>
          <h3>{citation.filename}</h3>
          <p>
            {citation.source_type === "web"
              ? `网页快照${citation.source_captured_at ? ` · ${new Date(citation.source_captured_at).toLocaleString()}` : ""}`
              : citation.document_version ? `版本 ${citation.document_version}` : "会话附件"}
            {citation.page_number ? ` · 第 ${citation.page_number} 页` : ""}
          </p>
          {citation.source_url && <a href={citation.source_url} target="_blank" rel="noreferrer">打开来源网页</a>}
          <blockquote>{citation.evidence_text}</blockquote>
          <small>来源哈希 {citation.source_hash.slice(0, 12)}…</small>
        </aside>
      )}
      {evidenceCheck && (
        <aside className="citation-drawer evidence-check-drawer">
          <button className="drawer-close" onClick={() => setEvidenceCheck(null)}>关闭</button>
          <span className="eyebrow">EVIDENCE CHECK</span>
          <h3>{evidenceCheck.verdict}</h3>
          <blockquote>{evidenceCheck.claim}</blockquote>
          <p>{evidenceCheck.reason}</p>
          {evidenceCheck.evidence.map((item) => (
            <article key={item.id}>
              <strong>{item.filename}</strong>
              <blockquote>{item.evidence_text}</blockquote>
            </article>
          ))}
          <small>{evidenceCheck.disclaimer}</small>
        </aside>
      )}
      {showDocuments && (
        <aside className="document-drawer">
          <button className="drawer-close" onClick={() => setShowDocuments(false)}>关闭</button>
          <span className="eyebrow">WORKSPACE DOCUMENTS</span>
          <h3>空间资料</h3>
          {documents.length === 0 && <p>还没有加入空间的资料。</p>}
          {documents.map((document) => (
            <article key={document.id}>
              <strong>{document.filename}</strong>
              <small>版本 {document.version} · {document.sha256.slice(0, 10)}…</small>
              <label>替换为新版本<input type="file" onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void replaceDocument(document.id, file);
              }} /></label>
            </article>
          ))}
        </aside>
      )}
      {showMemories && (
        <aside className="document-drawer">
          <button className="drawer-close" onClick={() => setShowMemories(false)}>关闭</button>
          <span className="eyebrow">MEMORY CENTER</span>
          <h3>长期记忆</h3>
          <label>
            <input
              type="checkbox"
              checked={workspace.memory_auto_apply}
              onChange={(event) => void updateMemoryAutoApply(event.target.checked)}
            />
            允许低风险记忆自动生效
          </label>
          <p>关闭后，新提取的低风险记忆将保留为候选，直到你手动确认。</p>
          <form onSubmit={createMemory}>
            <label>新增空间记忆<input value={memoryDraft} onChange={(event) => setMemoryDraft(event.target.value)} /></label>
            <button className="primary-button" type="submit">创建候选</button>
          </form>
          {memories.length === 0 && <p>还没有记忆候选。</p>}
          {memories.map((memory) => (
            <article key={memory.id}>
              <strong>{memory.content}</strong>
              <small>{memory.scope} · {memory.status} · {memory.risk_level}</small>
              {memory.source_message_id && <small>来源消息：{memory.source_message_id}</small>}
              {memory.conflict && memory.conflict.status === "pending" && (
                <div className="memory-conflict">
                  <p>发现冲突</p>
                  <del>{memory.conflict.old_content}</del>
                  <ins>{memory.conflict.new_content}</ins>
                  <div className="form-actions">
                    <button onClick={() => resolveMemoryConflict(memory, "retain")}>保留旧值</button>
                    <button onClick={() => resolveMemoryConflict(memory, "replace")}>替换为新值</button>
                    <button onClick={() => resolveMemoryConflict(memory, "coexist")}>两者并存</button>
                  </div>
                </div>
              )}
              <div className="form-actions">
                {(memory.status === "candidate" || memory.status === "inactive") && <button onClick={() => transitionMemory(memory, "confirm")}>确认生效</button>}
                {memory.status === "active" && <button onClick={() => transitionMemory(memory, "deactivate")}>停用</button>}
                <button onClick={() => editMemory(memory)}>编辑</button>
                <button className="danger-link" onClick={() => deleteMemory(memory)}>删除</button>
              </div>
            </article>
          ))}
        </aside>
      )}
      {showSkills && (
        <aside className="document-drawer">
          <button className="drawer-close" onClick={() => setShowSkills(false)}>关闭</button>
          <span className="eyebrow">TRUSTED SKILLS</span>
          <h3>扩展</h3>
          <p>Skill 只约束工作方式；不可执行，也不会因安装而获取额外数据权限。</p>
          <article>
            <strong>本地受信 MCP</strong>
            <small>{mcpWorkspaceEnabled ? "已在此空间启用" : "未在此空间启用"}</small>
            <small>当前会话工具：{mcpTools.map((tool) => tool.name).join("、") || "无"}</small>
            <div className="form-actions">
              <button onClick={() => void setWorkspaceMcpEnabled(!mcpWorkspaceEnabled)}>
                {mcpWorkspaceEnabled ? "停用本地 MCP" : "启用本地 MCP"}
              </button>
            </div>
          </article>
          {skills.map((skill) => {
            const effective = effectiveSkills.find((item) => item.slug === skill.slug);
            return (
              <article key={skill.slug}>
                <strong>{skill.name}</strong>
                <small>{skill.publisher} · v{skill.version}</small>
                <p>{skill.description}</p>
                <small>允许工具：{skill.manifest.allowed_tools.join("、") || "无"}</small>
                <small>所需能力：{skill.manifest.required_capabilities.join("、") || "无"}</small>
                <div className="form-actions">
                  {!skill.installed && <button onClick={() => installSkill(skill)}>安装</button>}
                  {skill.installed && !effective && (
                    <button onClick={() => setWorkspaceSkillEnabled(skill, true)}>在此空间启用</button>
                  )}
                  {effective && (
                    <>
                      <button onClick={() => setWorkspaceSkillEnabled(skill, !effective.workspace_enabled)}>
                        {effective.workspace_enabled ? "在此空间停用" : "在此空间启用"}
                      </button>
                      <label>
                        <input
                          type="checkbox"
                          checked={effective.enabled}
                          disabled={!effective.workspace_enabled}
                          onChange={(event) => void setConversationSkillOverride(skill, event.target.checked)}
                        />
                        在当前会话生效
                      </label>
                    </>
                  )}
                </div>
              </article>
            );
          })}
        </aside>
      )}
      {showSandbox && (
        <aside className="document-drawer sandbox-drawer">
          <button className="drawer-close" onClick={() => setShowSandbox(false)}>关闭</button>
          <span className="eyebrow">PYTHON SANDBOX</span>
          <h3>Agent 受限计算</h3>
          <p>Agent 仅在研究需要时创建执行；Docker 默认无网络、只读根文件系统且非 root 运行。</p>
          {sandboxTodos.length === 0 && <p>本次研究没有请求 Python Sandbox。</p>}
          {sandboxTodos.map((todo) => (
            <section className="sandbox-result" key={todo.id}>
              <strong>{todo.title} · {todo.status}</strong>
              <p>目的：{todo.purpose}</p>
              {todo.sandbox_code && <pre>{todo.sandbox_code}</pre>}
              <p>输入附件：{todo.sandbox_input_attachment_ids.join("、") || "无"}</p>
              {todo.result_summary && <pre>{todo.result_summary}</pre>}
              {todo.failure_reason && <p className="error-banner">{todo.failure_reason}</p>}
              {todo.sandbox_artifacts.map((artifact) => (
                <button key={artifact.id} onClick={() => downloadArtifact(artifact)}>
                  下载 {artifact.filename} · {artifact.size_bytes} 字节
                </button>
              ))}
            </section>
          ))}
        </aside>
      )}
    </div>
  );
}
