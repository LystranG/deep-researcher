import { FormEvent, useEffect, useState } from "react";

import { apiRequest, Workspace } from "./api";
import { WorkspaceView } from "./WorkspaceView";
import "./styles.css";

const TOKEN_KEY = "deep-researcher-token";

type AuthPayload = { access_token: string };
type WorkspaceList = { items: Workspace[] };

export function App() {
  const [token, setToken] = useState<string | null>(() => window.localStorage.getItem(TOKEN_KEY));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<Workspace | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    void loadWorkspaces(token);
  }, [token]);

  async function loadWorkspaces(accessToken: string) {
    try {
      const response = await apiRequest<WorkspaceList>("/api/v1/workspaces", {}, accessToken);
      setWorkspaces(response.items);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "空间加载失败");
    }
  }

  async function register(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await apiRequest<AuthPayload>("/api/v1/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      window.localStorage.setItem(TOKEN_KEY, response.access_token);
      setToken(response.access_token);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "注册失败");
    } finally {
      setBusy(false);
    }
  }

  async function createWorkspace(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      const workspace = await apiRequest<Workspace>(
        "/api/v1/workspaces",
        { method: "POST", body: JSON.stringify({ name: workspaceName }) },
        token,
      );
      setWorkspaces((current) => [...current, workspace]);
      setSelected(workspace);
      setWorkspaceName("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "空间创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function renameWorkspace() {
    if (!token || !selected) return;
    const name = window.prompt("新的空间名称", selected.name)?.trim();
    if (!name) return;
    try {
      const updated = await apiRequest<Workspace>(
        `/api/v1/workspaces/${selected.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            description: selected.description,
            instructions: selected.instructions,
            memory_auto_apply: selected.memory_auto_apply,
          }),
        },
        token,
      );
      setSelected(updated);
      setWorkspaces((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "空间更新失败");
    }
  }

  function updateWorkspace(updated: Workspace) {
    setSelected(updated);
    setWorkspaces((current) => current.map((item) => item.id === updated.id ? updated : item));
  }

  async function archiveWorkspace() {
    if (!token || !selected) return;
    try {
      await apiRequest(`/api/v1/workspaces/${selected.id}/archive`, { method: "POST" }, token);
      setWorkspaces((current) => current.filter((item) => item.id !== selected.id));
      setSelected(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "空间归档失败");
    }
  }

  async function deleteWorkspace() {
    if (!token || !selected) return;
    if (!window.confirm(`删除“${selected.name}”及其会话、文档和产物？此操作需要二次确认。`)) return;
    try {
      await apiRequest(
        `/api/v1/workspaces/${selected.id}`,
        { method: "DELETE", body: JSON.stringify({ confirm_name: selected.name }) },
        token,
      );
      setWorkspaces((current) => current.filter((item) => item.id !== selected.id));
      setSelected(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "空间删除失败");
    }
  }

  if (!token) {
    return (
      <main className="auth-shell">
        <section className="brand-panel">
          <span className="eyebrow">DEEP RESEARCH WORKSPACE</span>
          <h1>把研究变成可积累、可追溯的长期工作。</h1>
          <p>多会话、可信资料和稳定引用，从一个隔离的空间开始。</p>
          <div className="signal-grid" aria-hidden="true">
            <span>空间隔离</span><span>事件恢复</span><span>证据定位</span>
          </div>
        </section>
        <form className="auth-card" onSubmit={register}>
          <div>
            <span className="eyebrow">本地账户</span>
            <h2>开始你的研究空间</h2>
          </div>
          <label>
            邮箱
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              required
            />
          </label>
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={12}
              autoComplete="new-password"
              required
            />
          </label>
          {error && <p className="error-banner" role="alert">{error}</p>}
          <button className="primary-button" disabled={busy} type="submit">
            {busy ? "正在创建…" : "创建本地账户"}
          </button>
          <p className="helper">账户只用于隔离本地 Workspace 数据。</p>
        </form>
      </main>
    );
  }

  return (
    <main className="workspace-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="brand-mark">DR</span>
          <div><strong>深度研究</strong><small>工作台</small></div>
        </div>
        <nav aria-label="空间列表">
          <span className="nav-caption">你的空间</span>
          {workspaces.map((workspace) => (
            <button
              className={selected?.id === workspace.id ? "space-link active" : "space-link"}
              key={workspace.id}
              onClick={() => setSelected(workspace)}
            >
              <span>{workspace.name.slice(0, 1)}</span>{workspace.name}
            </button>
          ))}
        </nav>
        <form className="new-space-form" onSubmit={createWorkspace}>
          <label>
            空间名称
            <input
              value={workspaceName}
              onChange={(event) => setWorkspaceName(event.target.value)}
              placeholder="例如：新能源产业"
              required
            />
          </label>
          <button type="submit" disabled={busy}>创建空间</button>
        </form>
      </aside>
      <section className="workspace-main">
        {error && <p className="error-banner" role="alert">{error}</p>}
        {selected ? (
          <>
            <header className="workspace-header">
              <div><span className="eyebrow">WORKSPACE</span><h1>{selected.name}</h1></div>
              <div className="workspace-actions">
                <span className="status-pill"><i />数据已隔离</span>
                <button onClick={renameWorkspace}>编辑</button>
                <button onClick={archiveWorkspace}>归档</button>
                <button className="danger-link" onClick={deleteWorkspace}>删除</button>
              </div>
            </header>
            <WorkspaceView workspace={selected} token={token} onWorkspaceUpdated={updateWorkspace} />
          </>
        ) : (
          <div className="welcome-state">
            <span className="eyebrow">RESEARCH, WITH MEMORY</span>
            <h1>选择一个空间，<br />或创建新的长期研究边界。</h1>
            <p>空间会保存会话、可信资料和后续启用的记忆与扩展。</p>
          </div>
        )}
      </section>
    </main>
  );
}
