"""Link-state routing: LSA exchange, Dijkstra shortest-path resolution,
and kernel route table programming.

The graph/Dijkstra/RoutingEngine layer is pure Python and fully unit
testable; only KernelRouteInjector shells out (`ip route replace`), and
that's isolated to a thin, mockable wrapper.
"""
from __future__ import annotations

import heapq
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LinkStateAdvertisement:
    router_id: str
    neighbors: dict[str, float]  # neighbor_id -> link cost
    sequence: int = 0


class TopologyGraph:
    """Adjacency-list graph assembled from received LSAs."""

    def __init__(self) -> None:
        self._adj: dict[str, dict[str, float]] = {}
        self._lsdb: dict[str, LinkStateAdvertisement] = {}

    def receive_lsa(self, lsa: LinkStateAdvertisement) -> bool:
        """Installs an LSA if it's newer than what we have for that router.
        Returns True if the topology changed (routes need recomputing)."""
        existing = self._lsdb.get(lsa.router_id)
        if existing and existing.sequence >= lsa.sequence:
            return False
        self._lsdb[lsa.router_id] = lsa
        self._adj[lsa.router_id] = dict(lsa.neighbors)
        return True

    def add_link(self, a: str, b: str, cost: float = 1) -> None:
        self._adj.setdefault(a, {})[b] = cost
        self._adj.setdefault(b, {})[a] = cost

    def remove_link(self, a: str, b: str) -> None:
        self._adj.get(a, {}).pop(b, None)
        self._adj.get(b, {}).pop(a, None)

    def neighbors(self, node: str) -> dict[str, float]:
        return self._adj.get(node, {})

    def nodes(self) -> list[str]:
        return list(self._adj.keys())


def dijkstra(graph: TopologyGraph, source: str) -> tuple[dict[str, float], dict[str, Optional[str]]]:
    """Standard Dijkstra shortest-path over the topology graph. Returns
    (distances, predecessors) rooted at `source`."""
    distances: dict[str, float] = {source: 0}
    predecessors: dict[str, Optional[str]] = {source: None}
    visited: set[str] = set()
    heap: list[tuple[float, str]] = [(0, source)]

    while heap:
        dist, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        for neighbor, cost in graph.neighbors(node).items():
            new_dist = dist + cost
            if neighbor not in distances or new_dist < distances[neighbor]:
                distances[neighbor] = new_dist
                predecessors[neighbor] = node
                heapq.heappush(heap, (new_dist, neighbor))

    return distances, predecessors


def build_routing_table(graph: TopologyGraph, source: str) -> dict[str, str]:
    """Returns {destination: next_hop} for every node reachable from source."""
    distances, predecessors = dijkstra(graph, source)
    table: dict[str, str] = {}
    for dest in distances:
        if dest == source:
            continue
        hop = dest
        while predecessors[hop] != source and predecessors[hop] is not None:
            hop = predecessors[hop]
        table[dest] = hop
    return table


@dataclass
class ConvergenceResult:
    changed: bool
    duration_seconds: float
    routing_table: dict[str, str]


class RoutingEngine:
    """Maintains one router's link-state database and recomputes Dijkstra
    routes whenever a new LSA arrives or a directly-connected link fails."""

    def __init__(self, router_id: str):
        self.router_id = router_id
        self.graph = TopologyGraph()
        self.routing_table: dict[str, str] = {}

    def install_lsa(self, lsa: LinkStateAdvertisement) -> ConvergenceResult:
        start = time.perf_counter()
        changed = self.graph.receive_lsa(lsa)
        if changed:
            self.routing_table = build_routing_table(self.graph, self.router_id)
        return ConvergenceResult(changed, time.perf_counter() - start, dict(self.routing_table))

    def handle_link_failure(self, neighbor: str) -> ConvergenceResult:
        start = time.perf_counter()
        self.graph.remove_link(self.router_id, neighbor)
        self.routing_table = build_routing_table(self.graph, self.router_id)
        duration = time.perf_counter() - start
        logger.info("router %s reconverged in %.4fs after losing link to %s", self.router_id, duration, neighbor)
        return ConvergenceResult(True, duration, dict(self.routing_table))


class KernelRouteInjector:
    """Programs computed routes into the Linux kernel routing table of a
    given network namespace via `ip route replace`."""

    def __init__(self, namespace: Optional[str] = None):
        self.namespace = namespace

    def _base_cmd(self) -> list[str]:
        if self.namespace:
            return ["ip", "netns", "exec", self.namespace, "ip", "route", "replace"]
        return ["ip", "route", "replace"]

    def inject(self, destination_cidr: str, via: str, dev: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = self._base_cmd() + [destination_cidr, "via", via]
        if dev:
            cmd += ["dev", dev]
        logger.debug("injecting route: %s", " ".join(cmd))
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    def inject_table(self, routes: dict[str, tuple[str, Optional[str]]]) -> None:
        """routes: {destination_cidr: (via, dev)}"""
        for dest, (via, dev) in routes.items():
            self.inject(dest, via, dev)
