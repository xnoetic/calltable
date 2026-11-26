# calltable client config and machine-readable views

schema_version: 1

Status: stable contract. Applies to `calltable` 0.3.0 and later. Other tools
(project scripts, agent skills) that need to know whether a machine can use
calltable, which hub it talks to, and which verification machines are
registered locally read **only** the two commands described here. They never
parse the config file (it holds the token) and never parse the human
`MODE=... READY` line.

Contents

1. Versioning
2. Local config file
3. `ct config show --json`
4. `ct doctor --json`
5. Secrets

## 1. Versioning

Both outputs carry `schema_version` (an integer, currently `1`) and `kind`.

* Adding a field is **not** a breaking change and does not bump the version.
  Readers must ignore fields they do not know.
* Renaming or removing a field, changing its type, or changing its meaning
  bumps `schema_version`. Readers should refuse a `schema_version` greater than
  the one they were written for.
* Every field listed in sections 3 and 4 is always present (its value may be
  `null` where stated).

## 2. Local config file

### Location

The client config is one JSON object. It is looked up in this fixed order; the
first one that exists is used:

1. `--config <path>` (source `flag`; a missing file is an error)
2. the `CALLTABLE_CONFIG` environment variable (source `env`; a missing file is
   an error)
3. `~/.calltable/config.json` (source `home`; `%USERPROFILE%\.calltable\config.json`
   on Windows)
4. `~/.config/calltable/config.json` (source `legacy`; the location used before
   this contract, still supported)

When both user-level files exist, `~/.calltable/config.json` wins and the other
file is ignored; it is listed in `config.shadowed` of both outputs, and
`ct doctor` names it on stderr.

`ct setup` without `--config` writes `~/.calltable/config.json` on a new
machine. On a machine that only has `~/.config/calltable/config.json` it updates
that file in place, so an existing install keeps working unchanged. The file is
written owner-only: mode `0600` in a `0700` directory on POSIX; on Windows
inherited ACEs are removed and only the current user is granted access.

### Client fields

The same file format is shared with the hub and executor configs (see
`config.example.json` and the README settings reference); the fields a client
uses are:

| field | type | meaning |
| --- | --- | --- |
| `hub_url` | string | hub base URL; default `http://127.0.0.1:8091` |
| `token` | string | bearer token; `CALLTABLE_TOKEN` overrides it. Never printed by the commands below |
| `owner_tailnet` | string | optional; enables the tailscale branch of `ct doctor` |
| `machines` | object | optional; verification machines registered on this computer, keyed by name |
| `machines.<name>.ssh` | string | optional; an ssh alias (one word, no spaces), resolved by the user's own ssh config |
| `machines.<name>.platform` | string | optional; `linux`, `windows` or `macos` (case-insensitive, reported lower-case) |
| `machines.<name>.purpose` | string | optional; free-text note on what the machine is for |

`machines` is not used for scheduling; calltable only validates it and reports
it, so that this information is kept in one place instead of in each project's
documentation. A machine name matches `[A-Za-z0-9][A-Za-z0-9._-]*`. A machine
entry may be `null` or `{}`. Unknown keys inside an entry, non-string values, an
ssh value containing whitespace, or an unknown platform make the config invalid
(every `ct` command then fails with a config error naming the entry). Do not put
addresses, ports, user names or key paths here: keep those in `~/.ssh/config`
and register only the alias.

Example:

```json
{
  "hub_url": "http://hub.example.internal:8091",
  "token": "…",
  "machines": {
    "runner-a": {"ssh": "runner-a", "platform": "linux", "purpose": "Linux builds"},
    "runner-b": {"ssh": "runner-b", "platform": "windows", "purpose": "UI tests"}
  }
}
```

## 3. `ct config show --json`

Prints one JSON object on stdout. Exit code `0` when a config file was found and
is valid; `2` when there is no config file or it is invalid (the object is
still printed, with `config.error` set). It does not contact the hub.

| field | type | meaning |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `kind` | string | `"calltable.config"` |
| `client_version` | string | version of the `ct` that produced the output |
| `config` | object | which config file is in use |
| `config.path` | string or null | absolute path of the file in use; `null` when none was found |
| `config.source` | string or null | `flag`, `env`, `home` or `legacy` (section 2); `null` when none was found |
| `config.shadowed` | array of strings | user-level files that exist but were ignored because a higher-priority one won |
| `config.search_order` | array of objects | every location checked, in lookup order (`flag` and `env` only when given) |
| `config.search_order[].source` | string | `flag`, `env`, `home` or `legacy` |
| `config.search_order[].path` | string | path checked |
| `config.search_order[].exists` | boolean | whether that file exists |
| `config.error` | string or null | why no usable config was loaded; `null` when it loaded |
| `hub_url` | string or null | configured hub URL with any `user:password@`, query and fragment removed; `null` without a config |
| `token_present` | boolean | whether a token is available (config or `CALLTABLE_TOKEN`) |
| `token_source` | string | `env`, `config` or `none` (`env` wins, as in every command) |
| `owner_tailnet` | string | configured tailnet name, `""` when not set |
| `machines` | array of objects | registered machines in config order; `[]` when none |
| `machines[].name` | string | machine name |
| `machines[].ssh` | string | ssh alias, `""` when not set |
| `machines[].platform` | string | `linux`, `windows`, `macos` or `""` |
| `machines[].purpose` | string | note, `""` when not set |

`ct config show` without `--json` prints the same information for humans.

## 4. `ct doctor --json`

Runs the same checks as `ct doctor` and prints one JSON object on stdout instead
of the `MODE=...` line (no stderr detail lines). The exit code is unchanged:
`0` ready, `3` not ready. A missing or invalid config does not stop the checks;
it is reported in `config.error` and the defaults are used, exactly as in
`ct doctor`.

| field | type | meaning |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `kind` | string | `"calltable.doctor"` |
| `client_version` | string | version of the `ct` that produced the output |
| `mode` | string | `tailscale` or `direct` |
| `ready` | boolean | `true` exactly when `exit_code` is 0 |
| `exit_code` | integer | the process exit code (`0` or `3`) |
| `line` | string | the `MODE=...` line `ct doctor` would print |
| `reasons` | array of strings | not-ready reasons, each with a suggested fix; `[]` when ready |
| `config` | object | same object as in section 3 |
| `config.path` | string or null | as in section 3 |
| `config.source` | string or null | as in section 3 |
| `config.shadowed` | array of strings | as in section 3 |
| `config.search_order` | array of objects | as in section 3 |
| `config.search_order[].source` | string | as in section 3 |
| `config.search_order[].path` | string | as in section 3 |
| `config.search_order[].exists` | boolean | as in section 3 |
| `config.error` | string or null | as in section 3 |
| `token_present` | boolean | whether a token was used for the checks |
| `token_source` | string | `flag` (`--token`), `env`, `config` or `none` |
| `hub` | object | hub reachability |
| `hub.url` | string | hub URL checked, credentials removed as in section 3 |
| `hub.reachable` | boolean | `GET /api/health` answered 200 |
| `hub.auth_ok` | boolean | an authenticated request succeeded |
| `hub.version` | string | hub version, `""` when unknown |
| `hub.detail` | string | ASCII error detail, `""` when none |
| `checks` | object | local prerequisites |
| `checks.python` | object | Python interpreter |
| `checks.python.version` | string | e.g. `3.12.4` |
| `checks.python.ok` | boolean | at least 3.12 |
| `checks.git` | object | git |
| `checks.git.version` | string | e.g. `2.45.0`, `""` when git is missing |
| `checks.git.ok` | boolean | at least 2.20 |
| `checks.tailscale` | object | optional tailscale networking |
| `checks.tailscale.found` | boolean | a tailscale CLI was found |
| `checks.tailscale.ok` | boolean | `tailscale status` worked |
| `checks.tailscale.tailnet` | string | current tailnet, `""` when unknown |
| `checks.tailscale.online` | boolean | this node is online |
| `checks.tailscale.owner_tailnet` | string | configured tailnet (`--tailnet` or `owner_tailnet`), `""` when not set |
| `checks.tailscale.error` | string | error detail, `""` when none |
| `hosts` | array of objects or null | executor hosts known to the hub; `null` when the hub could not be queried |
| `hosts[].name` | string | host name, usable as `ct run --host` |
| `hosts[].online` | boolean | heartbeat seen in the last 90 s |
| `hosts[].draining` | boolean | not taking new jobs (`ct drain`) |
| `hosts[].disk_low` | boolean | over its disk watermark, not claiming new jobs |
| `hosts[].queue` | integer | queued jobs targeting this host |
| `hosts[].os` | string | operating system reported by the executor |
| `hosts[].labels` | array of strings | host labels |
| `hosts_error` | string or null | why `hosts` is `null`; `null` otherwise |
| `groups` | object | host groups from the hub, `{group: [host, ...]}` in priority order (usable as `ct run --host <group>`); `{}` when unknown |

## 5. Secrets

Neither output ever contains the token, whether it comes from the config file,
`CALLTABLE_TOKEN` or `--token`: only `token_present` and `token_source` are
reported. Hub URLs are printed without `user:password@`, query or fragment. As
a safety net, any occurrence of a known secret value (token, URL password or
query value, at least 8 characters long) in any string of the output is
replaced with `[redacted]`.
