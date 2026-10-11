"""Distances between vectors.

Two measures, written the way pgvector writes them in SQL:

    <->   Euclidean (L2) distance: the straight-line distance between two
          points, sqrt(sum((a_i - b_i)^2)). Zero means identical.
    <=>   Cosine distance: 1 - cos(angle between the vectors). It ignores
          length and compares direction only. 0 means the same direction,
          1 means perpendicular, 2 means opposite.

Cosine distance is undefined when either vector is all zeros, because a zero
vector has no direction. This module returns None in that case, and the query
operator treats None as "no distance".

Two implementations compute the same thing:

* distance() is plain Python, one pair at a time. It is the reference: short
  enough to check by eye.
* batch_distances() takes many vectors at once. With NumPy installed it does
  the arithmetic on a whole matrix in C. Without NumPy it falls back to calling
  distance() in a loop, so the engine works either way.
* batch_distances_raw() takes the vectors as the bytes stored on disk. This is
  the fast one: measured on this engine, building Python tuples of floats costs
  far more than the distance arithmetic, and this path never builds them.

NumPy is the only third-party library the engine can use, and only here.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any

L2 = "l2"
COSINE = "cosine"
OPERATORS = {"<->": L2, "<=>": COSINE}
SYMBOLS = {metric: symbol for symbol, metric in OPERATORS.items()}

Vector = tuple[float, ...]

_np: Any
try:
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by tests through monkeypatching
    _np = None


def numpy_available() -> bool:
    return _np is not None


def as_float32(vector: Sequence[float]) -> Vector:
    """Round each element to the 32-bit value it would have once stored.

    Query vectors are rounded this way so that searching for a vector that is
    in the table finds it at distance exactly zero.
    """
    return struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector))


def is_zero(vector: Sequence[float]) -> bool:
    return not any(vector)


def distance(a: Sequence[float], b: Sequence[float], metric: str) -> float | None:
    """Distance between two vectors of equal length, or None if undefined."""
    if metric == L2:
        return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b, strict=True)))
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    # Rounding can push the cosine a hair outside [-1, 1].
    return min(2.0, max(0.0, 1.0 - dot / math.sqrt(norm_a * norm_b)))


def batch_distances(
    vectors: Sequence[Sequence[float]], query: Sequence[float], metric: str
) -> list[float | None]:
    """Distance from `query` to each of `vectors`."""
    if not vectors:
        return []
    if _np is None:
        return [distance(vector, query, metric) for vector in vectors]

    return _matrix_distances(_np.asarray(vectors, dtype=_np.float64), query, metric)


def batch_distances_raw(
    buffer: bytes, dimension: int, query: Sequence[float], metric: str
) -> list[float | None]:
    """Like batch_distances, for vectors still in their stored form.

    `buffer` is the concatenation of several vectors exactly as they sit in
    their records: little-endian 32-bit floats, `dimension` of them each. NumPy
    reads that memory directly, so no Python float objects are created. The
    arithmetic is done in 64 bits, as in the other paths, so all three agree.

    Requires NumPy.
    """
    if not buffer:
        return []
    matrix = _np.frombuffer(buffer, dtype="<f4").reshape(-1, dimension).astype(_np.float64)
    return _matrix_distances(matrix, query, metric)


def _matrix_distances(matrix: Any, query: Sequence[float], metric: str) -> list[float | None]:
    target = _np.asarray(query, dtype=_np.float64)
    if metric == L2:
        difference = matrix - target
        result: list[float | None] = _np.sqrt(
            _np.einsum("ij,ij->i", difference, difference)
        ).tolist()
        return result
    norms = _np.einsum("ij,ij->i", matrix, matrix)
    target_norm = float(target @ target)
    if target_norm == 0.0:
        return [None] * len(matrix)
    undefined = norms == 0.0
    # Avoid dividing by zero; those rows are replaced by None below.
    safe = _np.where(undefined, 1.0, norms)
    cosine = (matrix @ target) / _np.sqrt(safe * target_norm)
    distances = _np.clip(1.0 - cosine, 0.0, 2.0).tolist()
    if undefined.any():
        return [None if missing else d for d, missing in zip(distances, undefined, strict=True)]
    result = distances
    return result
