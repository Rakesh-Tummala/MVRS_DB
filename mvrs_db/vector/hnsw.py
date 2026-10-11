"""HNSW: an approximate nearest-neighbour index, held in memory.

The problem
    Exact search measures the query against every stored vector. That is
    linear in the table size. An approximate index answers "roughly the k
    nearest" by looking at a small fraction of the vectors, accepting that it
    will occasionally miss one of the true nearest.

The idea (Hierarchical Navigable Small World, Malkov and Yashunin, 2016)
    Build a graph in which each vector is a node linked to a handful of nearby
    vectors. To search, start somewhere and keep stepping to whichever
    neighbour is closer to the query, like walking downhill.

    One graph alone gets stuck or takes many small steps. So there are layers.
    Every node is in layer 0. A node is also in layer 1 with a small
    probability, in layer 2 with a smaller one, and so on, like a skip list.
    The top layers hold few nodes with long links: a search starts there,
    covers a lot of ground in a few hops, and drops a layer each time it can
    get no closer, finishing with a careful search in the dense bottom layer.

The knobs
    M                links per node (2*M in layer 0). More links: better recall,
                     more memory, slower build.
    ef_construction  how many candidates are examined when inserting a node.
                     Larger: a better graph, a slower build.
    ef_search        how many candidates a query keeps while searching layer 0.
                     This is the recall/speed dial at query time. It must be at
                     least k.

Measuring quality
    recall@k = (how many of the true k nearest the index returned) / k.
    The exact search is the ground truth. See benchmarks/vector_recall.py.

What is simplified here
    * The index lives only in memory. It is rebuilt from the table when first
      needed after the database is opened, and thrown away on rollback. It is
      never written to disk, so it needs no logging or recovery.
    * Deleting marks a node as dead. Dead nodes still route searches but are
      never returned. The owner rebuilds the index when too many pile up.
    * Distances inside the graph are squared Euclidean. For cosine, vectors are
      scaled to length 1 on the way in, which makes the Euclidean ranking equal
      to the cosine ranking. A zero vector has no direction and is not indexed.

Requires NumPy. Without it the planner uses the exact search instead.
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

from mvrs_db.vector import distance

_np: Any = distance._np


@dataclass(slots=True)
class SearchSettings:
    """Per-database switches for vector search."""

    # False forces the exact scan even when an index exists (used to get ground truth).
    use_index: bool = True
    # Candidates kept while searching the bottom layer. Raised to k if smaller.
    # 100 measured a recall@10 of 0.87 to 0.97 on the evaluation datasets.
    ef_search: int = 100
    # Below this many rows the exact scan is fast enough and is always right.
    min_rows: int = 1000


class HnswIndex:
    def __init__(
        self,
        dimension: int,
        metric: str,
        *,
        m: int = 16,
        ef_construction: int = 100,
        seed: int = 0,
    ) -> None:
        if _np is None:
            raise RuntimeError("the HNSW index needs NumPy")
        self.dimension = dimension
        self.metric = metric
        self.m = m
        self.m0 = 2 * m
        self.ef_construction = ef_construction
        self._level_scale = 1.0 / math.log(m)
        self._random = random.Random(seed)
        self._vectors = _np.empty((1024, dimension), dtype=_np.float32)
        self._count = 0
        # _links[node][layer] is the list of that node's neighbours in that layer.
        self._links: list[list[list[int]]] = []
        self._payloads: list[Hashable] = []
        self._dead = bytearray()
        self._node_of: dict[Hashable, int] = {}
        self._entry = -1
        self._top_layer = -1
        self.distance_computations = 0

    # ------------------------------------------------------------------ size

    def __len__(self) -> int:
        """Vectors that can be returned by a search."""
        return len(self._node_of)

    @property
    def dead_fraction(self) -> float:
        return 0.0 if self._count == 0 else 1.0 - len(self._node_of) / self._count

    # ------------------------------------------------------------------ helpers

    def _prepare(self, vector: bytes | Sequence[float]) -> Any | None:
        """A float32 array for a vector, scaled to length 1 for cosine. None if unusable."""
        if isinstance(vector, bytes):
            array = _np.frombuffer(vector, dtype="<f4").astype(_np.float32)
        else:
            array = _np.asarray(vector, dtype=_np.float32)
        if self.metric == distance.COSINE:
            norm = float(_np.sqrt(array @ array))
            if norm == 0.0:
                return None
            array = array / norm
        return array

    def _distances(self, target: Any, nodes: list[int]) -> list[float]:
        """Squared Euclidean distance from `target` to each listed node."""
        self.distance_computations += len(nodes)
        difference = self._vectors[nodes] - target
        result: list[float] = _np.einsum("ij,ij->i", difference, difference).tolist()
        return result

    def _search_layer(
        self, target: Any, entry_points: list[tuple[float, int]], ef: int, layer: int
    ) -> list[tuple[float, int]]:
        """Best-first search within one layer. Returns up to `ef` (distance, node), nearest first.

        `candidates` is a min-heap of nodes still to expand. `results` is a
        max-heap (distances negated) of the best `ef` found so far. The search
        stops when the closest unexpanded candidate is farther than the worst
        result: nothing reachable from it can improve the answer.
        """
        visited = {node for _d, node in entry_points}
        candidates = list(entry_points)
        heapq.heapify(candidates)
        results = [(-d, node) for d, node in entry_points]
        heapq.heapify(results)
        links = self._links
        while candidates:
            d, current = heapq.heappop(candidates)
            if len(results) >= ef and d > -results[0][0]:
                break
            fresh = [n for n in links[current][layer] if n not in visited]
            if not fresh:
                continue
            visited.update(fresh)
            worst = -results[0][0]
            for dn, n in zip(self._distances(target, fresh), fresh, strict=True):
                if len(results) < ef or dn < worst:
                    heapq.heappush(candidates, (dn, n))
                    heapq.heappush(results, (-dn, n))
                    if len(results) > ef:
                        heapq.heappop(results)
                    worst = -results[0][0]
        return sorted((-nd, node) for nd, node in results)

    def _select(self, candidates: list[tuple[float, int]], count: int) -> list[int]:
        """Choose up to `count` neighbours from candidates sorted nearest-first.

        The plain choice is "the closest ones", but in clustered data those all
        sit on one side and the node ends up with no link out of its cluster.
        The heuristic from the paper keeps a candidate only if it is closer to
        the new node than to every neighbour already chosen, which spreads the
        links in different directions.

        Three rules were measured on 5,000 vectors of 64 dimensions (recall@10
        at ef_search 10 / 50 / 100, and build time):

            closest only            clustered 0.38 / 0.52 / 0.64    5 s
                                    uniform   0.48 / 0.87 / 0.96    9 s
            heuristic (this one)    clustered 0.69 / 0.96 / 0.99   11 s
                                    uniform   0.46 / 0.87 / 0.97   13 s
            heuristic, then fill    clustered 0.75 / 0.97 / 1.00   25 s
            spare slots with the    uniform   0.48 / 0.88 / 0.97   32 s
            closest rejected

        "Closest only" is fast to build and poor on clustered data. Filling the
        spare slots buys a little recall for more than twice the build time.
        Since the index is rebuilt every time the database is opened, build
        time matters, and the plain heuristic is the choice.
        """
        chosen: list[int] = []
        for d, candidate in candidates:
            if len(chosen) >= count:
                break
            if not chosen or d < min(self._distances(self._vectors[candidate], chosen)):
                chosen.append(candidate)
        return chosen

    # ------------------------------------------------------------------ building

    def add(self, payload: Hashable, vector: bytes | Sequence[float]) -> bool:
        """Insert a vector. Returns False if it cannot be indexed (zero vector under cosine)."""
        target = self._prepare(vector)
        if target is None:
            return False
        node = self._count
        if node == len(self._vectors):
            grown = _np.empty((2 * node, self.dimension), dtype=_np.float32)
            grown[:node] = self._vectors
            self._vectors = grown
        self._vectors[node] = target
        self._count += 1

        # Layer chosen at random with exponentially falling probability.
        level = int(-math.log(1.0 - self._random.random()) * self._level_scale)
        self._links.append([[] for _ in range(level + 1)])
        self._payloads.append(payload)
        self._dead.append(0)
        self._node_of[payload] = node

        if self._entry < 0:
            self._entry, self._top_layer = node, level
            return True

        nearest = [(self._distances(target, [self._entry])[0], self._entry)]
        # Above the new node's top layer: just walk towards it.
        for layer in range(self._top_layer, level, -1):
            nearest = self._search_layer(target, nearest, 1, layer)
        # From its top layer down: find neighbours and link both ways.
        for layer in range(min(level, self._top_layer), -1, -1):
            found = self._search_layer(target, nearest, self.ef_construction, layer)
            neighbours = self._select(found, self.m)
            self._links[node][layer] = neighbours
            limit = self.m0 if layer == 0 else self.m
            for neighbour in neighbours:
                links = self._links[neighbour][layer]
                links.append(node)
                if len(links) > limit:
                    # The neighbour has too many links now: keep its best ones.
                    ranked = sorted(
                        zip(self._distances(self._vectors[neighbour], links), links, strict=True)
                    )
                    self._links[neighbour][layer] = self._select(ranked, limit)
            nearest = found
        if level > self._top_layer:
            self._entry, self._top_layer = node, level
        return True

    def remove(self, payload: Hashable) -> bool:
        """Mark a vector as deleted. It keeps routing searches but is never returned."""
        node = self._node_of.pop(payload, None)
        if node is None:
            return False
        self._dead[node] = 1
        return True

    # ------------------------------------------------------------------ searching

    def search(self, query: Sequence[float], k: int, ef: int) -> list[Hashable]:
        """Up to k payloads, nearest first. Fewer if the index holds fewer live vectors."""
        if self._entry < 0 or k <= 0:
            return []
        target = self._prepare(query)
        if target is None:
            return []
        nearest = [(self._distances(target, [self._entry])[0], self._entry)]
        for layer in range(self._top_layer, 0, -1):
            nearest = self._search_layer(target, nearest, 1, layer)
        found = self._search_layer(target, nearest, max(ef, k), 0)
        dead, payloads = self._dead, self._payloads
        return [payloads[node] for _d, node in found if not dead[node]][:k]
