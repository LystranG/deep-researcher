# Runtime v2 Ticket #48 Acceptance

This document records the verification boundary for the Runtime v2 clean-cut
on the `main` branch. It is an acceptance record, not a second schema or
runtime contract.

## Verified Locally

The following checks pass in the repository's local SQLite and deterministic
test environment:

- `make verify`
- API tests, including research runs, research records, Worker behavior,
  durable SandboxJob API behavior, and web search/source citation behavior
- web unit tests, typecheck, and production build
- Ruff, mypy, and `git diff --check`
- Alembic migrations from the clean initial schema through the durable
  SandboxJob clean-cut and input-provenance revisions

The production boundary is `RunWorker -> ResearchCoordinator.execute_runtime_v2`.
Python execution is submitted as a durable `SandboxJob` and consumed by the
`SandboxJobWorker`; API-owned `SandboxExecution` persistence and projection
paths are not part of this boundary. Sandbox provenance is stored on
`SandboxJob` and downstream facts reference `sandbox_job_id`.

The deterministic local tests also cover the observable Plan, task execution,
verification, writing, citation, terminal event, and SSE sequence, together
with sandbox artifact publication and cancellation cases. Tests that exercise
takeover, fencing, checkpoint recovery, and late-result rejection remain
bounded and use explicit time limits.

## External Verification Boundary

These checks require services or credentials that are not part of the default
local test environment and were not run as part of this acceptance:

- PostgreSQL queue and checkpoint takeover with two live Workers
- Docker sandbox execution and container cancellation against a deployed
  runtime
- local MCP service cancellation and late-result behavior
- configured model, embedding, rerank, or search providers
- deployed Worker topology and restart recovery

They are recorded as `unverified`, rather than inferred from deterministic
SQLite tests. Before release, run the relevant integration suites with the
deployment configuration and attach their results to issue #48.

## Recovery Contract

Durable facts, event idempotency keys, SandboxJob attempt state, and Worker
claim fencing are the sources used to resume work. A restarted Worker must
re-read the durable run/job state and use the stable waiting reference; it
must not recreate an API-owned execution projection. A stale run epoch or late
external result must be rejected before publishing a business fact or
terminal event. Cancellation is terminal for the run and must prevent a late
SandboxJob result from being published.

The local tests establish these contracts for the deterministic adapters and
document the boundaries above. Provider, PostgreSQL, Docker, MCP, and deployed
topology behavior still requires the external verification listed above.
