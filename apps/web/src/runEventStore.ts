import { useSyncExternalStore } from "react";

export type RunEvent = {
  id: number;
  event: string;
  data: Record<string, unknown>;
};

export type RunStreamOutcome = "terminal" | "waiting_approval";

type Listener = () => void;

export class RunEventStore {
  private snapshot: RunEvent[] = [];
  private listeners = new Set<Listener>();
  lastEventId = 0;

  ingest(event: RunEvent) {
    if (event.id <= this.lastEventId) return;
    this.lastEventId = event.id;
    this.snapshot = [...this.snapshot, event];
    this.listeners.forEach((listener) => listener());
  }

  getSnapshot = () => this.snapshot;
  getServerSnapshot = () => [] as RunEvent[];

  subscribe = (listener: Listener) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

export function useRunEvents(store: RunEventStore | null): RunEvent[] {
  const emptyStore = EMPTY_STORE;
  const target = store ?? emptyStore;
  return useSyncExternalStore(target.subscribe, target.getSnapshot, target.getServerSnapshot);
}

const EMPTY_STORE = new RunEventStore();

function parseBlock(block: string): RunEvent | null {
  let id: number | null = null;
  let event = "message";
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("id:")) id = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (id === null || !Number.isFinite(id)) return null;
  try {
    return { id, event, data: JSON.parse(data.join("\n")) as Record<string, unknown> };
  } catch {
    return { id, event, data: { raw: data.join("\n") } };
  }
}

export async function streamRunEvents(
  runId: string,
  token: string,
  store: RunEventStore,
  signal: AbortSignal,
): Promise<RunStreamOutcome> {
  let terminal = false;
  let waitingApproval = false;
  while (!terminal && !signal.aborted) {
    const response = await fetch(`/api/v1/runs/${runId}/events?after=${store.lastEventId}`, {
      headers: {
        Accept: "text/event-stream",
        Authorization: `Bearer ${token}`,
      },
      signal,
    });
    if (!response.ok || !response.body) throw new Error("研究事件流连接失败");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = buffer.search(/\r?\n\r?\n/);
      while (boundary >= 0) {
        const separator = buffer.slice(boundary).match(/^\r?\n\r?\n/)?.[0] ?? "\n\n";
        const parsed = parseBlock(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + separator.length);
        if (parsed) {
          store.ingest(parsed);
          if (parsed.event === "tool_approval_requested") waitingApproval = true;
          if (["tool_call_completed", "tool_call_rejected"].includes(parsed.event)) {
            waitingApproval = false;
          }
          terminal = ["run_completed", "run_cancelled", "run_failed"].includes(parsed.event);
        }
        boundary = buffer.search(/\r?\n\r?\n/);
      }
      if (done) break;
    }
    if (waitingApproval && !terminal && !signal.aborted) return "waiting_approval";
    if (!terminal && !signal.aborted) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
  }
  return "terminal";
}
