# calltable protocol, version 0.3

Status: draft for M1. Applies to `calltable` 0.3.0 and later. All additions in
0.3 are **additive**: a 0.3 hub serves 0.2 clients and executors unchanged, and a
0.3 client degrades gracefully against a 0.2 hub (see §9).

The metaphor is a restaurant queue: an agent takes a ticket, then either waits
at the door (blocking) or leaves a phone number (webhooks). This document
specifies the ticket, the waiting line, the "your table is ready" call, the
ordering receipt (queue position and ETA), idempotent ticket taking, and
ticket-holder heartbeats.

Contents

1. Roles and transport
2. Job state machine
3. Queue position and ETA
4. HTTP API (0.3 surface)
5. Waiting: long poll and SSE
6. Events and webhook payloads
7. Webhook signing and retries (Standard Webhooks)
8. Idempotent submission
9. Queue-hold heartbeat
10. Versioning and compatibility
11. Reference: existing 0.2 API

## 1. Roles and transport

* **hub** — authoritative scheduler: SQLite state, HTTP API, dashboard,
  webhook dispatcher, event log.
* **executor** — one process per host; heartbeats, claims jobs, runs them,
  reports terminal state.
* **client** — `ct`/`calltable` CLI, scripts, agents.

Transport is HTTP/1.1 with `Authorization: Bearer <token>` on every `/api/*`
request except `GET /` (dashboard HTML) and the artifact download query-token
exception inherited from 0.2. Bodies are JSON unless stated otherwise. The hub
defaults to `127.0.0.1:8091`.

The hub advertises what it supports in `GET /api/health`:

```json
{
  "ok": true,
  "version": "0.3.0",
  "capabilities": ["queue_eta", "wait_longpoll", "events_sse", "webhooks",
                    "idempotency", "queue_hold"],
  "webhooks": {"enabled": true, "signature_version": "v1"}
}
```

Clients must treat unknown capability strings as unsupported.

## 2. Job state machine

```
                create
                  |
                  v
              +--------+   claim (slot + locks free)   +---------+
              | queued | ----------------------------> | running |
              +--------+                               +---------+
                |  |                                      |  |
   cancel       |  |  queue-hold expired                  |  |  executor reports
   (client)     |  |  (no heartbeat)                      |  |  terminal status
                v  v                                      v  v
          cancelled                                   succeeded
                                                       failed
                                                       timeout
                                                       error
```

Terminal states: `succeeded`, `failed`, `cancelled`, `timeout`, `error`.
`cancelled` for a queued job uses exit code `-2`; a queued job whose queue hold
expired is also `cancelled` with exit code `-2` and an explanatory `error`.

Cancelling a `running` job sets `cancel_requested`; the executor kills the
process tree (or aborts preparation before launching it) and reports
`cancelled`. Cancel takes precedence: once `cancel_requested` is set, whatever
terminal state is written next — an executor report (`error`, `succeeded`, ...)
or the hub's own reconciliation (job missing from executor heartbeats, executor
worker changed, host heartbeat lost, provider reconciliation) — is recorded as
`cancelled`. The original status and message are kept in `error`
(`cancel requested; reported <status>: <message>`); when there is no real exit
code (none, or the `-1` that reconciliation uses) the exit code is `-2`; this
also applies when the executor itself reports `cancelled` without an exit code
(for example when it aborts during preparation). The
`job.finished` event and webhook therefore report `cancelled`. This closes the
race where a cancel lands between the claim and the executor registering the
job.
Terminal states are final; a repeated finish report (executor replay) does not
change the state and does not emit a second `job.finished` event.

### 2.1 Who may write a terminal state

`POST /api/exec/jobs/<id>/finish` never overwrites a delivered result: the
verdict and the write happen under one lock (`Store.finish_from_executor`). The
body is `status` (required, one of the terminal states), `exit_code` (integer or
`null`), `error` (string), and optionally the reporter identity `host` and
`worker`.

Every answer is machine-readable. `200` returns the job as it is now recorded,
plus `finish_accepted` and `finish_reason`:

```json
{"id": "job-...", "status": "error", "exit_code": -1, "error": "...",
 "finish_accepted": false, "finish_reason": "conflict"}
```

* `recorded` — written as reported (`finish_accepted: true`).
* `cancel_precedence` — written, but as `cancelled` because the job carries
  `cancel_requested` (see above; `finish_accepted: true`).
* `duplicate` — the job is already terminal with the same `status` and
  `exit_code` (compared after cancel precedence), e.g. a retry or a replayed
  pending file. Nothing changes: not `status`, not `finished`, not the retention
  deadline, not `error`.
* `conflict` — the job is already terminal with a different result: a late
  report from an execution nobody waits for any more. Nothing changes; the body
  still returns the recorded state, so the reporter can see what won.

Everything the hub refuses is a `4xx` carrying `finish_rejected: true` and the
same `finish_reason` vocabulary:

| status | `finish_reason` | when |
| --- | --- | --- |
| `409` | `other_owner` | the job runs on another `host`, or the reported `worker` is not the one that claimed it |
| `409` | `not_running` | the job is queued again (a provider host that never attached it was requeued, `requeue_unattached`) |
| `400` | `bad_request` | body is not a JSON object, `status` is not a terminal state, or `exit_code` is not an integer/`null` (booleans are refused) |
| `404` | `unknown` | no such job (unknown id, or purged between the check and the write) |

A rejection never writes: `status`, `exit_code`, `error` (including the reason
recorded at run time), `finished` and the cleanup deadline stay as they are. The
`finish_rejected` marker exists only on this route, so a plain `404` elsewhere is
never mistaken for "this result can be dropped". An executor clears its local
pending result only on `200` or on one of these marked refusals — anything else
(network error, `5xx`, unmarked `4xx`) keeps it for the next replay.

Ownership is checked only for clients that send identity: `host` must equal the
job's current `host`, and `worker` must equal the worker recorded when the job
was claimed (a provider's `pending:` placeholder is exempt, since the job is not
attached yet). A body with neither field is handled exactly as in 0.2 — no
ownership check — so upgrading the hub never strands a fleet of old executors.
That compatibility is not protection: a body without identity can still land
its result on a re-run of the same job and is only protected by the terminal
rules above. A restarted executor that reports for the job a previous instance
left behind must send that instance's `worker`, never its own new id.

Jobs also carry `needs_reconcile` and `purged` bookkeeping flags inherited from
0.2; they are not part of the protocol between clients and hub beyond being
present in the JSON.

## 3. Queue position and ETA

Every queued job returned by the API is enriched with:

```json
"queue": {
  "ahead": 2,
  "queued_seconds": 41,
  "targets": ["runner-a", "runner-b"],
  "group": "linux",
  "wait_seconds": 620,
  "runtime_seconds": 300,
  "starts_in_seconds": 620,
  "known": true,
  "runtime_samples": 7,
  "wait_unknown_blockers": 0
}
```

* `ahead` counts **queued jobs that compete for the same slot or the same named
  lock** and were created earlier. It is the restaurant "2 parties ahead of
  you". Jobs for other hosts/slots/locks are not counted.
* `queued_seconds` is wall time since the ticket was created.
* `targets` is the host group's member list (priority order); `group` is the
  group name or empty for a single-host job.
* `runtime_seconds` is the predicted duration of **this** job.
* `wait_seconds` is the predicted time until this job starts, and
  `starts_in_seconds = wait_seconds` (kept for clarity in receipts).
* `known` is false when either prediction is unavailable; `null` fields and
  `wait_unknown_blockers` say why. Clients must render unknown as `unknown`,
  never as zero.

### 3.1 Prediction algorithm

Estimates come from successful history of the same **estimate key**:

* key = (`profile`, `slot`) when the job has a profile. A profile name is
  `project@host` (or a `project`-wide profile), so this is the "same profile"
  rule.
* fallback key = (`project`, `slot`) when the job has no profile.

The sample set is the most recent `estimate_window` (default **20**) jobs with
`status = succeeded`, non-null `started`/`finished`, same key, ordered by
`finished` descending. Duration is `finished - started`.

* `runtime_seconds` = median of the sample durations.
* At least `estimate_min_samples` (default **3**) samples are required;
  otherwise the prediction is unknown. No mean, no defaults, no guessing.
* `wait_seconds` = sum of contributions from every **blocker** of this job:
  * each competing queued job earlier in the line contributes its own
    `runtime_seconds`;
  * each competing running job contributes `max(0, runtime_seconds - elapsed)`,
    where `runtime_seconds` is estimated from *its* estimate key and `elapsed`
    is `now - started`.
  * "competing" means sharing a target host and either the same slot or an
    overlapping lock, exactly like `ahead`.
* If any blocker's prediction is unknown, `wait_seconds` is `null` and
  `wait_unknown_blockers` counts those blockers; `known` is false.

The model deliberately ignores machine speed variation inside a profile, queue
reordering, and jobs that appear after the receipt. It is documented so agents
can decide whether to wait, detach, or pick another host. The same enrichment is
applied by `GET /api/jobs`, `GET /api/jobs/<id>`, `GET /api/state` and the
`POST /api/jobs` receipt, so CLI and dashboard always show identical numbers.

## 4. HTTP API (0.3 surface)

Only the new or extended endpoints are described here; §11 lists the 0.2
surface that remains unchanged.

### 4.1 `POST /api/jobs` (extended)

Request gains three optional members plus one header:

| field | type | meaning |
| --- | --- | --- |
| `webhooks` | array of strings | per-job notification URLs, at most 10, `http`/`https` only |
| `hold_seconds` | integer | queue hold: if > 0 the job is cancelled when no heartbeat arrives for this long while queued; `0` disables it (default) |
| `idempotency_key` | string | alternative to the `Idempotency-Key` header (§8) |
| `Idempotency-Key` | header | preferred way to send the key |
| `artifact_patterns` | array of strings | worktree-relative artifact globs from the repository declaration; replace the profile's `artifacts` for this job, echoed on the job (`null` when absent). At most 100 patterns, each at most 256 characters with at most 4 runs of `*`. See [`repo-declaration.md`](repo-declaration.md) |

Response is `201 Created` for a new job and `200 OK` for an idempotent replay,
with `X-Calltable-Idempotent-Replay: true` in the replay case. The body is the
job object, already enriched with `queue` (§3), `webhooks`, and
`hold_seconds`. `idempotency_key` is echoed on the job. If a webhook is
requested but the hub has no signing secret configured, the request fails with
`400` and an explanatory message; the hub never accepts unsigned deliveries.

### 4.2 `POST /api/jobs/<id>/heartbeat` (new)

```json
{"ok": true, "job_id": "job-...", "status": "queued", "hold_seconds": 180,
 "expires_in": 179}
```

Refreshes the queue hold for a queued job whose `hold_seconds > 0`. Unknown job
→ `404`. A terminal job returns `200` with `ok: false`; a job without a hold
returns `200` with `ok: false` and `hold_seconds: 0`. Heartbeats for running
jobs are accepted and ignored (a claimed job is not reaped by the hold).
`ct wait` sends these automatically while the job is queued.

### 4.3 `GET /api/jobs/<id>` (extended)

Query parameters:

* `wait=<seconds>` — long poll, server-capped at 60 s (`max_wait_seconds`);
  `0` returns immediately.
* `until=started|done` — condition; default `done`. `started` is satisfied when
  `started` is non-null **or** the job is terminal.

The response is the job JSON plus `X-Calltable-Wait-Result: done|timeout` and
`X-Calltable-Wait-Until: <until>`. `done` means the condition is satisfied;
`timeout` means the deadline passed first (the body is the current state).
When the concurrent-waiter limit is reached the hub returns
`503` with `Retry-After: 1`; clients retry.

### 4.4 `GET /api/events` (new, SSE)

Server-Sent Events stream.

| parameter | meaning |
| --- | --- |
| `job=<id>` | optional filter: only events for this job |
| `after=<seq>` | start after this event id (0/absent = from the current head) |

`Last-Event-ID` header takes precedence over `after` for automatic resume. Every
event has a monotonically increasing integer `id` (per hub database, survives
restarts). Frames use the named event type:

```
id: 42
event: job.finished
data: {"type":"job.finished","event_id":42,...}

```

The stream also sends `: keepalive` comment lines (default every 15 s) and one
`retry: 1000` line at the start. The hub caps concurrent streams (default 8) and
answers `503` beyond that. The event log is bounded; the hub prunes it to the
most recent N events (default 10000), so `Last-Event-ID` resume is best-effort
and a client asking for a pruned id simply receives the oldest retained events.

### 4.5 Job detail webhook status

`GET /api/jobs/<id>` additionally returns `webhook_deliveries`, grouped per URL:

```json
"webhook_deliveries": [
  {"url": "https://example.com/hook", "events": [
    {"event": "job.started", "status": "delivered", "attempts": 1,
     "last_status": 204, "last_error": "", "updated": 1762300000.0},
    {"event": "job.finished", "status": "pending", "attempts": 0,
     "last_status": null, "last_error": "", "updated": 0.0}
  ]}
]
```

`status` is `pending`, `delivered`, or `failed`. Delivery state is persisted, so
hub restarts resume pending deliveries.

## 5. Waiting: long poll and SSE

Rules for clients:

1. `ct wait <id>` first waits for `started` (so it can print progress) and then
   for `done`, each time with `wait` up to 60 s. It never sleeps in a fixed
   short poll loop.
2. `ct run --wait` uses the same long poll, with a smaller wait chunk (5 s) so
   log output still refreshes; it is not a `sleep`-based poll loop.
3. If the hub does not return `X-Calltable-Wait-Result`, it is a 0.2 hub that
   ignored `wait`; the client falls back to polling with a **2 s** interval
   (documented compatibility path) and keeps working.
4. SSE is optional for clients; agents should prefer `ct wait` because it is one
   blocking process, not a shell loop. SSE exists for dashboards and
   integrations.

Exit codes of `ct wait`:

| situation | exit code |
| --- | --- |
| job reached the requested condition | job's own code (`0` success, otherwise its exit code) |
| `--until started` and the job started | `0` |
| job cancelled (before or after start) | `130` |
| job timed out | `124` |
| local `--timeout` expired first | `75` |
| hub/API error | `1` |
| bad usage / unknown job | `2` |

## 6. Events and webhook payloads

Event types: `job.queued`, `job.started`, `job.requeued`, `job.finished`.
Webhooks are sent for `job.started` and `job.finished` (that is "your table is
ready" and "the meal is over"). `job.requeued` means a reserved job went back
to `queued` before any executor attached (a Cloud Run overflow that failed to
start); it is sent as a webhook only to URLs that already received
`job.started`, and that job's `job.started` is delivered again when it really
starts. Consumers should ignore event types they do not know. Both the SSE
`data` and the webhook body use:

```json
{
  "type": "job.finished",
  "event_id": 42,
  "created_at": "2026-11-04T12:00:00Z",
  "dashboard_url": "https://hub.example.internal:8091/#/job/job-20261104120000-ab12cd",
  "duration_seconds": 301.2,
  "job": {
    "id": "job-20261104120000-ab12cd",
    "project": "demo",
    "host": "runner-a",
    "slot": "heavy",
    "status": "succeeded",
    "exit_code": 0,
    "created": 1762300000.0,
    "started": 1762300010.0,
    "finished": 1762300311.2,
    "profile": "demo@runner-a",
    "group_name": "linux",
    "label": "full gate",
    "submitter": "devbox/me/demo@feat",
    "agent": "claude-code",
    "sha": "abc123...",
    "error": "",
    "cmd": ["bash", "gate.sh"]
  }
}
```

`job` is the same job object as the API (queue enrichment aside), captured at
event time; retries replay the same payload. `dashboard_url` is included only
when the hub has a `public_url`/`hub_url` configured; it never contains a token.

## 7. Webhook signing and retries (Standard Webhooks)

The hub follows <https://www.standardwebhooks.com/>:

| header | value |
| --- | --- |
| `webhook-id` | stable message id, `msg_<hex>`; identical across retries |
| `webhook-timestamp` | Unix seconds at the moment of the attempt |
| `webhook-signature` | `v1,<base64(HMAC-SHA256(key, "{id}.{timestamp}.{body}"))>` |
| `content-type` | `application/json` |
| `user-agent` | `calltable/<version>` |

The key is the hub's `webhook_secret` configuration value. A `whsec_` prefix is
stripped and the remainder is base64-decoded (Standard Webhooks convention);
otherwise the secret is used as UTF-8 bytes. Receivers must reject timestamps
older than their tolerance (5 minutes is typical), compare in constant time,
and de-duplicate on `webhook-id`.

Retry semantics: delivery happens on a background thread, never in the
scheduler path. `2xx` is success. HTTP `408`, `429`, any `5xx`, and network
errors are retried with exponential backoff `base * 2^(attempt-1)` capped at
`max_delay` (defaults: base 2 s, cap 300 s, 6 attempts). Every other `4xx` means
the request itself is wrong (bad URL, revoked endpoint, signature rejected):
the delivery is marked `failed` immediately with the HTTP status recorded and
is **not** retried. Each attempt is persisted (`attempts`, `next_attempt`,
`last_error`) before it happens; a hub restart resumes pending deliveries.
After the last attempt the delivery is marked `failed` and stays visible in
`ct show`. A failed webhook never affects job state.

A minimal receiver (verification included) lives in
`integrations/verify_webhook.py`, and a forwarder to Slack/Feishu incoming
webhooks lives in `integrations/forward_to_im.py`.

## 8. Idempotent submission

`ct run --idempotency-key <key>` (or any HTTP client sending `Idempotency-Key`)
makes ticket taking safe to retry.

* Key format: 1–200 characters of `[A-Za-z0-9._:-]`.
* Window: `idempotency_window_seconds`, default **86400** (24 h); expired keys
  are pruned.
* The hub computes a fingerprint over the submission parameters:
  `project`, `host`, `cmd`, `base_ref`, `dirty`, resolved `slot`, resolved
  `locks`, resolved `timeout`, `label`, and a **code identity**:
  * clean worktree: the commit `sha` (`commit:<sha>`);
  * dirty worktree: the snapshot's **tree hash**, sent by the client as
    `tree_sha` (`tree:<tree_sha>`). A tree hash depends only on content, not on
    the snapshot commit's timestamp, so retrying unchanged content reuses the
    ticket while any content change produces a different identity and `409`.
  * If a third-party client sends `dirty: true` without `tree_sha`, the hub
    falls back to the commit sha; such clients should send `tree_sha` to get
    stable retries.
  `base_sha`, `base_ref_sha`, and `bundle_id` are excluded because `ct run`
  creates a fresh snapshot commit and re-uploads the bundle on every attempt.
  `submitter`, `agent`, and `webhooks` are excluded because the transport
  identity may differ. Within one idempotency key, changing the code is a
  different submission and is rejected with `409`; use a new key for a new
  verification.
* Same key + same fingerprint inside the window → the original job is returned
  with `200` and `X-Calltable-Idempotent-Replay: true`; no new job, no new
  queue position, no new webhook.
* Same key + different fingerprint → `409 Conflict` with a message naming the
  conflicting fields.
* New key → a fresh job. The check and the insert happen in one SQLite
  `BEGIN IMMEDIATE` transaction, so concurrent duplicates cannot both win.
* `artifact_patterns` is part of the fingerprint when present. A hub that
  predates it ignored the member and stored a fingerprint without it, so after
  such a hub is upgraded, a retry (same key) of a submission that declared
  artifacts is answered with `409` rather than as a replay; see
  [`repo-declaration.md`](repo-declaration.md) §5.

## 9. Versioning and compatibility

* `calltable` package version is `0.3.0`.
* All 0.3 changes are additive fields, endpoints, query parameters, and tables.
  A 0.2 executor keeps claiming and finishing jobs; a 0.2 client keeps
  submitting and reading jobs.
* SQLite schema changes are applied automatically at startup (`ALTER TABLE ...
  ADD COLUMN` for existing tables, `CREATE TABLE IF NOT EXISTS` for new ones).
  Existing rows receive defaults; no data is rewritten or dropped.
* 0.3 client against a 0.2 hub:
  * `wait`: no `X-Calltable-Wait-Result` header → fall back to 2 s polling.
  * `queue`: absent → print queue position from `ahead`/`queued_seconds` if
    present, otherwise `unknown` for ETA.
  * `webhooks`, `hold_seconds`, `idempotency_key`: not echoed by the hub →
    print a warning and continue (the old hub ignores the fields).
  * `artifact_patterns` (repository declaration): not echoed → the client
    warns that the declared artifacts are not collected.
* 0.2 client against a 0.3 hub: extra response fields are ignored by the old
  client; `slot`/`locks`/`timeout` handling is unchanged.

## 10. Queue-hold heartbeat

`ct run --hold-while-alive` sets `hold_seconds` (default 180, override with
`--hold-timeout`). While the job is queued the hub cancels it when
`now - COALESCE(last_hold, created) > hold_seconds`, recording
`error = "queue hold expired: ..."`, exit code `-2`, and emitting
`job.finished`. `ct wait` heartbeats automatically (at most every 60 s, which is
below the default hold); a detached agent that never runs `ct wait` therefore
does not block the queue forever. The feature is off by default and does not
affect running jobs.

### Provider hosts: `POST /api/exec/jobs/<id>/attach` (Cloud Run)

Hosts with `provider: "cloudrun"` have no long-running executor. The hub
reserves a queued job for such a host (status `running`, `worker` =
`pending:<random>`), then starts a one-shot executor with the job id. That
executor sends `{"host": "<host>", "worker": "<worker id>"}` to
`POST /api/exec/jobs/<id>/attach` and receives the same payload as
`/api/exec/claim`. The call returns `409` unless the job is running on that
host and is still reserved (or already attached by the same worker).
`POST /api/exec/claim` returns `204` for provider hosts. Heartbeats from a
provider host only reconcile jobs owned by the reporting worker, because
several executions share one host name. `GET /api/cloudrun` reports each
provider host's month-to-date spend estimate and active executions. Health
advertises the `exec_attach` and `cloudrun` capabilities.

## 11. Reference: existing 0.2 API

Unchanged endpoints (still supported):

* `GET /api/health`, `GET /`, `GET /api/state`, `GET /api/hosts`,
  `GET /api/groups`, `GET /api/jobs`, `GET /api/jobs/<id>[/logs|/samples|
  /artifacts|/bundle]`, `GET /api/projects/<p>/commits`
* `POST /api/hosts/heartbeat`, `POST /api/exec/claim`,
  `POST /api/exec/jobs/<id>/finish`, `POST /api/hosts/<h>/drain`,
  `POST /api/jobs`, `POST /api/jobs/<id>/logs`, `POST /api/jobs/<id>/samples`,
  `POST /api/jobs/<id>/artifacts`, `POST /api/jobs/<id>/cancel`,
  `POST /api/projects/<p>/{have,bundle,bundle/part,bundle/finalize}`

Lock, slot, group, artifact, bundle, drain, and heartbeat-reconciliation
semantics are as specified by the 0.2 implementation and its tests; 0.3 does not
change them.
