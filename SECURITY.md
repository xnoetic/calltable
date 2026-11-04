# Security

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately
through GitHub's "Report a vulnerability" (Security → Advisories) on
`github.com/eanzhao-os/calltable`, or by email to the maintainers listed in the
repository profile. Include a reproduction and the affected version; we aim to
respond within a few days.

## Threat model

calltable is designed for a trusted network of machines you control: one hub and
several executors, usually on a private network or a tailnet. It is **not**
hardened against hostile clients or the public internet.

* **Authentication is a single shared bearer token.** There are no user accounts,
  no roles, no per-project permissions and no audit log of who did what beyond
  the `submitter` / `agent` strings stored with each job.
* **There is no TLS.** Run the hub on loopback or behind a private network /
  reverse proxy that terminates TLS if you need encryption in transit.
* **The hub binds `127.0.0.1` by default.** Only change `hub_bind` when you
  really want other machines to reach it.
* **Arbitrary command execution is the point.** Anyone with the token can queue
  any command on any executor of any host they can name. Treat the token like
  SSH access to every worker machine.
* **Artifact download accepts `?token=`** because an `<a download>` link cannot
  send an `Authorization` header. Every other endpoint requires the header, and
  dashboard log URLs redact tokens. Prefer header auth where possible.
* **Executors read `env_files` from the host.** A profile is trusted input: it
  can source local files and inject environment variables into jobs.

## Hardening checklist

1. Use a long random token (`python -c "import secrets;print(secrets.token_urlsafe(32))"`).
2. Keep the hub on loopback or a private network / tailnet; never port-forward it.
3. Run executors as an unprivileged user dedicated to build jobs.
4. Keep `data/` (SQLite database, bundle mirrors, artifacts) on a private volume.
5. Rotate the token if a machine or configuration is lost; update all clients
   and executors (`ct setup`, executor config).
6. Review profiles: `env_files` and `setup` run with the executor's privileges.
