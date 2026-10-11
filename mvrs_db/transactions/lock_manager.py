"""Shared/exclusive locks with deadlock detection.

Each lock protects a named resource (a table, or the single writer slot).

    S (shared)     many transactions may hold it at once; taken to read.
    X (exclusive)  only one transaction may hold it, and no one else may hold S.

Strict two-phase locking: a transaction only acquires locks while it runs and
releases them all together when it ends. Holding every lock until the end is
what stops other transactions from seeing uncommitted changes (no dirty reads)
or changing something this transaction has read (repeatable reads).

Fairness
    If readers could always join other readers, a steady stream of them would
    keep a writer waiting forever. So a new shared request also waits behind
    any exclusive request that was already waiting for the same resource.

Deadlock
    Two transactions can each hold a lock the other wants. Before a transaction
    waits, the manager checks the "waits-for" graph: an edge A -> B means A is
    waiting for something B holds (or for B's turn). If waiting would close a
    cycle that leads back to the requester, nobody in the cycle could ever
    proceed, so the requester gets DeadlockError instead of waiting. Its
    transaction is then rolled back, releasing its locks and letting the others
    continue.

A timeout backs this up, so a bug or an unexpectedly long transaction turns
into an error and not a hang.
"""

from __future__ import annotations

import itertools
import threading
import time

from mvrs_db.errors import DeadlockError, LockTimeoutError

SHARED = "S"
EXCLUSIVE = "X"

_NOT_WAITING = float("inf")


class LockManager:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._condition = threading.Condition()
        self._holders: dict[str, dict[int, str]] = {}  # resource -> owner -> mode
        # owner -> (resource, mode, ticket). Tickets order waiters by arrival.
        self._waiting: dict[int, tuple[str, str, int]] = {}
        self._tickets = itertools.count()
        self.deadlocks_detected = 0

    def _blockers(self, owner: int, resource: str, mode: str, ticket: float) -> set[int]:
        """Other owners this request has to wait for."""
        holders = self._holders.get(resource, {})
        if mode == EXCLUSIVE:
            return {o for o in holders if o != owner}
        blockers = {o for o, held in holders.items() if o != owner and held == EXCLUSIVE}
        # Fairness: queue behind exclusive requests that arrived earlier.
        for other, (other_resource, other_mode, other_ticket) in self._waiting.items():
            if (
                other != owner
                and other_resource == resource
                and other_mode == EXCLUSIVE
                and other_ticket < ticket
            ):
                blockers.add(other)
        return blockers

    def _would_deadlock(self, owner: int, blockers: set[int]) -> bool:
        """Follow waits-for edges from the blockers; a path back to `owner` is a cycle."""
        seen: set[int] = set()
        stack = list(blockers)
        while stack:
            current = stack.pop()
            if current == owner:
                return True
            if current in seen:
                continue
            seen.add(current)
            waiting = self._waiting.get(current)
            if waiting is not None:
                stack.extend(self._blockers(current, *waiting))
        return False

    def acquire(self, owner: int, resource: str, mode: str) -> None:
        """Block until the lock is granted. Raises DeadlockError or LockTimeoutError."""
        deadline = time.monotonic() + self._timeout
        with self._condition:
            held = self._holders.get(resource, {}).get(owner)
            if held in (EXCLUSIVE, mode):
                return
            ticket: float = _NOT_WAITING
            try:
                while True:
                    blockers = self._blockers(owner, resource, mode, ticket)
                    if not blockers:
                        self._holders.setdefault(resource, {})[owner] = mode
                        return
                    if owner not in self._waiting:
                        ticket = next(self._tickets)
                        self._waiting[owner] = (resource, mode, int(ticket))
                    if self._would_deadlock(owner, blockers):
                        self.deadlocks_detected += 1
                        raise DeadlockError(
                            f"deadlock detected while waiting for the {mode} lock on {resource}; "
                            f"this transaction was chosen to be rolled back"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LockTimeoutError(
                            f"timed out after {self._timeout}s waiting for the "
                            f"{mode} lock on {resource}"
                        )
                    self._condition.wait(remaining)
            finally:
                if self._waiting.pop(owner, None) is not None:
                    # Requests queued behind this one may be able to proceed now.
                    self._condition.notify_all()

    def release_all(self, owner: int) -> None:
        with self._condition:
            for resource in [r for r, holders in self._holders.items() if owner in holders]:
                holders = self._holders[resource]
                del holders[owner]
                if not holders:
                    del self._holders[resource]
            self._condition.notify_all()

    def held(self, owner: int) -> dict[str, str]:
        """The locks an owner currently holds (for tests and diagnostics)."""
        with self._condition:
            return {r: holders[owner] for r, holders in self._holders.items() if owner in holders}
