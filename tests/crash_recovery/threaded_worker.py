"""Child process for the concurrent crash tests.

    python threaded_worker.py <database> <threads> <transactions per thread>
                              <buffer pages> <checkpoint bytes>

Starts several threads. Each runs transactions of two statements, committed
together: insert the next row of its own sequence, and set its counter to the
number of rows inserted so far. After each commit returns, the thread appends
"thread sequence" to "<database>.ack". The ack file is not fsynced: it only has
to survive this process being killed, not the machine losing power, and an fsync
per acknowledgement would serialise the threads and defeat the test.

The crash comes from MVRS_CRASH_AT (see mvrs_db/faults.py). With MVRS_ISOLATION=snapshot
the database uses snapshot isolation and one more thread reads the tables in a loop
while the others commit, so a kill lands while snapshots and page versions exist.
"""

from __future__ import annotations

import os
import sys
import threading

import mvrs_db


def main() -> None:
    path = sys.argv[1]
    threads, per_thread, buffer_pages, checkpoint_bytes = (int(a) for a in sys.argv[2:6])
    ack_fd = os.open(path + ".ack", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    isolation = os.environ.get("MVRS_ISOLATION", "locking")
    db = mvrs_db.connect(
        path, buffer_pages=buffer_pages, checkpoint_bytes=checkpoint_bytes, isolation=isolation
    )
    db.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, tid INTEGER, seq INTEGER, pad TEXT)")
    db.execute("CREATE TABLE counters (tid INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    for tid in range(threads):
        db.execute(f"INSERT INTO counters VALUES ({tid}, 0)")
    os.write(ack_fd, b"setup\n")

    def work(tid: int) -> None:
        connection = db.connect()
        for seq in range(per_thread):
            connection.execute("BEGIN")
            connection.execute(
                f"INSERT INTO log VALUES ({tid * 100_000 + seq}, {tid}, {seq}, '{'p' * 400}')"
            )
            connection.execute(f"UPDATE counters SET n = {seq + 1} WHERE tid = {tid}")
            connection.execute("COMMIT")
            os.write(ack_fd, f"{tid} {seq}\n".encode())

    stop = threading.Event()

    failures: list[BaseException] = []

    def read() -> None:
        connection = db.connect()
        try:
            while not stop.is_set():
                counts = connection.execute("SELECT tid, n FROM counters").rows
                rows = connection.execute("SELECT COUNT(*) FROM log").rows
                # One snapshot per statement, so each answer is a state that existed.
                assert len(counts) == threads and rows[0][0] <= threads * per_thread
        except (AssertionError, mvrs_db.errors.MvrsDbError) as exc:
            failures.append(exc)

    workers = [threading.Thread(target=work, args=(tid,)) for tid in range(threads)]
    reader = threading.Thread(target=read) if isolation == "snapshot" else None
    for worker in workers:
        worker.start()
    if reader is not None:
        reader.start()
    for worker in workers:
        worker.join()
    stop.set()
    if reader is not None:
        reader.join()
    if failures:
        raise failures[0]
    db.close()
    os.write(ack_fd, b"closed\n")


if __name__ == "__main__":
    main()
