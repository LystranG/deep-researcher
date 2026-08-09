import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";

beforeEach(() => {
  window.localStorage.clear();
});

test("用户注册后可以创建并进入空空间", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "token" }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      id: "workspace-1",
      name: "新能源汽车研究",
      description: null,
      instructions: null,
      archived: false
    }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(null), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.type(screen.getByLabelText("邮箱"), "researcher@example.com");
  await user.type(screen.getByLabelText("密码"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "创建本地账户" }));
  await user.type(screen.getByLabelText("空间名称"), "新能源汽车研究");
  await user.click(screen.getByRole("button", { name: "创建空间" }));

  expect(await screen.findByRole("heading", { name: "新能源汽车研究" })).toBeInTheDocument();
  expect(screen.getByText("还没有会话，创建一个会话开始研究。")) .toBeInTheDocument();
});

test("用户可以在空间中创建第一个会话", async () => {
  window.localStorage.setItem("deep-researcher-token", "token");
  const workspace = {
    id: "workspace-1",
    name: "行业研究",
    description: null,
    instructions: null,
    archived: false,
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [workspace] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      id: "conversation-1",
      workspace_id: "workspace-1",
      title: "市场规模",
      archived: false,
    }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(null), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: /行业研究/ }));
  await user.click(await screen.findByRole("button", { name: "创建第一个会话" }));
  await user.type(screen.getByLabelText("会话标题"), "市场规模");
  await user.click(screen.getByRole("button", { name: "创建会话" }));

  expect(await screen.findByRole("heading", { name: "市场规模" })).toBeInTheDocument();
  expect(screen.getByPlaceholderText("提出一个研究问题…")).toBeInTheDocument();
});

test("用户可以在记忆中心关闭空间的低风险记忆自动生效", async () => {
  window.localStorage.setItem("deep-researcher-token", "token");
  const workspace = {
    id: "workspace-1",
    name: "行业研究",
    description: null,
    instructions: null,
    archived: false,
    memory_auto_apply: true,
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [workspace] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      id: "conversation-1",
      workspace_id: "workspace-1",
      title: "市场规模",
      archived: false,
    }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(null), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(null), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...workspace, memory_auto_apply: false }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: /行业研究/ }));
  await user.click(await screen.findByRole("button", { name: "创建第一个会话" }));
  await user.type(screen.getByLabelText("会话标题"), "市场规模");
  await user.click(screen.getByRole("button", { name: "创建会话" }));
  await user.click(await screen.findByRole("button", { name: "长期记忆" }));

  const toggle = await screen.findByRole("checkbox", { name: "允许低风险记忆自动生效" });
  expect(toggle).toBeChecked();
  await user.click(toggle);

  expect(toggle).not.toBeChecked();
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/v1/workspaces/workspace-1",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({
        name: "行业研究",
        description: null,
        instructions: null,
        memory_auto_apply: false,
      }),
    }),
  );
});

test("用户可以从扩展目录安装受信任 Skill", async () => {
  window.localStorage.setItem("deep-researcher-token", "token");
  const workspace = {
    id: "workspace-1",
    name: "行业研究",
    description: null,
    instructions: null,
    archived: false,
    memory_auto_apply: true,
  };
  const conversation = {
    id: "conversation-1",
    workspace_id: "workspace-1",
    title: "来源比较",
    archived: false,
  };
  const sourceComparison = {
    slug: "source-comparison",
    name: "来源比较",
    description: "比较同一主张的多份来源。",
    publisher: "深度研究工作台",
    version: "1.0.0",
    content_hash: "a".repeat(64),
    manifest: {
      executable: false,
      allowed_tools: ["document_search"],
      required_capabilities: ["workspace_documents.read"],
    },
    installed: false,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/workspaces") return new Response(JSON.stringify({ items: [workspace] }));
    if (path === "/api/v1/workspaces/workspace-1/conversations") {
      return new Response(JSON.stringify({ items: [conversation] }));
    }
    if (path === "/api/v1/conversations/conversation-1/messages") {
      return new Response(JSON.stringify({ items: [] }));
    }
    if (path.endsWith("/latest-run") || path.endsWith("/active-run")) {
      return new Response(JSON.stringify(null));
    }
    if (path === "/api/v1/skills/catalog") {
      const installed = init?.method === "POST" || fetchMock.mock.calls.some(([url, options]) => (
        String(url) === "/api/v1/skills/source-comparison/install" && options?.method === "POST"
      ));
      return new Response(JSON.stringify({ items: [{ ...sourceComparison, installed }] }));
    }
    if (path === "/api/v1/conversations/conversation-1/skills") {
      return new Response(JSON.stringify({ items: [] }));
    }
    if (path === "/api/v1/conversations/conversation-1/mcp/local-trusted/tools") {
      return new Response(JSON.stringify({
        workspace_enabled: true,
        items: [
          {
            name: "research_notes.create",
            description: "创建研究记录",
            input_schema: { type: "object" },
          },
        ],
      }));
    }
    if (path === "/api/v1/skills/source-comparison/install") {
      return new Response(JSON.stringify({ ...sourceComparison, installed: true }), { status: 201 });
    }
    return new Response(JSON.stringify({ detail: `unexpected request: ${path}` }), { status: 500 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: /行业研究/ }));
  await user.click(await screen.findByRole("button", { name: /来源比较/ }));
  await user.click(await screen.findByRole("button", { name: "扩展" }));

  expect(await screen.findByText("Skill 只约束工作方式；不可执行，也不会因安装而获取额外数据权限。")).toBeInTheDocument();
  expect(await screen.findByText("当前会话工具：research_notes.create")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "安装" }));

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/skills/source-comparison/install",
    expect.objectContaining({ method: "POST" }),
  );
  expect(await screen.findByRole("button", { name: "在此空间启用" })).toBeInTheDocument();
});

// 用户可查看本次研究读取的网页正文
test("用户可以查看研究来源保存的网页正文", async () => {
  window.localStorage.setItem("deep-researcher-token", "token");
  const workspace = {
    id: "workspace-1",
    name: "行业研究",
    description: null,
    instructions: null,
    archived: false,
    memory_auto_apply: true,
  };
  const conversation = {
    id: "conversation-1",
    workspace_id: "workspace-1",
    title: "市场规模",
    archived: false,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/workspaces") {
      return new Response(JSON.stringify({ items: [workspace] }));
    }
    if (path === "/api/v1/workspaces/workspace-1/conversations") {
      return new Response(JSON.stringify({ items: [conversation] }));
    }
    if (path === "/api/v1/conversations/conversation-1/messages") {
      return new Response(JSON.stringify({ items: [
        { id: "user-1", role: "user", content: "研究储能市场", version: 1 },
        { id: "assistant-1", role: "assistant", content: "研究已完成。", version: 1 },
      ] }));
    }
    if (path === "/api/v1/conversations/conversation-1/latest-run") {
      return new Response(JSON.stringify({ run_id: "run-1", status: "completed", tasks: [] }));
    }
    if (path === "/api/v1/conversations/conversation-1/active-run") {
      return new Response(JSON.stringify(null));
    }
    if (path === "/api/v1/runs/run-1/todos") {
      return new Response(JSON.stringify({ items: [] }));
    }
    if (path === "/api/v1/runs/run-1/sources") {
      return new Response(JSON.stringify({ items: [{
        id: "source-1",
        ordinal: 1,
        title: "储能产业报告",
        url: "https://example.com/storage-report",
        content_kind: "web_page",
        captured_at: "2026-08-09T08:00:00Z",
        content_preview: "储能装机持续增长。",
      }] }));
    }
    if (path === "/api/v1/sources/source-1") {
      return new Response(JSON.stringify({
        id: "source-1",
        ordinal: 1,
        title: "储能产业报告",
        url: "https://example.com/storage-report",
        content_kind: "web_page",
        captured_at: "2026-08-09T08:00:00Z",
        content_preview: "储能装机持续增长。",
        content: "储能装机持续增长，电网侧需求正在扩大。",
      }));
    }
    return new Response(JSON.stringify({ detail: `unexpected request: ${path}` }), { status: 500 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: /行业研究/ }));
  await user.click(await screen.findByRole("button", { name: /市场规模/ }));
  await user.click(await screen.findByRole("button", { name: "研究来源" }));
  expect(await screen.findByText("已读取网页正文")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /储能产业报告/ }));

  expect(await screen.findByText("储能装机持续增长，电网侧需求正在扩大。")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开原网页" })).toHaveAttribute(
    "href",
    "https://example.com/storage-report",
  );
});

test("用户刷新后可以拒绝待审批工具调用并继续原研究运行", async () => {
  window.localStorage.setItem("deep-researcher-token", "token");
  const workspace = {
    id: "workspace-1",
    name: "工具研究",
    description: null,
    instructions: null,
    archived: false,
    memory_auto_apply: true,
  };
  const conversation = {
    id: "conversation-1",
    workspace_id: "workspace-1",
    title: "审批恢复",
    archived: false,
  };
  let rejected = false;
  let streamCount = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/workspaces") {
      return new Response(JSON.stringify({ items: [workspace] }));
    }
    if (path === "/api/v1/workspaces/workspace-1/conversations") {
      return new Response(JSON.stringify({ items: [conversation] }));
    }
    if (path === "/api/v1/conversations/conversation-1/messages") {
      return new Response(JSON.stringify({
        items: rejected
          ? [
            { id: "user-1", role: "user", content: "记录审批主题", version: 1 },
            { id: "assistant-1", role: "assistant", content: "已在拒绝工具后继续研究。", version: 2 },
          ]
          : [
            { id: "user-1", role: "user", content: "记录审批主题", version: 1 },
            { id: "assistant-1", role: "assistant", content: "", version: 1 },
          ],
      }));
    }
    if (path === "/api/v1/conversations/conversation-1/latest-run") {
      return new Response(JSON.stringify({
        run_id: "run-1",
        status: rejected ? "completed" : "waiting_approval",
        tasks: [],
      }));
    }
    if (path === "/api/v1/conversations/conversation-1/active-run") {
      return new Response(JSON.stringify(rejected ? null : {
        run_id: "run-1",
        assistant_message_id: "assistant-1",
        status: "waiting_approval",
      }));
    }
    if (path.startsWith("/api/v1/runs/run-1/events")) {
      streamCount += 1;
      const body = streamCount === 1
        ? "id: 1\nevent: tool_approval_requested\ndata: {\"approval_id\":\"approval-1\"}\n\n"
        : [
          "id: 1\nevent: tool_approval_requested\ndata: {\"approval_id\":\"approval-1\"}\n\n",
          "id: 2\nevent: tool_call_rejected\ndata: {\"status\":\"rejected\"}\n\n",
          "id: 3\nevent: assistant_delta\ndata: {\"content\":\"已在拒绝工具后继续研究。\"}\n\n",
          "id: 4\nevent: run_completed\ndata: {\"status\":\"completed\"}\n\n",
        ].join("");
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (path === "/api/v1/runs/run-1/tool-approvals") {
      return new Response(JSON.stringify({
        items: [{
          id: "approval-1",
          run_id: "run-1",
          tool_call_id: "tool-call-1",
          tool_name: "research_notes.create",
          risk_level: "write",
          parameters_hash: "a".repeat(64),
          safe_summary: "创建研究记录：审批恢复",
          status: rejected ? "rejected" : "pending",
          expires_at: "2026-08-08T14:00:00Z",
        }],
      }));
    }
    if (path === "/api/v1/tool-approvals/approval-1/reject" && init?.method === "POST") {
      rejected = true;
      return new Response(JSON.stringify({ id: "approval-1", run_id: "run-1", status: "rejected" }));
    }
    return new Response(JSON.stringify({ detail: `unexpected request: ${path}` }), { status: 500 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: /工具研究/ }));
  await user.click(await screen.findByRole("button", { name: /审批恢复/ }));

  expect(await screen.findByRole("heading", { name: "工具调用审批" })).toBeInTheDocument();
  expect(screen.getByText("创建研究记录：审批恢复")).toBeInTheDocument();
  expect(screen.getByText("写入外部系统")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "拒绝调用" }));

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/tool-approvals/approval-1/reject",
    expect.objectContaining({ method: "POST" }),
  );
  expect(await screen.findByText("已在拒绝工具后继续研究。", { exact: true })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "工具调用审批" })).not.toBeInTheDocument();
});
