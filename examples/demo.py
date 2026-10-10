"""KestrelDB demo: create, populate, query, reopen, crash mid-transaction, recover.

    python examples/demo.py

The crash is real. A child process is killed with os._exit halfway through a
transaction, with no chance to clean up. The parent then reopens the database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import kestreldb


def show(db: kestreldb.Database, sql: str) -> None:
    print(f"\n> {sql}")
    result = db.execute(sql)
    for row in result.rows:
        print("   ", row[0] if result.columns == ("plan",) else row)
    if not result.columns:
        print(f"    ok ({result.rowcount} rows affected)")


def crashing_child(path: str) -> None:
    """Runs in the child: one committed transfer, then a crash in the middle of another."""
    db = kestreldb.connect(path)
    db.execute("BEGIN")
    db.execute("UPDATE accounts SET balance = 400 WHERE id = 1")
    db.execute("UPDATE accounts SET balance = 600 WHERE id = 2")
    db.execute("COMMIT")
    print("    child: committed a transfer of 100 from account 1 to account 2", flush=True)

    db.execute("BEGIN")
    db.execute("UPDATE accounts SET balance = 0 WHERE id = 1")  # money leaves account 1 ...
    print("    child: took 400 out of account 1, about to credit account 2 ...", flush=True)
    print("    child: *** killed here ***", flush=True)
    os._exit(1)  # ... and the process dies before the money arrives anywhere


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--crash":
        crashing_child(sys.argv[2])
        return

    folder = Path(tempfile.mkdtemp(prefix="kestrel-demo-", dir=Path(__file__).parent))
    path = folder / "bank.kdb"
    print(f"database file: {path}")

    print("\n=== 1. create and populate ===")
    db = kestreldb.connect(path)
    show(db, "CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, balance INTEGER)")
    show(db, "INSERT INTO accounts VALUES (1, 'asha', 500), (2, 'ravi', 500), (3, 'mei', 250)")

    print("\n=== 2. query ===")
    show(db, "SELECT * FROM accounts ORDER BY balance DESC, id")
    show(db, "SELECT owner FROM accounts WHERE balance >= 300 AND NOT owner = 'ravi'")
    show(db, "EXPLAIN SELECT * FROM accounts WHERE id = 2")

    print("\n=== 3. constraints and rollback ===")
    try:
        db.execute("INSERT INTO accounts VALUES (1, 'duplicate', 0)")
    except kestreldb.KestrelError as exc:
        print(f"\n> INSERT with an existing id\n    refused: {exc}")
    show(db, "BEGIN")
    show(db, "DELETE FROM accounts")
    show(db, "ROLLBACK")
    show(db, "SELECT id FROM accounts ORDER BY id")

    print("\n=== 4. close and reopen ===")
    db.close()
    db = kestreldb.connect(path)
    show(db, "SELECT * FROM accounts ORDER BY id")
    db.close()

    print("\n=== 5. crash in the middle of a transaction ===")
    sys.stdout.flush()
    subprocess.run([sys.executable, __file__, "--crash", str(path)], check=False)

    print("\n=== 6. reopen: recovery runs automatically ===")
    db = kestreldb.connect(path)
    stats = db.recovery
    print(
        f"    recovery replayed {stats.committed_transactions} committed transaction(s) "
        f"and restored {stats.pages_restored} page(s) from the log"
    )
    show(db, "SELECT * FROM accounts ORDER BY id")
    total = sum(row[0] for row in db.execute("SELECT balance FROM accounts").rows)  # type: ignore[misc]
    print(f"\n    total money: {total} (it was 1250 before the crash)")
    print("    the committed transfer survived; the half-finished one left no trace")
    report = db.check()
    print(f"    integrity check passed: {report.pages} pages, {report.tables}")
    db.close()

    for leftover in folder.iterdir():
        leftover.unlink()
    folder.rmdir()


if __name__ == "__main__":
    main()
