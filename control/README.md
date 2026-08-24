# Symphony Owner Control

`owner_control` is a small host-side control process. It is not a worker orchestrator: it reads the
existing Symphony observability API, GitHub Project/Issue/PR/CI data, the canonical Git ref, and the
TEST health endpoint, then exposes one deterministic owner snapshot and a fixed action allowlist.

It runs separately so Telegram and `restart` remain available when the Symphony process is down.
The implementation uses only the Python standard library.

## Endpoints

Machine endpoints require `Authorization: Bearer <SYMPHONY_CONTROL_TOKEN>`. The localhost-only web
UI uses a per-process same-origin CSRF token and never exposes the bearer token to the browser.

- `GET /v1/snapshot`
- `GET /v1/intake`
- `GET /v1/logs?tail=100`
- `POST /v1/actions/run` with `{"issue": 401}`
- `POST /v1/actions/lease` with `{"issue": 401}` (runtime-only preflight; requires active intake
  and a fresh open `Ready for AI` Issue)
- `POST /v1/actions/accept` with `{"issue": 401}`
- `POST /v1/actions/rework` with `{"issue": 401, "reason": "short reason"}`
- `POST /v1/actions/pause`
- `POST /v1/actions/resume`
- `POST /v1/actions/start_service`
- `POST /v1/actions/stop_service` with `{"confirm_running_workers": 2}` when workers are active
- `POST /v1/actions/restart`

There is no generic command, shell, Docker, or GitHub proxy endpoint. Service actions address only
the fixed Compose file and fixed Symphony service from local configuration.

## Local configuration

All values are supplied at process start. Tokens, Telegram IDs, state, offsets, and notification
watermarks must live outside the repository.

Required environment variables:

```text
SYMPHONY_CONTROL_TOKEN
SYMPHONY_CONTROL_STATE_PATH
SYMPHONY_COMPOSE_FILE
SYMPHONY_URL
SYMPHONY_CONTAINER
SYMPHONY_COMPOSE_SERVICE
SYMPHONY_WORKER_LIMIT
GITHUB_TOKEN (or SYMPHONY_GITHUB_TOKEN)
GITHUB_REPOSITORY
GITHUB_PROJECT_ID
GITHUB_CANONICAL_REF
TEST_HEALTH_URL
TEST_URL
TELEGRAM_BOT_TOKEN
TELEGRAM_OWNER_CHAT_ID
TELEGRAM_OWNER_USER_ID
CODEX_HOME
```

Optional environment variables:

```text
SYMPHONY_CONTROL_BIND=127.0.0.1
SYMPHONY_CONTROL_PORT=4080
CODEX_EXECUTABLE=codex
```

Non-loopback binding is rejected unless `SYMPHONY_CONTROL_ALLOW_NON_LOOPBACK=1` is explicit. If the
Symphony container later needs a Docker-host binding, restrict that interface with the host firewall
and keep bearer authentication enabled.

Start from the repository root:

```powershell
$env:PYTHONPATH = "control"
python -m owner_control
```

The Symphony runtime uses `SYMPHONY_OWNER_CONTROL_URL` and `SYMPHONY_OWNER_CONTROL_TOKEN` for the
authoritative intake gate, one fresh Ready-for-AI projection per dispatch cycle, and the fixed
`lease` action. A lease only adds the durable `symphony` label; it never promotes Backlog work.
When configured control or its GitHub source is unavailable, new unleased dispatch fails closed
while already-running workers continue. The native Phoenix page remains a runtime diagnostics
surface.

## Telegram and AI boundary

Both the Telegram chat ID and sender user ID must match the allowlist. Slash commands call the same
typed actions as the dashboard. Plain text is sent to an ephemeral Codex `exec` invocation with an
empty read-only workspace, ignored user/rule configuration, a minimal environment, and no control
tools. Authentication comes from `CODEX_HOME`; `OPENAI_API_KEY` is removed from the child environment.

Notification fingerprints are persisted in `SYMPHONY_CONTROL_STATE_PATH`. Pushes are limited to new
owner blockers, Ready for Acceptance, systemic failures, and unexpected service stops.

## Tests

```powershell
$env:PYTHONPATH = "control"
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m unittest discover -s control/tests -v
```
