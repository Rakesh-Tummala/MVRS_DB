"""Crash injection for the crash-recovery tests.

A test starts a child process with the environment variable MVRS_CRASH_AT set
to "point" or "point:n". The n-th time execution reaches that named point, the
child dies immediately with os._exit: no cleanup, no flushing of Python buffers,
no `finally` blocks. That is as close to a real crash as a process can get
without cutting the power.

When the variable is not set, every hook is a single boolean test.
"""

from __future__ import annotations

import os

ENV_VAR = "MVRS_CRASH_AT"
CRASH_EXIT_CODE = 86

_name: str | None = None
_remaining = 0


def _configure(spec: str | None) -> None:
    global _name, _remaining
    if not spec:
        _name, _remaining = None, 0
        return
    name, _, count = spec.partition(":")
    _name = name
    _remaining = int(count) if count else 1


_configure(os.environ.get(ENV_VAR))


def armed(name: str) -> bool:
    """True when the *next* hit of `name` should crash. Counts the hit."""
    global _remaining
    if _name is None or name != _name:
        return False
    _remaining -= 1
    return _remaining == 0


def crash() -> None:
    """Die now, the way a killed process does."""
    os._exit(CRASH_EXIT_CODE)


def crash_point(name: str) -> None:
    """Crash here if this point is the armed one."""
    if _name is not None and armed(name):
        crash()
