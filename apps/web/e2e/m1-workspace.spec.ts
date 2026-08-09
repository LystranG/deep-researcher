import { expect, test } from "@playwright/test";

test("用户可以创建空间、会话并完成一轮持久化研究", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("邮箱").fill(`e2e-${Date.now()}@example.com`);
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "创建本地账户" }).click();

  await page.getByLabel("空间名称").fill("气候研究");
  await page.getByRole("button", { name: "创建空间" }).click();
  await expect(page.getByRole("heading", { name: "气候研究" })).toBeVisible();

  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("年度趋势");
  await page.getByRole("button", { name: "创建会话" }).click();

  await page.getByPlaceholder("提出一个研究问题…").fill("总结今年的气候研究方向");
  await page.getByRole("button", { name: "发送并研究" }).click();

  await expect(page.getByText("总结今年的气候研究方向", { exact: true })).toBeVisible();
  await expect(
    page.getByText("已完成对“总结今年的气候研究方向”的初步研究。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("分析用户问题并形成初步结论", { exact: true })).toBeVisible();

  await page.reload();
  await page.getByRole("button", { name: /气候研究/ }).click();
  await page.getByRole("button", { name: /年度趋势/ }).click();
  await expect(
    page.getByText("已完成对“总结今年的气候研究方向”的初步研究。", { exact: true }),
  ).toBeVisible();
});

test("待审批工具调用刷新后仍可拒绝且跨用户不可访问", async ({ page, request }) => {
  const email = `approval-${Date.now()}@example.com`;
  await page.goto("/");
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "创建本地账户" }).click();
  await page.getByLabel("空间名称").fill("审批研究");
  await page.getByRole("button", { name: "创建空间" }).click();
  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("工具风险");
  await page.getByRole("button", { name: "创建会话" }).click();

  await page.getByPlaceholder("提出一个研究问题…").fill("请使用受信工具记录研究主题：审批拒绝验收");
  await page.getByRole("button", { name: "发送并研究" }).click();
  await expect(page.getByRole("heading", { name: "工具调用审批" })).toBeVisible();
  await expect(page.getByText("创建研究记录：审批拒绝验收")).toBeVisible();

  const ownerState = await page.evaluate(async () => {
    const token = window.localStorage.getItem("deep-researcher-token") ?? "";
    const headers = { Authorization: `Bearer ${token}` };
    const workspaces = await fetch("/api/v1/workspaces", { headers }).then((response) => response.json());
    const workspaceId = workspaces.items[0].id as string;
    const conversations = await fetch(`/api/v1/workspaces/${workspaceId}/conversations`, { headers })
      .then((response) => response.json());
    const conversationId = conversations.items[0].id as string;
    const run = await fetch(`/api/v1/conversations/${conversationId}/active-run`, { headers })
      .then((response) => response.json());
    const approvals = await fetch(`/api/v1/runs/${run.run_id}/tool-approvals`, { headers })
      .then((response) => response.json());
    return { token, workspaceId, conversationId, runId: run.run_id as string, approvalId: approvals.items[0].id as string };
  });
  const outsider = await request.post("/api/v1/auth/register", {
    data: { email: `outsider-${Date.now()}@example.com`, password: "correct horse battery" },
  });
  const outsiderToken = (await outsider.json()).access_token as string;
  const forbidden = await request.get(`/api/v1/runs/${ownerState.runId}/tool-approvals`, {
    headers: { Authorization: `Bearer ${outsiderToken}` },
  });
  expect(forbidden.status()).toBe(404);

  await page.reload();
  await page.getByRole("button", { name: /审批研究/ }).click();
  await page.getByRole("button", { name: /工具风险/ }).click();
  await expect(page.getByRole("heading", { name: "工具调用审批" })).toBeVisible();
  await page.getByRole("button", { name: "拒绝调用" }).click();

  await expect(page.getByRole("heading", { name: "工具调用审批" })).not.toBeVisible();
  await expect(page.getByText(/已完成对“请使用受信工具记录研究主题：审批拒绝验收”的初步研究。/)).toBeVisible();
  const toolRuns = await request.get(`/api/v1/runs/${ownerState.runId}/tool-runs`, {
    headers: { Authorization: `Bearer ${ownerState.token}` },
  });
  expect((await toolRuns.json()).items).toEqual([]);
});

test("等待审批时取消不会产生工具执行或新结论", async ({ page, request }) => {
  await page.goto("/");
  await page.getByLabel("邮箱").fill(`cancel-approval-${Date.now()}@example.com`);
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "创建本地账户" }).click();
  await page.getByLabel("空间名称").fill("取消研究");
  await page.getByRole("button", { name: "创建空间" }).click();
  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("取消工具");
  await page.getByRole("button", { name: "创建会话" }).click();
  await page.getByPlaceholder("提出一个研究问题…").fill("请使用受信工具记录研究主题：取消后不得执行");
  await page.getByRole("button", { name: "发送并研究" }).click();
  await expect(page.getByRole("heading", { name: "工具调用审批" })).toBeVisible();

  const runState = await page.evaluate(async () => {
    const token = window.localStorage.getItem("deep-researcher-token") ?? "";
    const headers = { Authorization: `Bearer ${token}` };
    const workspaces = await fetch("/api/v1/workspaces", { headers }).then((response) => response.json());
    const workspaceId = workspaces.items[0].id as string;
    const conversations = await fetch(`/api/v1/workspaces/${workspaceId}/conversations`, { headers })
      .then((response) => response.json());
    const conversationId = conversations.items[0].id as string;
    const run = await fetch(`/api/v1/conversations/${conversationId}/active-run`, { headers })
      .then((response) => response.json());
    return { token, conversationId, runId: run.run_id as string };
  });
  await page.getByRole("button", { name: "停止研究" }).click();
  await expect(page.getByRole("heading", { name: "工具调用审批" })).not.toBeVisible();

  await expect.poll(async () => {
    const response = await request.get(`/api/v1/conversations/${runState.conversationId}/latest-run`, {
      headers: { Authorization: `Bearer ${runState.token}` },
    });
    return (await response.json()).status;
  }).toBe("cancelled");
  const toolRuns = await request.get(`/api/v1/runs/${runState.runId}/tool-runs`, {
    headers: { Authorization: `Bearer ${runState.token}` },
  });
  const messages = await request.get(`/api/v1/conversations/${runState.conversationId}/messages`, {
    headers: { Authorization: `Bearer ${runState.token}` },
  });
  expect((await toolRuns.json()).items).toEqual([]);
  expect((await messages.json()).items.at(-1).content).toBe("");
  await expect(page.getByText(/已完成对“请使用受信工具记录研究主题：取消后不得执行”/)).not.toBeVisible();
});

test("切换会话后不会展示其他会话的附件", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("邮箱").fill(`attachment-isolation-${Date.now()}@example.com`);
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "创建本地账户" }).click();
  await page.getByLabel("空间名称").fill("附件隔离");
  await page.getByRole("button", { name: "创建空间" }).click();
  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("会话 A");
  await page.getByRole("button", { name: "创建会话" }).click();

  await page.locator('input[type="file"]').first().setInputFiles({
    name: "private-a.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("只属于会话 A 的附件"),
  });
  await page.getByPlaceholder("提出一个研究问题…").fill("分析当前会话附件");
  await page.getByRole("button", { name: "发送并研究" }).click();
  await expect(page.locator(".attachment-chip", { hasText: "private-a.txt" })).toBeVisible();
  await expect.poll(async () => page.evaluate(async () => {
    const token = window.localStorage.getItem("deep-researcher-token") ?? "";
    const headers = { Authorization: `Bearer ${token}` };
    const workspaces = await fetch("/api/v1/workspaces", { headers }).then((response) => response.json());
    const conversations = await fetch(
      `/api/v1/workspaces/${workspaces.items[0].id}/conversations`,
      { headers },
    ).then((response) => response.json());
    return fetch(`/api/v1/conversations/${conversations.items[0].id}/active-run`, { headers })
      .then((response) => response.json());
  })).toBeNull();

  await page.getByRole("button", { name: "+" }).click();
  await page.getByLabel("会话标题").fill("会话 B");
  await page.getByRole("button", { name: "创建会话" }).click();

  await expect(page.getByRole("heading", { name: "会话 B" })).toBeVisible();
  await expect(page.locator(".attachment-chip", { hasText: "private-a.txt" })).not.toBeVisible();
});

test("Memory 与 Skill 保持 Workspace 分层且停用后不再生效", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("邮箱").fill(`layering-${Date.now()}@example.com`);
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "创建本地账户" }).click();
  await page.getByLabel("空间名称").fill("分层空间 A");
  await page.getByRole("button", { name: "创建空间" }).click();
  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("分层会话 A");
  await page.getByRole("button", { name: "创建会话" }).click();

  await page.getByRole("button", { name: "扩展" }).click();
  await page.getByRole("button", { name: "安装" }).click();
  await page.getByRole("button", { name: "在此空间启用" }).click();
  await expect(page.getByRole("button", { name: "在此空间停用" })).toBeVisible();
  await page.getByRole("button", { name: "关闭" }).click();

  await page.getByRole("button", { name: "长期记忆" }).click();
  await page.getByLabel("新增空间记忆").fill("仅空间 A 使用简体中文");
  await page.getByRole("button", { name: "创建候选" }).click();
  const memory = page.locator(".document-drawer article", { hasText: "仅空间 A 使用简体中文" });
  await memory.getByRole("button", { name: "确认生效" }).click();
  await expect(memory).toContainText("active");

  await page.getByLabel("空间名称").fill("分层空间 B");
  await page.getByRole("button", { name: "创建空间" }).click();
  await page.getByRole("button", { name: "创建第一个会话" }).click();
  await page.getByLabel("会话标题").fill("分层会话 B");
  await page.getByRole("button", { name: "创建会话" }).click();

  await expect(page.getByText("仅空间 A 使用简体中文")).not.toBeVisible();
  await page.getByRole("button", { name: "长期记忆" }).click();
  await expect(page.getByText("还没有记忆候选。")).toBeVisible();
  await page.getByRole("button", { name: "关闭" }).click();
  await page.getByRole("button", { name: "扩展" }).click();
  await expect(page.getByRole("button", { name: "在此空间启用" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "在当前会话生效" })).not.toBeVisible();
  await page.getByRole("button", { name: "关闭" }).click();

  await page.getByRole("button", { name: /分层空间 A/ }).click();
  await page.getByRole("button", { name: /分层会话 A/ }).click();
  await page.getByRole("button", { name: "长期记忆" }).click();
  const activeMemory = page.locator(".document-drawer article", { hasText: "仅空间 A 使用简体中文" });
  await activeMemory.getByRole("button", { name: "停用" }).click();
  await expect(activeMemory).toContainText("inactive");
  await page.getByRole("button", { name: "关闭" }).click();
  await page.getByRole("button", { name: "扩展" }).click();
  await page.getByRole("button", { name: "在此空间停用" }).click();
  await expect(page.getByRole("button", { name: "在此空间启用" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "在当前会话生效" })).toBeDisabled();
});
