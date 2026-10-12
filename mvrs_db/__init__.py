"""MVRS_DB: an educational relational database engine written from scratch.

import mvrs_db

with mvrs_db.connect("example.mvrs") as db:
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("INSERT INTO users VALUES (1, 'asha')")
    print(db.execute("SELECT * FROM users").rows)
"""

from mvrs_db.database import Connection, Database, IntegrityReport, PreparedStatement
from mvrs_db.errors import MvrsDbError
from mvrs_db.execution.executor import Result

__version__ = "0.1.0"

connect = Database.open

__all__ = [
    "Connection",
    "PreparedStatement",
    "Database",
    "IntegrityReport",
    "MvrsDbError",
    "Result",
    "__version__",
    "connect",
]
