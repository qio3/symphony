# Blocked Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve eligible Zavod Project `Blocked` questions with one bounded, read-only GPT-6 Astra review and return only resolved Issues to `Ready for AI`.

**Architecture:** Owner Control remains the durable authority for blocker versions, claims, idempotent comments and Project transitions. The existing Elixir Orchestrator reserves one ordinary worker slot, launches a specialized read-only `BlockedReview` turn through the existing Codex app-server, and never gives the reviewer a delivery lease or write-capable tools. A completed model result is retried only at the Owner Control apply boundary.

**Tech Stack:** Elixir/OTP TaskSupervisor and Codex app-server, Python Owner Control and atomic JSON StateStore, GitHub Projects v2.

**Spec:** https://github.com/qio3/zavod/issues/892

## Global Constraints

- Exactly one concurrent blocked review; it consumes the existing worker limit and runs only when no ordinary `Ready for AI` work is waiting.
- Model is exactly `gpt-6-astra`; one turn, 600-second timeout, read-only sandbox, no dynamic tools.
- Review never edits code, creates a PR, deploys, changes production, or acquires the `symphony` delivery lease.
- The controller, not the model, owns comments, labels and Project status mutations.
- One completed review per semantic blocker version; self-comments and timestamps do not change the version.
- Explicit owner decisions, missing access, spending, production, secrets and irreversible actions remain outside delegated authority.

---

### Task 1: Durable blocker contract in Owner Control

**Files:**
- Modify: `control/owner_control/clients.py`
- Modify: `control/owner_control/snapshot.py`
- Modify: `control/owner_control/state_store.py`
- Modify: `control/owner_control/actions.py`
- Modify: `control/owner_control/http_server.py`
- Test: `control/tests/test_owner_control.py`
- Test: `control/tests/test_state_store.py`

**Interfaces:**
- Produces: `StateStore.claim_blocked_review(issue, version, claimed_at) -> bool` and `StateStore.complete_blocked_review(issue, version, result, completed_at) -> None`.
- Produces: internal actions `claim_blocked_review` and `apply_blocked_review`.
- Produces: each blocked snapshot item contains `body`, bounded `comments`, `blocker_version`, and `blocked_review`.

- [ ] Write failing tests proving stable semantic versions, durable claim/result deduplication, resolved/unresolved transitions, stale-result rejection and idempotent comment/status recovery.
- [ ] Run `python -m unittest control.tests.test_owner_control control.tests.test_state_store` and require failures for missing methods/actions.
- [ ] Add body/comment author data to the existing Project query and compute SHA-256 from relevant body, owner question, human comments, labels and linked PR state while excluding the delegated-review marker.
- [ ] Add atomic StateStore methods and expose stored review state in `SnapshotBuilder.build(..., blocked_reviews=...)`.
- [ ] Add internal claim/apply actions. Claim requires fresh GitHub, open `Blocked`, matching version, no delivery lease and no completed same-version result. Apply revalidates all those facts, persists the structured result, uses `comment_once`, removes `ждёт-владельца` only for `resolved`, and then sets `Ready for AI`.
- [ ] Run the focused Python tests and commit the durable contract.

### Task 2: Read-only Astra reviewer

**Files:**
- Create: `elixir/lib/symphony_elixir/blocked_review.ex`
- Modify: `elixir/lib/symphony_elixir/owner_control/client.ex`
- Test: `elixir/test/symphony_elixir/blocked_review_test.exs`
- Test: `elixir/test/symphony_elixir/owner_control_test.exs`

**Interfaces:**
- Produces: `BlockedReview.run(issue, context, on_message: callback) -> {:ok, result} | {:error, reason}`.
- Result is `%{outcome: "resolved" | "unresolved", decision: String.t(), evidence: [String.t()], assumptions: [String.t()], next_step: String.t(), question: String.t() | nil}`.
- Produces: `OwnerControl.Client.claim_blocked_review/2` and `apply_blocked_review/3`.

- [ ] Write failing parser/prompt tests for an allowed reversible choice, an explicit owner prohibition and missing access.
- [ ] Run `mix test test/symphony_elixir/blocked_review_test.exs test/symphony_elixir/owner_control_test.exs` and require the missing-module failure.
- [ ] Implement a strict output schema and concise prompt that states delegated boundaries. Start Codex with `model: "gpt-6-astra"`, `approval_policy: "never"`, `thread_sandbox: "read-only"`, `%{"type" => "readOnly", "networkAccess" => false}`, `dynamic_tools: false`, and `turn_timeout_ms: 600_000`.
- [ ] Collect structured agent output with the same tested message extraction pattern used by `MetadataClassifier`; convert model/timeout/rate-limit failures to an `unresolved` result without fallback model.
- [ ] Add typed Owner Control client calls, run focused tests and commit.

### Task 3: Scheduler integration and apply-only recovery

**Files:**
- Modify: `elixir/lib/symphony_elixir/orchestrator.ex`
- Test: `elixir/test/symphony_elixir/owner_control_test.exs`
- Test: `elixir/test/symphony_elixir/orchestrator_status_test.exs`

**Interfaces:**
- Consumes: Task 1 claim/apply actions and Task 2 `BlockedReview.run/3`.
- Produces: running entries with `mode: :blocked_review`; apply retry metadata carries `blocked_review_version` and `blocked_review_result` and never calls the model twice.

- [ ] Write failing lifecycle tests: one legacy Blocked candidate is claimed; ordinary ready work wins; pause/full capacity/lease suppress review; one active review suppresses a second; changed status/version discards late output; apply failure retries only the saved result.
- [ ] Run the focused tests and confirm failures before implementation.
- [ ] Extend the fresh Owner Control dispatch context with eligible blocked items, then schedule at most one review after ordinary issue selection and only when no ready issue is waiting.
- [ ] Use a versioned `GH-<number>-blocked-<hash>` workspace, run existing after-create/before-run setup, and remove only that dedicated clean review workspace after completion.
- [ ] Add dedicated DOWN handling and bounded apply-only retry. Never route review completion through normal continuation, failure fingerprint escalation or delivery retry.
- [ ] Expose mode/outcome/reason/model/usage through existing running and diagnostic projections, run focused tests and commit.

### Task 4: Policy acknowledgement and complete verification

**Files:**
- Modify in separate Zavod worktree: `AGENTS.md`
- Modify in separate Zavod worktree: `WORKFLOW.md`
- Test in Symphony worktree: all control and Elixir checks

**Interfaces:**
- Consumes: delegated decision comment marker emitted by Task 1.
- Produces: ordinary delivery workers accept a current delegated Astra resolution unless superseded by a later explicit owner decision.

- [ ] Add the smallest policy paragraph: the marked Astra result is authorization only inside #892 delegated boundaries; it never overrides a later owner decision or authorizes access, cost, production, secrets or irreversible actions.
- [ ] Add/adjust prompt contract tests so a resolved marker continues delivery and an explicit owner prohibition remains Blocked.
- [ ] Run `python -m unittest discover -s control/tests`, `mix format --check-formatted`, focused Elixir tests, `mix specs.check`, and fork `make-all` CI.
- [ ] Open one runtime PR and one Zavod policy PR, both `Refs #892`; land through each repository's required flow.
- [ ] On a drained live runtime, update the single release SHA, rebuild/restart once, require healthy Owner Control and Symphony, and run one controlled read-only Astra smoke without mutating a real Blocked Issue.
