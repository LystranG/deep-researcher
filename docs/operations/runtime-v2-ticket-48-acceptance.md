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

- configured model, embedding, rerank, or search providers
- deployed Worker topology and restart recovery

The following external checks were run on 2026-09-01 against Docker
29.2.1 on macOS with a PostgreSQL 17 / pgvector 0.8.5 container. No
credentials were included in the command output:

- `apps/api/tests/integration/test_postgres_event_log.py -k
  'concurrent_event_writers or two_postgres_workers or
  postgres_cancelled_queue_item'`: 3 passed. This covered concurrent event
  sequence allocation, two-Worker claim competition with terminal
  idempotency, and cancellation winning before queue claim.
- A fresh PostgreSQL database migrated from the clean initial revision to
  `a4b5c6d7e8`: passed. The clean-cut migration now removes PostgreSQL
  foreign-key dependencies before dropping the legacy table while preserving
  SQLite's batch migration path.
- `apps/api/tests/integration/test_docker_sandbox.py`: 3 passed. This
  covered Docker isolation, artifact publication, daemon-unavailable
  fail-closed behavior, and environment filtering. The daemon reported
  `cgroupfs`; no rootless claim was made. Durable sandbox cancellation and
  late-result behavior remain covered only by the deterministic local tests.
- Local HTTP MCP fixture tests in
  `apps/api/tests/api/test_tool_approvals.py -k
  'local_trusted_http_mcp or cancelling_waiting_approval'`: 2 passed.
  `apps/api/tests/test_mcp_provider.py`: 3 passed. These covered the local
  MCP call path, cancellation before invocation, structured result
  validation, and late-result fencing in the deterministic adapter.

The remaining checks are recorded as `unverified`, rather than inferred from
deterministic tests: configured model/embedding/rerank/search providers,
model-backed lease/checkpoint answer recovery, and deployed Worker topology
restart recovery. The PostgreSQL lease/checkpoint test was not counted as
passed because the environment had no configured model provider and therefore
produced no final answer content. Before release, run those suites with the
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
