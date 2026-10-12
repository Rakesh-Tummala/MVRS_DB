"""Constants that define the on-disk format and the engine's limits.

See docs/02-scope-and-limits.md and docs/03-storage-format.md.
"""

from __future__ import annotations

# --- data file header (page 0) ---
MAGIC = b"MVRS_DB\x00"
FILE_FORMAT_VERSION = 1
FILE_HEADER_SIZE = 64

# --- pages ---
PAGE_FORMAT_VERSION = 1
DEFAULT_PAGE_SIZE = 4096
# In-page offsets are 16-bit, so a page can be at most 32768 bytes.
ALLOWED_PAGE_SIZES = (4096, 8192, 16384, 32768)

# Page IDs are 32-bit unsigned. Page 0 is the file header.
MAX_PAGE_ID = 2**32 - 1

# --- buffer pool ---
DEFAULT_BUFFER_PAGES = 1024
MIN_BUFFER_PAGES = 8

# --- index ---
MAX_KEY_SIZE = 255

# --- write-ahead log ---
WAL_MAGIC = b"MVRSWAL\x00"
WAL_FORMAT_VERSION = 1
WAL_SUFFIX = ".wal"
LOCK_SUFFIX = ".lock"
# The log is emptied by a checkpoint once it grows past this many bytes.
DEFAULT_CHECKPOINT_BYTES = 8 * 1024 * 1024

# --- statistics ---
# A table's statistics are refreshed automatically once this many rows plus this
# fraction of the rows it had when last analysed have been inserted, updated or
# deleted (PostgreSQL's autovacuum uses the same two numbers: 50 and 10%).
AUTO_ANALYZE_MIN_CHANGES = 50
AUTO_ANALYZE_FRACTION = 0.1

# --- SQL ---
MAX_SQL_LENGTH = 1024 * 1024
MAX_IDENTIFIER_LENGTH = 63
MAX_COLUMNS = 64

# --- vectors ---
# An upper bound on VECTOR(n). A vector must also fit in one record, so the
# practical limit at the default 4 KiB page is about 1000 dimensions.
MAX_VECTOR_DIMENSION = 4096

# --- transactions ---
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
