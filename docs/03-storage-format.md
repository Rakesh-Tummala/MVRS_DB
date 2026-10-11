# 03 — On-disk storage format (format version 1)

All multi-byte integers are little-endian. Offsets are in bytes from the start of the page. `P` is the page size.

## The data file

The file is an array of pages. Page `n` lives at byte offset `n × P`. The file length is always a multiple of `P`.

| Page | Contents |
|---|---|
| 0 | File header |
| 1 | First page of the catalog (always a heap page) |
| 2+ | Heap pages, B+ tree pages, free pages |

## Page 0: file header

The first 64 bytes are the header. The rest of page 0 is zero. The header is read before the page size is known, which is why it has its own layout and its own checksum.

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 8 | `magic` | `b"MVRS_DB\0"` |
| 8 | 2 | `format_version` | 1 |
| 10 | 2 | reserved | 0 |
| 12 | 4 | `page_size` | 4096, 8192, 16384 or 32768 |
| 16 | 4 | `page_count` | Pages in the file, including page 0 |
| 20 | 4 | `free_list_head` | First free page, 0 = none |
| 24 | 4 | `catalog_root` | First page of the catalog; 0 only until the catalog is created on first open |
| 28 | 4 | reserved | 0 |
| 32 | 8 | `lsn` | Last log record that changed the header (0 before Phase 4) |
| 40 | 8 | `db_id` | Random at creation; ties the WAL to this file |
| 48 | 12 | reserved | 0 |
| 60 | 4 | `header_crc` | CRC32 of bytes 0–59 |

`struct` format: `<8sHHIIIIIQQ12sI` (64 bytes).

Open checks, in order, each with its own error: file shorter than 64 bytes; wrong magic; bad header CRC; unsupported version; invalid page size; file length not equal to `page_count × P`. Nothing is written to a file that fails any check.

## Common page header (pages 1 and up)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `checksum` | CRC32 of bytes 4 to P−1 |
| 4 | 4 | `page_id` | Must equal the ID the page was read at |
| 8 | 8 | `lsn` | Last log record applied to this page |
| 16 | 1 | `page_type` | See below |
| 17 | 1 | `format_version` | 1 |
| 18 | 2 | `flags` | 0 |
| 20 | 4 | reserved | 0 |

`struct` format: `<IIQBBHI` (24 bytes).

| `page_type` | Meaning |
|---|---|
| 0 | Invalid. An all-zero page is never valid. |
| 1 | Free |
| 2 | Heap (slotted records) |
| 3 | B+ tree leaf |
| 4 | B+ tree internal |

**Checksum.** Computed when a page is written to disk, verified when it is read. It is first in the page so it covers one contiguous range. CRC32 detects torn writes (half old, half new) and random bit damage with probability about 1 − 2⁻³². It is not cryptographic and does not detect a *lost write*, where the disk keeps the whole old page with its old, valid checksum.

**Page ID in the header** detects a misdirected write: a valid page stored at the wrong offset.

## Heap page (type 2)

Records grow down from the end of the page. The slot directory grows up from the header. Free space is the gap between them.

```
0        24        36                                      P
+--------+---------+---------+.........+------------------+
| common | heap    | slots → |  free   | ← record bytes   |
| header | header  |         |         |                  |
+--------+---------+---------+.........+------------------+
                              ^free_end
```

Heap header (12 bytes, format `<HHHHI`):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 24 | 2 | `slot_count` | Slots in the directory, including dead ones |
| 26 | 2 | `free_end` | Offset of the lowest record byte; P when empty |
| 28 | 2 | `frag_bytes` | Bytes held by deleted records, reclaimable by compaction |
| 30 | 2 | reserved | 0 |
| 32 | 4 | `next_page` | Next heap page of the same table, 0 = last |

Slot entry (4 bytes, format `<HH`), slot `i` at offset `36 + 4i`:

| Field | Notes |
|---|---|
| `offset` | Where the record starts; 0 = dead slot |
| `length` | Record length in bytes |

Rules:

* Contiguous free space = `free_end − (36 + 4 × slot_count)`.
* **Slot numbers never change.** Deleting a record sets its slot's offset to 0. Compaction moves record bytes and rewrites offsets but keeps slot numbers. This is what lets an index hold a record ID safely.
* An insert reuses a dead slot if one exists, otherwise appends a slot.
* An update writes in place if the new record is no longer than the old one. Otherwise it is stored again in the same page under the same slot if it fits (after compaction if needed), and only then moved to another page, which changes the record ID and requires an index update.

## Record ID

A record ID (RID) is `(page_id: u32, slot: u16)`, 6 bytes, format `<IH`. Indexes store RIDs. A table scan produces them by walking the heap chain page by page and slot by slot.

## Record encoding (row format 1)

A record is decoded against the table's schema from the catalog. It is not self-describing.

```
[format: u8 = 1] [null bitmap: ceil(n/8) bytes] [values of non-NULL columns, in column order]
```

| Type | Encoding |
|---|---|
| `INTEGER` | 8 bytes, signed (`<q`) |
| `REAL` | 8 bytes, IEEE-754 double (`<d`); NaN rejected at encode time |
| `TEXT` | `u16` byte length, then UTF-8 bytes |
| `VECTOR(n)` | `u16` element count, then that many 32-bit IEEE-754 floats (`<f`); NaN and infinity rejected |
| NULL | Bit `i` of the bitmap set; no value bytes |

A vector is stored at 32-bit precision, the usual choice for embeddings: half the space, and the models that produce them are no more precise. A value read back is the nearest 32-bit float to the one written, so `0.1` returns as `0.10000000149011612`. A 768-dimension vector takes 3,074 bytes and fits a 4 KiB page; 1,536 dimensions needs 8 KiB pages.

The decoder rejects, with `CorruptRecordError`: an unknown format byte, set bits beyond column `n`, a value that runs past the end of the record, invalid UTF-8, and trailing bytes.

## Table storage and the catalog

A table is a chain of heap pages linked by `next_page`. Its first page never changes for the life of the table.

The catalog is itself a chain of heap pages, starting at the page named by `catalog_root` (page 1 in practice). Each catalog record is one table definition in its own explicit binary layout (`catalog/schema.py`):

```
[format u8 = 1][table_id u32][first_heap_page u32]
[name_len u8][name]
[column_count u16]
per column: [name_len u8][name][type u8][flags u8][index_root u32]
            [dimension u16]   only when the type is VECTOR
```

`flags`: bit 0 `NOT NULL`, bit 1 `PRIMARY KEY`, bit 2 `UNIQUE`. `index_root` is the root page of the column's B+ tree, or 0 if the column has no index. It is not the row format, because the catalog has to be readable before any schema is known. On open, the engine reads the whole chain into memory.

Creating the catalog is the first logged transaction of a new database, so a crash during creation leaves a file that is simply initialised again on the next open.

**Index root pages never move.** When a root splits, its contents are copied to a new page and the root page is rewritten in place as the new root. The catalog therefore never needs updating because of a split.

## Page allocation and freeing

* **Free list.** Free pages form a linked list. `free_list_head` in the file header points at the first; each free page (type 1) stores the next free page ID as a `u32` at offset 24.
* **Allocate:** pop the head of the free list; if it is empty, increase `page_count`. Growing is *logical*: the new page exists only in the buffer pool, and the file gets longer when the page is first written. Nothing uncommitted touches the disk.
* **Free:** rewrite the page as type 1 and push it on the list.
* **Finding room for a record.** Each open table keeps an in-memory map of `page → free bytes`, built by walking the table's chain the first time it is written after open. Pages are grouped into power-of-two size classes, so finding a page with enough room is a constant-time lookup. The map is never stored on disk, so it cannot become inconsistent after a crash.

Because allocation is logical, the header on disk can lag behind the file between checkpoints. The "file length = page count × page size" check (F1) therefore runs after recovery, which brings the header up to date from the log.

## How indexes reference records

A B+ tree maps **key bytes** to a RID. Keys are compared as raw bytes, so each type has an order-preserving encoding (big-endian integer with the sign bit flipped, UTF-8 for text, a transformed IEEE layout for reals). The tree never needs to know column types. The node layout and algorithms are in `09-btree.md`.

Duplicate-key policy: the tree stores each key once and rejects a second insert of the same key. That is all a primary-key or unique index needs.

## Versioning

`format_version` in the file header covers the file layout; the per-page and per-record format bytes let individual layouts change later. Version 1 code refuses any version it does not know.
