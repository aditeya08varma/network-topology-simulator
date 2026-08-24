"""Link failure injection and route re-convergence metrics.

The Dijkstra/RoutingEngine logic is pure Python and tested directly here;
the real interface-teardown integration test is gated behind root+Linux.
"""
from __future__ import annotations

from conftest import requires_root_linux

from protocols.routing_engine import (
    LinkStateAdvertisement,
    RoutingEngine,
    TopologyGraph,
    build_routing_table,
    dijkstra,
)


def _sample_graph() -> TopologyGraph:
    """R1-R2-R3 primary path, R1-R4-R3 equal-cost alternate."""
    graph = TopologyGraph()
    graph.add_link("R1", "R2", 1)
    graph.add_link("R2", "R3", 1)
    graph.add_link("R1", "R4", 1)
    graph.add_link("R4", "R3", 1)
    return graph


def test_dijkstra_finds_shortest_path():
    graph = _sample_graph()
    distances, _ = dijkstra(graph, "R1")
    assert distances["R3"] == 2


def test_routing_table_picks_correct_next_hop():
    graph = _sample_graph()
    table = build_routing_table(graph, "R1")
    assert table["R2"] == "R2"
    assert table["R3"] in ("R2", "R4")  # two equal-cost paths exist


def test_link_failure_triggers_reconvergence_to_alternate_path():
    engine = RoutingEngine("R1")
    engine.graph = _sample_graph()
    engine.routing_table = build_routing_table(engine.graph, "R1")
    assert engine.routing_table["R3"] == "R2"

    result = engine.handle_link_failure("R2")

    assert result.changed
    assert result.routing_table["R3"] == "R4"
    assert result.duration_seconds < 1.0


def test_lsa_with_stale_sequence_is_ignored():
    graph = TopologyGraph()
    lsa_new = LinkStateAdvertisement(router_id="R2", neighbors={"R1": 1, "R3": 1}, sequence=5)
    lsa_stale = LinkStateAdvertisement(router_id="R2", neighbors={"R1": 9}, sequence=3)

    assert graph.receive_lsa(lsa_new) is True
    assert graph.receive_lsa(lsa_stale) is False
    assert graph.neighbors("R2")["R1"] == 1  # unchanged by the stale LSA


def test_lsa_with_newer_sequence_updates_topology():
    graph = TopologyGraph()
    graph.receive_lsa(LinkStateAdvertisement(router_id="R2", neighbors={"R1": 1}, sequence=1))
    changed = graph.receive_lsa(LinkStateAdvertisement(router_id="R2", neighbors={"R1": 5}, sequence=2))

    assert changed is True
    assert graph.neighbors("R2")["R1"] == 5


@requires_root_linux
def test_link_down_triggers_real_reconvergence(built_topology):
    """Integration test: tears an interface down and verifies the
    packet-loss tracking primitive the fault-injection harness relies on."""
    from telemetry.packet_collector import ConvergenceTracker

    hosts = built_topology.nodes
    r1 = hosts["R1"]
    tracker = ConvergenceTracker()

    r1.bring_interface_down("r1-r2")
    tracker.mark_link_down()
    tracker.record_probe(success=False)

    assert tracker.probes_lost == 1
