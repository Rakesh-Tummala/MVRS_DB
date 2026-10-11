"""Lock manager: compatibility, upgrades, deadlock detection, timeouts."""

from __future__ import annotations

import threading
import time

import pytest

from mvrs_db.errors import DeadlockError, LockTimeoutError
from mvrs_db.transactions.lock_manager import EXCLUSIVE, SHARED, LockManager


def in_thread(function: object, *args: object) -> tuple[threading.Thread, dict[str, object]]:
    """Run function(*args) in a thread, capturing its outcome."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = function(*args)  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001 - the test inspects whatever was raised
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_shared_locks_are_compatible() -> None:
    locks = LockManager(timeout=1)

    locks.acquire(1, "t", SHARED)
    locks.acquire(2, "t", SHARED)

    assert locks.held(1) == {"t": SHARED}
    assert locks.held(2) == {"t": SHARED}


def test_reacquiring_a_held_lock_is_a_no_op() -> None:
    locks = LockManager(timeout=1)
    locks.acquire(1, "t", EXCLUSIVE)

    locks.acquire(1, "t", EXCLUSIVE)
    locks.acquire(1, "t", SHARED)  # exclusive already covers shared

    assert locks.held(1) == {"t": EXCLUSIVE}


@pytest.mark.parametrize(
    ("held", "wanted"), [(EXCLUSIVE, SHARED), (EXCLUSIVE, EXCLUSIVE), (SHARED, EXCLUSIVE)]
)
def test_conflicting_lock_waits_until_release(held: str, wanted: str) -> None:
    locks = LockManager(timeout=5)
    locks.acquire(1, "t", held)

    thread, outcome = in_thread(locks.acquire, 2, "t", wanted)
    thread.join(0.2)
    assert thread.is_alive(), "the second owner should be waiting"

    locks.release_all(1)
    thread.join(2)
    assert not thread.is_alive() and "error" not in outcome
    assert locks.held(2) == {"t": wanted}


def test_locks_on_different_resources_do_not_interact() -> None:
    locks = LockManager(timeout=1)

    locks.acquire(1, "a", EXCLUSIVE)
    locks.acquire(2, "b", EXCLUSIVE)

    assert locks.held(1) == {"a": EXCLUSIVE}
    assert locks.held(2) == {"b": EXCLUSIVE}


def test_sole_shared_holder_can_upgrade() -> None:
    locks = LockManager(timeout=1)
    locks.acquire(1, "t", SHARED)

    locks.acquire(1, "t", EXCLUSIVE)

    assert locks.held(1) == {"t": EXCLUSIVE}


def test_timeout() -> None:
    locks = LockManager(timeout=0.2)
    locks.acquire(1, "t", EXCLUSIVE)

    started = time.monotonic()
    with pytest.raises(LockTimeoutError):
        locks.acquire(2, "t", SHARED)

    assert 0.15 < time.monotonic() - started < 2
    assert locks.held(2) == {}


def test_deadlock_between_two_resources_is_detected() -> None:
    locks = LockManager(timeout=10)
    locks.acquire(1, "a", EXCLUSIVE)
    locks.acquire(2, "b", EXCLUSIVE)
    thread, outcome = in_thread(locks.acquire, 1, "b", EXCLUSIVE)  # 1 waits for 2
    thread.join(0.2)
    assert thread.is_alive()

    started = time.monotonic()
    with pytest.raises(DeadlockError):
        locks.acquire(2, "a", EXCLUSIVE)  # 2 waiting for 1 would close the cycle

    assert time.monotonic() - started < 1, "detection must not wait for the timeout"
    assert locks.deadlocks_detected == 1
    locks.release_all(2)  # the victim rolls back, and the other proceeds
    thread.join(2)
    assert "error" not in outcome
    assert locks.held(1) == {"a": EXCLUSIVE, "b": EXCLUSIVE}


def test_deadlock_from_two_upgrades_is_detected() -> None:
    locks = LockManager(timeout=10)
    locks.acquire(1, "t", SHARED)
    locks.acquire(2, "t", SHARED)
    thread, outcome = in_thread(locks.acquire, 1, "t", EXCLUSIVE)
    thread.join(0.2)
    assert thread.is_alive()

    with pytest.raises(DeadlockError):
        locks.acquire(2, "t", EXCLUSIVE)

    locks.release_all(2)
    thread.join(2)
    assert "error" not in outcome
    assert locks.held(1) == {"t": EXCLUSIVE}


def test_three_way_deadlock_is_detected() -> None:
    locks = LockManager(timeout=10)
    for owner, resource in ((1, "a"), (2, "b"), (3, "c")):
        locks.acquire(owner, resource, EXCLUSIVE)
    first, _ = in_thread(locks.acquire, 1, "b", EXCLUSIVE)
    second, _ = in_thread(locks.acquire, 2, "c", EXCLUSIVE)
    first.join(0.2)
    second.join(0.2)

    with pytest.raises(DeadlockError):
        locks.acquire(3, "a", EXCLUSIVE)

    locks.release_all(3)
    second.join(2)
    locks.release_all(2)
    first.join(2)
    assert not first.is_alive() and not second.is_alive()


def test_waiting_is_not_mistaken_for_deadlock() -> None:
    locks = LockManager(timeout=5)
    locks.acquire(1, "a", EXCLUSIVE)
    waiter, outcome = in_thread(locks.acquire, 2, "a", EXCLUSIVE)
    waiter.join(0.1)

    # Owner 3 waits behind both. Nobody waits for owner 3, so there is no cycle.
    third, third_outcome = in_thread(locks.acquire, 3, "a", SHARED)
    third.join(0.2)
    assert third.is_alive() and "error" not in third_outcome

    locks.release_all(1)
    waiter.join(2)
    locks.release_all(2)
    third.join(2)
    assert "error" not in outcome and "error" not in third_outcome
    assert locks.deadlocks_detected == 0


def test_new_readers_queue_behind_a_waiting_writer() -> None:
    """Without this, a steady stream of readers would starve the writer forever."""
    locks = LockManager(timeout=5)
    locks.acquire(1, "t", SHARED)
    writer, writer_outcome = in_thread(locks.acquire, 2, "t", EXCLUSIVE)
    writer.join(0.2)
    assert writer.is_alive()

    late_reader, reader_outcome = in_thread(locks.acquire, 3, "t", SHARED)
    late_reader.join(0.2)
    assert late_reader.is_alive(), "a reader arriving after the writer must wait its turn"
    locks.acquire(1, "t", SHARED)  # but a reader that already holds the lock is not blocked

    locks.release_all(1)
    writer.join(2)
    assert locks.held(2) == {"t": EXCLUSIVE}
    assert late_reader.is_alive()
    locks.release_all(2)
    late_reader.join(2)
    assert locks.held(3) == {"t": SHARED}
    assert "error" not in writer_outcome and "error" not in reader_outcome


def test_reader_is_released_when_the_writer_ahead_of_it_gives_up() -> None:
    locks = LockManager(timeout=0.4)
    locks.acquire(1, "t", SHARED)
    writer, writer_outcome = in_thread(locks.acquire, 2, "t", EXCLUSIVE)
    writer.join(0.1)
    late_reader, reader_outcome = in_thread(locks.acquire, 3, "t", SHARED)

    writer.join(2)  # the writer times out behind owner 1
    late_reader.join(2)

    assert isinstance(writer_outcome.get("error"), LockTimeoutError)
    assert "error" not in reader_outcome
    assert locks.held(3) == {"t": SHARED}


def test_release_all_releases_everything_an_owner_holds() -> None:
    locks = LockManager(timeout=1)
    locks.acquire(1, "a", EXCLUSIVE)
    locks.acquire(1, "b", SHARED)
    locks.acquire(2, "b", SHARED)

    locks.release_all(1)

    assert locks.held(1) == {}
    assert locks.held(2) == {"b": SHARED}
    locks.acquire(2, "a", EXCLUSIVE)
    locks.release_all(99)  # an owner holding nothing is fine
