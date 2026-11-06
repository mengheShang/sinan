# ADR 0001: Single-machine Docker Compose for phase one

- Status: Accepted
- Date: 2026-11-05
- Decider: Menghe

## Context

The v1.0 design used a cloud service plus a local agent. That hybrid setup required cross-device communication, cloud token storage and a public attack surface. The deployment and security burden would have slowed down the phase-one main path: email arrives, a to-do or action is produced, and it runs after approval or a grant.

Phase one has a single user with two Google mailboxes and very little data.

## Decision

Phase one is a modular monolith on one machine. Docker Compose starts five containers:

| Container | Responsibility | External access |
| --- | --- | --- |
| Web | Next.js dashboard | None |
| API | FastAPI routes | None |
| Worker | Scheduled sync, prefilter, extraction, daily digest | Read-only token |
| Executor | Executes approved or granted actions | Write token; the only module that writes to external services |
| PostgreSQL | Data storage | None |

- All ports bind only to `127.0.0.1`.
- Scheduling uses APScheduler; no message queue.
- No LangGraph and no vector database for now.

## Consequences

Benefits:

- One command starts everything, so new users can run the demo directly.
- No public entry point, so the attack surface is small.
- Read/write separation comes from separate containers. The Executor boundary is clear and easy to review.

Costs:

- Mail does not sync while the computer is off; it catches up on the next start.
- No multi-device access.
- Separating the read and write tokens is a code-level restriction, not a hard boundary enforced by Google. The write token is still a highly sensitive credential.

## When to revisit

Revisit if any of these happen: multi-device access is needed, 24/7 sync is needed, or one machine is no longer fast enough.
