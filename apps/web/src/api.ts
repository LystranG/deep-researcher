export type Workspace = {
  id: string;
  name: string;
  description: string | null;
  instructions: string | null;
  archived: boolean;
  memory_auto_apply: boolean;
};

export type Conversation = {
  id: string;
  workspace_id: string;
  title: string;
  archived: boolean;
};

export type Message = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  version: number;
};

export type Attachment = {
  id: string;
  conversation_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  status: "processing" | "ready" | "failed";
  failure_reason: string | null;
};

export type Citation = {
  id: string;
  label: number;
  source_type: string;
  filename: string;
  source_url: string | null;
  source_captured_at: string | null;
  document_version: number | null;
  page_number: number | null;
  evidence_text: string;
  source_hash: string;
};

export type EvidenceCheck = {
  id: string;
  claim: string;
  verdict: string;
  reason: string;
  evidence: Citation[];
  model_version: string;
  disclaimer: string;
};

export type WorkspaceDocument = {
  id: string;
  filename: string;
  version: number;
  sha256: string;
  mime_type: string;
};

export type Memory = {
  id: string;
  workspace_id: string;
  conversation_id: string | null;
  scope: "user" | "workspace" | "conversation";
  category: string;
  risk_level: string;
  content: string;
  status: "candidate" | "active" | "conflicted" | "inactive" | "expired";
  expires_at: string | null;
  source_message_id: string | null;
  conflict: {
    id: string;
    old_memory_id: string;
    old_content: string;
    new_memory_id: string;
    new_content: string;
    status: string;
    resolution: string | null;
  } | null;
};

export type Skill = {
  slug: string;
  name: string;
  description: string;
  publisher: string;
  version: string;
  content_hash: string;
  manifest: {
    executable: boolean;
    allowed_tools: string[];
    required_capabilities: string[];
  };
  installed: boolean;
};

export type EffectiveSkill = {
  slug: string;
  name: string;
  version: string;
  manifest: Skill["manifest"];
  workspace_enabled: boolean;
  conversation_override: boolean | null;
  enabled: boolean;
};

export type McpTool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  token?: string | null,
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "请求失败" }));
    throw new ApiError(response.status, payload.detail ?? "请求失败");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
