import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(webRoot, "../..");
const runId = process.env.DEEP_RESEARCHER_E2E_RUN_ID ?? String(process.pid);
const apiPort = 18_107;
const webPort = 15_177;
const mcpPort = 18_108;

export default defineConfig({
    testDir: "./e2e",
    outputDir: "../../var/playwright-results",
    fullyParallel: false,
    retries: 0,
    reporter: "line",
    use: {
        baseURL: `http://127.0.0.1:${webPort}`,
        trace: "retain-on-failure",
    },
    projects: [
        {
            name: "chromium",
            use: { ...devices["Desktop Chrome"] },
        },
    ],
    webServer: [
        {
            command: `uv run uvicorn apps.api.tests.e2e_mcp_server:app --host 127.0.0.1 --port ${mcpPort}`,
            cwd: repoRoot,
            url: `http://127.0.0.1:${mcpPort}/healthz`,
            reuseExistingServer: false,
            timeout: 30_000,
        },
        {
            command: `env UV_CACHE_DIR=/private/tmp/deep-researcher-uv-cache DEEP_RESEARCHER_OPENAI_API_KEY= DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY= DEEP_RESEARCHER_EMBEDDED_WORKER=1 DEEP_RESEARCHER_DATABASE_URL=sqlite:////private/tmp/deep-researcher-e2e-${runId}.db DEEP_RESEARCHER_OBJECT_STORE_ROOT=/private/tmp/deep-researcher-e2e-objects-${runId} DEEP_RESEARCHER_TRUSTED_MCP_URL=http://127.0.0.1:${mcpPort}/mcp uv run uvicorn deep_researcher.app:app --host 127.0.0.1 --port ${apiPort}`,
            cwd: repoRoot,
            url: `http://127.0.0.1:${apiPort}/healthz`,
            reuseExistingServer: false,
            timeout: 30_000,
        },
        {
            command: `env DEEP_RESEARCHER_API_TARGET=http://127.0.0.1:${apiPort} npm --prefix apps/web run dev -- --host 127.0.0.1 --port ${webPort}`,
            cwd: repoRoot,
            url: `http://127.0.0.1:${webPort}`,
            reuseExistingServer: false,
            timeout: 30_000,
        },
    ],
});
