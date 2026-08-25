"""Programmatic topology builder: wires up the R1/R2/SW1/H1/H2/DHCPD lab
topology described in the project README, entirely via `ip netns` + OVS.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from topology.link_manager import STANDARD_MTU, LinkManager, Subnet
from topology.nodes import Host, Node, Router, Switch

logger = logging.getLogger(__name__)


@dataclass
class TopologyController:
    """Deploys and tears down the lab topology:

        R1 --(veth, Subnet A)-- SW1 (OVS) --(veth)-- H1
                                          `--(veth)-- H2
        R1 --(veth, Subnet B)-- R2 --(veth, Subnet C)-- DHCPD
    """

    subnet_a: str = "10.0.1.0/24"  # R1 <-> SW1 <-> {H1, H2}
    subnet_b: str = "10.0.2.0/24"  # R1 <-> R2 (inter-router link)
    subnet_c: str = "10.0.3.0/24"  # R2 <-> DHCP server

    link_manager: LinkManager = field(default_factory=LinkManager)
    nodes: dict[str, Node] = field(default_factory=dict)
    switches: dict[str, Switch] = field(default_factory=dict)
    graph_edges: list[tuple[str, str, float]] = field(default_factory=list)

    # Creates every namespace, the switch, and all the links that make up the lab topology.
    def build(self) -> None:
        r1 = Router(name="R1", namespace="ns-r1")
        r2 = Router(name="R2", namespace="ns-r2")
        h1 = Host(name="H1", namespace="ns-h1")
        h2 = Host(name="H2", namespace="ns-h2")
        dhcpd = Router(name="DHCPD", namespace="ns-dhcpd")
        sw1 = Switch(name="sw1")

        for node in (r1, r2, h1, h2, dhcpd):
            node.create_namespace()
        sw1.create()

        r1.enable_ip_forwarding()
        r2.enable_ip_forwarding()

        subnet_a = Subnet.from_cidr(self.subnet_a)
        subnet_b = Subnet.from_cidr(self.subnet_b)
        subnet_c = Subnet.from_cidr(self.subnet_c)

        self.link_manager.attach_to_bridge(r1, "r1-sw1", "sw1-r1", sw1, subnet_a, mtu=STANDARD_MTU)
        self.link_manager.attach_to_bridge(h1, "h1-eth0", "sw1-h1", sw1, subnet_a, mtu=STANDARD_MTU)
        self.link_manager.attach_to_bridge(h2, "h2-eth0", "sw1-h2", sw1, subnet_a, mtu=STANDARD_MTU)

        self.link_manager.attach_link(r1, "r1-r2", r2, "r2-r1", subnet_b, mtu=STANDARD_MTU)
        self.link_manager.attach_link(r2, "r2-dhcpd", dhcpd, "dhcpd-r2", subnet_c, mtu=STANDARD_MTU)

        self.nodes = {"R1": r1, "R2": r2, "H1": h1, "H2": h2, "DHCPD": dhcpd}
        self.switches = {"sw1": sw1}
        self.graph_edges = [("R1", "R2", 1.0)]

        logger.info("topology built: %s", list(self.nodes))

    # Turns the physical links into link-state advertisements a routing engine can use.
    def lsas(self):
        """Converts the physical topology into initial link-state
        advertisements, ready to hand to a RoutingEngine per router."""
        from protocols.routing_engine import LinkStateAdvertisement

        adjacency: dict[str, dict[str, float]] = {}
        for a, b, cost in self.graph_edges:
            adjacency.setdefault(a, {})[b] = cost
            adjacency.setdefault(b, {})[a] = cost
        return [LinkStateAdvertisement(router_id=r, neighbors=n, sequence=1) for r, n in adjacency.items()]

    # Deletes every namespace and switch created for the topology.
    def teardown(self) -> None:
        for node in self.nodes.values():
            node.delete_namespace()
        for switch in self.switches.values():
            switch.delete()
        self.link_manager.teardown_all()
