# Repository declaration `.calltable.json`

Status: draft. Applies to the `ct run` client and the hub from the change that
introduced it (issue #3). Additive: without the file nothing changes.

A project repository can say, in its own tree, what it is called in calltable,
where it runs by default and which named commands it offers. `ct run` reads the
file automatically when it runs inside a git repository, so the usual
invocation needs neither `--project` nor `--host`:

```bash
ct run verify                 # a named task from the declaration
ct run -- ./build/verify.sh   # any command, project/host from the declaration
```

## 1. Location and discovery

* The file is `.calltable.json` at the **git repository root**
  (`git rev-parse --show-toplevel` of `--repo`, default the current directory).
  Subdirectories are not searched separately; running `ct run` anywhere inside
  the repository finds the root file.
* The file is read from the working tree (uncommitted edits count, as they do
  for the snapshot that is submitted).
* Outside a git repository, or without the file, `ct run` behaves exactly as
  before: `--project` is required and `--host` is inferred from profiles.
* The file is UTF-8 JSON (a BOM is tolerated). It must be an object.

## 2. Fields

| field | type | meaning |
| --- | --- | --- |
| `$schema` | string | ignored; allowed so editors can attach a schema |
| `project` | string | project name; same rule as `--project` (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`) |
| `host` | string | default host **or host group** (for example an overflow group) |
| `platform` | `linux` \| `windows` \| `macos` | default platform hint, resolved against the profiles exactly like `--platform`. Mutually exclusive with `host` |
| `slot` | string | default slot |
| `locks` | array of strings | default locks. `[]` means "no locks" and drops the hub profile's locks; absent means "use the hub profile" |
| `timeout` | duration | default timeout: `"90m"`, `"3h"`, or seconds; must be positive |
| `artifacts` | array of strings | worktree-relative globs (`**` supported) collected after the job; replace the hub profile's `artifacts`. `..`, absolute paths and drive letters are rejected |
| `rules` | array of rule objects | command-matching defaults, see §3 |
| `tasks` | object, name → array of strings | named commands for `ct run <task>`, see §4 |

`slot`, `locks`, `timeout`, `artifacts` and `rules` have the same meaning as the
fields of the same name in a hub profile (`profiles/<project>@<host>.json`);
rules are parsed by the same code.

### Fields that are rejected

Anything that depends on the executor host stays in the hub-side profile. These
fields are rejected with an explanation, and the file is not used:

| field | reason |
| --- | --- |
| `env_files` | paths on the executor host |
| `env` | part of the host environment (set variables in the command or a task script) |
| `setup` | commands that prepare the host checkout |
| `memory_max` (top level and in rules) | host resource policy |
| `cpu_weight`, `slice` | host resource policy / systemd slice on the host |
| `cloudrun` | the Cloud Run image belongs to the provider host |
| `unsupported`, `note` | marking a project unsupported on a host is a hub decision |
| `name` | the hub profile key `<project>@<host>`; use `project` / `host` |

Any other unknown field is an error too, so typos are caught before a job is
submitted.

## 3. Rules

```json
"rules": [
  {"match": "integration", "slot": "heavy", "locks": ["db@{host}"], "timeout": "2h"}
]
```

* `match` (required) is a regular expression searched in the `shlex.join`ed
  command; the first matching rule wins.
* `slot`, `locks`, `timeout` are optional. As in profiles, a rule without a
  `locks` key keeps the declaration's locks; `"locks": []` means no locks.
* Only `match`, `slot`, `locks`, `timeout` are allowed; `memory_max` is
  rejected (host-only).

## 4. Tasks and how `ct run` tells a task from a command

```json
"tasks": {"verify": ["./build/verify.sh"], "formal": ["./formal/gate/formal-gate.sh"]}
```

* Task names match `[A-Za-z0-9][A-Za-z0-9._:-]*`; the value is a non-empty
  argv array (no shell string).
* `ct run <name>` runs the task when **all** of these hold: a declaration is
  present, there is **no `--`** on the command line, the command is **exactly
  one word**, and that word is a task name. The job's `cmd` is the task's argv,
  so profile and declaration rules match the expanded command.
* Everything else is a command, unchanged from before: `ct run -- <cmd...>`
  always submits `<cmd...>` literally (use `ct run -- verify` to run a program
  that happens to share a task's name), and multi-word commands without `--`
  are commands as well.
* A single word that is not a task (with tasks declared) runs as a command and
  prints a note on stderr naming the declared tasks.

## 5. Precedence

For every value: **command line > declaration > hub-side profile**.

| value | order |
| --- | --- |
| project | `--project` > `project` |
| host | `--host` > `--platform` (+ profiles) > `host` > `platform` (+ profiles) > inference from profiles |
| slot | `--slot` > matching declaration rule > declaration `slot` > hub profile rule > hub profile > hub default |
| timeout | `--timeout` > matching declaration rule > declaration `timeout` > hub profile rule > hub profile > 3h |
| locks | `--no-lock` drops every default. Otherwise declaration rule `locks` > declaration `locks` replace the hub profile's default locks; if neither is set, the hub profile (and its rules) decide. `--lock` is always added |
| artifacts | declaration `artifacts` > hub profile `artifacts` |

Overriding one field on the command line keeps the declaration's other fields
(for example `ct run --host h2 verify` still uses the declared project and task).

Implementation: the client sends the declaration's values to the hub as if they
were command-line values (`slot`, `timeout`; declared `locks` are sent with
`no_lock: true` so they replace the hub profile's default locks). Values the
declaration does not set are left to the hub, which resolves them from its own
profile as before. Artifacts travel in a new `POST /api/jobs` member:

| field | type | meaning |
| --- | --- | --- |
| `artifact_patterns` | array of strings (≤ 100) | worktree-relative globs that replace the profile's `artifacts` for this job. Absent = use the profile |

The hub validates the patterns (no `..`, no absolute paths), stores them on the
job (`jobs.artifact_patterns`, echoed on the job object; `null` when absent),
includes them in the idempotency fingerprint when present, and puts them into
`profile_data.artifacts` of the executor payload, so executors need no change.
A hub that predates the field does not echo it; the client then warns that the
declared artifacts are not collected.

## 6. Errors

A declaration problem stops `ct run` **before** anything is snapshotted or
submitted, with exit code 2 and a message naming the file and the field path,
for example:

```
error: /src/demo/.calltable.json: field 'rules[1].timeout': invalid duration: 'zz'
error: /src/demo/.calltable.json: field 'setup': not allowed in .calltable.json: setup commands prepare the host checkout; ...
```

Without a declaration and without `--project`, the error is argparse's usual
`the following arguments are required: --project` (exit code 2).

## 7. `ct run --dry-run`

The dry run keeps its previous lines and adds the declaration path and every
effective value with its source:

```
dry-run (nothing submitted)
  project=demo host=linux targets=runner-a,runner-b
  profile=demo@runner-a
  slot=heavy locks=db@{host} timeout=7200s rule=(profile default)
  command=pytest -k integration
  declaration=/src/demo/.calltable.json
  effective values (value <- source):
    project   demo  <- .calltable.json project
    host      linux  <- .calltable.json host
    command   pytest -k integration  <- task 'it' in .calltable.json
    slot      heavy  <- .calltable.json rule 'integration'
    locks     db@{host}  <- .calltable.json rule 'integration'
    timeout   7200s  <- .calltable.json rule 'integration'
    artifacts reports/*.xml  <- .calltable.json artifacts
```

Sources that fall through to the hub are printed as `hub profile <name>` (or
`... rule '<match>'`) when the client has a local copy of the profile, and as
`hub default (no local copy of a hub profile)` otherwise; the hub's own profile
is authoritative.

## 8. Example

```json
{
  "project": "demo",
  "host": "linux",
  "timeout": "30m",
  "artifacts": ["reports/**/*.xml"],
  "rules": [
    {"match": "integration", "slot": "heavy", "locks": ["db@{host}"], "timeout": "2h"}
  ],
  "tasks": {
    "verify": ["./build/verify.sh"],
    "it": ["pytest", "-k", "integration"]
  }
}
```
