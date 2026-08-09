import { expect, test, vi } from "vitest";

import { RunEventStore, streamRunEvents } from "./runEventStore";

test("重连时重复序号不会重复追加回答内容", () => {
  const store = new RunEventStore();

  store.ingest({ id: 7, event: "assistant_delta", data: { content: "第一段" } });
  store.ingest({ id: 7, event: "assistant_delta", data: { content: "第一段" } });
  store.ingest({ id: 8, event: "assistant_delta", data: { content: "第二段" } });

  expect(store.getSnapshot().map((event) => event.data.content).join(""))
    .toBe("第一段第二段");
  expect(store.lastEventId).toBe(8);
});

test("事件流结束于审批请求时返回等待态", async () => {
  const store = new RunEventStore();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
    "id: 4\nevent: tool_approval_requested\ndata: {\"approval_id\":\"approval-1\"}\n\n",
    { headers: { "Content-Type": "text/event-stream" } },
  )));

  const outcome = await streamRunEvents(
    "run-1",
    "token",
    store,
    new AbortController().signal,
  );

  expect(outcome).toBe("waiting_approval");
  expect(store.getSnapshot()[0]?.event).toBe("tool_approval_requested");
});
