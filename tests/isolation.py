"""Test-time isolation of user-level state.

Nothing in the suite may read or write the real user's home directory, nor fall
back to the real ``~/.calltable/config.json`` or
``~/.config/calltable/config.json`` if a test forgets to pass
``--config``. Importing :mod:`tests` calls :func:`apply` before any test module
loads ``calltable``, so ``Path.home()``, ``find_config()`` and the CLI
subprocesses all see a throwaway home.

The guard patched onto ``calltable.config`` makes it a hard failure when any
code resolves the user config or data directory inside the real home (the
temporary home is usually *inside* the real home on Windows, so it is
explicitly allowed).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

#: The developer's real home, captured before anything is rewritten.
REAL_HOME = Path(os.path.expanduser("~")).resolve()
#: Anything under the OS temp root is a legitimate test fixture (on Windows the
#: temp directory itself lives under the user profile, so containment in
#: REAL_HOME alone is not proof of a leak).
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
#: The throwaway home every test (and its subprocesses) must use.
TEST_HOME = Path(tempfile.mkdtemp(prefix="calltable-test-home-")).resolve()

_APPLIED = False
_PATCHES: list[tuple[object, str, object]] = []


def is_under(path: Path | str, root: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def check_path(path: Path | str) -> Path:
    """Fail if a resolved user-level path points into the real home.

    Temporary fixtures (including the isolated home and the fake homes some
    tests create) live under the OS temp root and are allowed even when that
    root is inside the user profile.
    """
    resolved = Path(path).expanduser().resolve()
    if (is_under(resolved, REAL_HOME)
            and not is_under(resolved, TEST_HOME)
            and not is_under(resolved, TEMP_ROOT)):
        raise AssertionError(
            "test isolation violated: a user-level path resolved to the real "
            f"home directory ({resolved}); tests must never read or write "
            "the developer's real calltable config/data")
    return resolved


def _cleanup() -> None:
    for module, name, original in reversed(_PATCHES):
        try:
            setattr(module, name, original)
        except Exception:
            pass
    shutil.rmtree(TEST_HOME, ignore_errors=True)


def apply() -> None:
    """Isolate the environment; safe to call more than once."""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    os.environ["HOME"] = str(TEST_HOME)
    os.environ["USERPROFILE"] = str(TEST_HOME)
    drive, tail = os.path.splitdrive(str(TEST_HOME))
    if drive:
        os.environ["HOMEDRIVE"] = drive
        os.environ["HOMEPATH"] = tail
    os.environ["XDG_CONFIG_HOME"] = str(TEST_HOME / ".config")

    # A deliberately missing file: a test that forgets --config must fail
    # loudly instead of silently using the developer's real config.
    os.environ["CALLTABLE_CONFIG"] = str(TEST_HOME / "missing-config.json")
    for name in ("CALLTABLE_TOKEN", "CALLTABLE_PROFILES_DIR", "CALLTABLE_AGENT",
                 "CALLTABLE_SUBMITTER"):
        os.environ.pop(name, None)

    from calltable import config as config_module

    for name in ("user_config_path", "legacy_user_config_path", "user_data_dir"):
        original = getattr(config_module, name)

        def guarded(*args, _original=original, **kwargs):
            # 只做检查，不改变返回值（resolved 与否是 config 的既有语义）。
            path = _original(*args, **kwargs)
            check_path(path)
            return path

        setattr(config_module, name, guarded)
        _PATCHES.append((config_module, name, original))

    atexit.register(_cleanup)
