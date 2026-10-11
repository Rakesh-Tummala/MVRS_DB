"""The state of one transaction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Transaction:
    txn_id: int
    # True for BEGIN ... COMMIT; False for a single statement run on its own.
    explicit: bool
    # Set once the transaction takes the writer slot and starts changing pages.
    writing: bool = False
    # Set when a statement failed after changing pages. Those changes cannot be
    # undone on their own, so the whole transaction has to be rolled back.
    must_abort: bool = False
