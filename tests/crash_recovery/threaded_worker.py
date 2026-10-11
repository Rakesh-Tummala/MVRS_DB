"""Child process for the concurrent crash tests.

    python threaded_worker.py <database> <threads> <transactions per thread>
                              <buffer pages> <checkpoint bytes>

Starts several threads. Each runs transactions of two statements, committed
together: insert the next row of its own sequence, and set its counter to the
number of rows inserted so far. After each commit returns, the thread appends
"thread sequence" to "<database>.ack". The ack file is not fsynced: it only has
to survive this process being killed, not the machine losing power, and an fsync
per acknowledgement would serialise the threads and defeat the test.

The crash comes from MVRS_CRASH_AT (see mvrs_db/faults.py).
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

    db = mvrs_db.connect(path, buffer_pages=buffer_pages, checkpoint_bytes=checkpoint_bytes)
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

    workers = [threading.Thread(target=work, args=(tid,)) for tid in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    db.close()
    os.write(ack_fd, b"closed\n")


if __name__ == "__main__":
    main()
