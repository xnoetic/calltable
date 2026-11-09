# calltable discipline for AI coding agents

Copy this block into `AGENTS.md` / `CLAUDE.md` (or the equivalent) in a
repository that uses calltable. It is the short version of
`spec/calltable-protocol.md`.

```markdown
## Verification tasks: use calltable, never poll

The verification queue is a restaurant. You take a ticket, you wait, you get
called. Do not stand at the counter asking "is it ready?" in a loop.

1. Submit with a receipt and detach:
   `ct run --detach --project <p> --host <group-or-host> --label "<what>" -- <cmd...>`
   The receipt prints the job id, queue position, and (when enough history
   exists) the estimated wait and run time. Record the job id.
2. Then block once, in the background:
   `ct wait <job-id> --until done --timeout 2h`
   Its exit code is the job's exit code (0 ok, otherwise the task's code;
   130 = cancelled, 124 = job timeout, 75 = local wait expired). When the
   background command finishes it wakes the agent, so you can go do other
   work instead of sleeping.
3. If you only need to know the job started (e.g. to tail logs yourself):
   `ct wait <job-id> --until started`.
4. Never write `while true; do ct show ...; sleep ...; done`. `ct wait` uses a
   server-side long poll; a shell polling loop burns tokens, hammers the hub,
   and violates the queue's etiquette.
5. Do not run the same verification twice in parallel. If a submission may be
   retried (network error, agent restart), pass a stable
   `--idempotency-key <repo>-<branch>-<sha>-<purpose>` so the retry reuses the
   same ticket instead of taking a second place in line.
6. If the ticket should not survive an abandoned agent session, add
   `--hold-while-alive`: the hub cancels the ticket when no heartbeat arrives
   while it is queued (running jobs are unaffected). `ct wait` heartbeats
   automatically.
7. Prefer leaving a phone number for long runs:
   `ct run --detach --notify <webhook-url> ...` sends signed
   `job.started` / `job.finished` events, so you can stop waiting entirely.
8. Before reporting a failure, read the verdict, not just the exit code:
   `ct show <job-id>` (status, exit code, duration, `error`, artifacts,
   webhook delivery status). Quote the failed command and the last log lines.
```

Related docs:

* `spec/calltable-protocol.md` — state machine, HTTP API, events, webhooks,
  idempotency, ETA algorithm.
* `integrations/verify_webhook.py` — minimal signed receiver.
* `integrations/forward_to_im.py` — relay to Slack / Feishu.
