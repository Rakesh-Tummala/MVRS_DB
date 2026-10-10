"""KestrelDB: an educational relational database engine written from scratch.

import kestreldb

with kestreldb.connect("example.kdb") as db:
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("INSERT INTO users VALUES (1, 'asha')")
    print(db.execute("SELECT * FROM users").rows)
"""

from kestreldb.database import Connection, Database, IntegrityReport
from kestreldb.errors import KestrelError
from kestreldb.execution.executor import Result

__version__ = "0.1.0"

connect = Database.open

__all__ = [
    "Connection",
    "Database",
    "IntegrityReport",
    "KestrelError",
    "Result",
    "__version__",
    "connect",
]
