# Contributing

Thanks for taking a look at calltable.

## Ground rules

* Python 3.12+ standard library only. Do not add third-party dependencies
  without discussing it first.
* Keep the platform layers isolated: `calltable/proc.py` imports
  `proc_linux` / `proc_windows` lazily, so `import calltable` must work on every
  OS (there are tests pinning this).
* Behaviour that agents rely on (exit codes, `--wait`, snapshot semantics, the
  `MODE=...` line from `ct doctor`) is part of the contract. Change it only with
  tests and a note in the pull request.
* Never commit tokens, real host names, private addresses, or deployment configs
  (`config.json`, `config.exec.json`, `data/`). `config.example.json` is the
  template.

## Development

```bash
git clone https://github.com/eanzhao-os/calltable.git
cd calltable
python -m unittest discover -s tests -t . -v     # full suite, this machine
python -m calltable --version
python bin/ci-local                              # full suite on devpc + yiqipc
```

There is nothing to install: run the package from the checkout, or link the
shim with `bash bin/install.sh`.

Some tests exercise real `git` repositories and real subprocesses; they create
their fixtures in temporary directories and clean up after themselves. Tests
that need a specific platform are skipped elsewhere.

**Test isolation is mandatory.** Importing `tests` repoints `HOME`,
`USERPROFILE`, `HOMEDRIVE`/`HOMEPATH` and `CALLTABLE_CONFIG` at a throwaway
directory before any test loads `calltable`; a guard fails the suite if any
user config/data path resolves into the real home. Never bypass that (for
example by passing `--config` after `--` in a CLI argument list, which makes
the CLI fall back to the user config), and never start a hub, executor or CLI
against a real deployment from a test.

## Pull requests

1. Keep changes focused; one behaviour per pull request when practical.
2. Add or update tests for every behaviour change.
3. Update `README.md` when user-facing commands, config keys or profile fields
   change, and make the same change in `README.zh-CN.md` (the Chinese
   translation; both files share the images in `docs/images/`).
4. Make sure the suite is green on your platform. There is no GitHub Actions
   workflow in this repository: run `python bin/ci-local` to execute the suite
   through calltable on the real executors, `devpc` (Linux) and `yiqipc`
   (Windows); it exits non-zero unless both pass.

## Code layout

```
calltable/
  cli.py         command line entry point (hub/exec/cli subcommands)
  config.py      config model and lookup order
  profiles.py    profile loading, rules, host inference
  db.py          SQLite schema, atomic scheduling, reconciliation
  hub.py         HTTP JSON API + dashboard routes + janitor
  client.py      standard-library hub client (streaming upload/download)
  executor.py    claim, prepare, run, log pump, sampling, artifacts, retries
  gitops.py      snapshot commits, bundles, mirrors, per-job checkouts
  proc*.py       process-tree abstraction (systemd / process group / Job Object)
  disk.py        disk watermarks and host resource snapshot
  dashboard.py   single-page dashboard (inline CSS/JS)
  doctor.py      `ct doctor` checks
  setup.py       `ct setup` / `ct self-update`
  seed.py        pre-fill a mirror from a local checkout
tests/           unittest suite
```

## Licensing

By contributing you agree that your contribution is licensed under the
Apache License, Version 2.0 (see `LICENSE`).
