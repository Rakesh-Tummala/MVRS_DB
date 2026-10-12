"""Child process for the crash tests.

    python worker.py <database path> <scenario> <transactions> <buffer pages> <checkpoint bytes>

Runs the scenario's setup and then its transactions in order. After each COMMIT
returns, it appends a line to "<database path>.ack" and fsyncs it. That file is
the parent's proof of which commits were acknowledged before the process died.

The crash itself comes from MVRS_CRASH_AT (see mvrs_db/faults.py), which
makes the engine call os._exit at a chosen point. With "exit" as a sixth
argument the worker instead dies right after its last transaction, without
closing the database, as if killed while idle.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenarios  # noqa: E402

import mvrs_db  # noqa: E402


def main() -> None:
    path, scenario = sys.argv[1], sys.argv[2]
    transactions, buffer_pages, checkpoint_bytes = (int(arg) for arg in sys.argv[3:6])
    die_at_end = len(sys.argv) > 6 and sys.argv[6] == "exit"
    setup, run_txn, _verify = scenarios.SCENARIOS[scenario]

    ack_fd = os.open(path + ".ack", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def acknowledge(line: str) -> None:
        os.write(ack_fd, (line + "\n").encode())
        os.fsync(ack_fd)

    # The crash points are counted on a fixed workload, so the extra commits of an automatic
    # ANALYZE are left out except in the one scenario that exists to test them.
    db = mvrs_db.connect(
        path,
        buffer_pages=buffer_pages,
        checkpoint_bytes=checkpoint_bytes,
        auto_analyze=scenarios.AUTO_ANALYZE.get(scenario, False),
    )
    setup(db)
    acknowledge("setup")
    for k in range(transactions):
        run_txn(db, k)
        acknowledge(str(k))
    if die_at_end:
        os._exit(0)
    db.close()
    acknowledge("closed")


if __name__ == "__main__":
    main()
