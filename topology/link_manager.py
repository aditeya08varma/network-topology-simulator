"""Veth interface creation, subnet allocation, MTU configuration, and
link up/down toggles used for fault-injection tests."""
from __future__ import annotations

import ipaddress
import subprocess
from dataclasses import dataclass, field

from topology.nodes import Interface, Node, Switch

STANDARD_MTU = 1500
JUMBO_MTU = 9000


@dataclass
class Subnet:
    """A CIDR block with sequential host allocation, e.g. 10.0.1.0/24."""

    network: ipaddress.IPv4Network
    _next_host: int = 1

    @classmethod
    def from_cidr(cls, cidr: str) -> "Subnet":
        return cls(network=ipaddress.IPv4Network(cidr))

    def allocate(self) -> ipaddress.IPv4Address:
        addr = self.network[self._next_host]
        if addr not in self.network or addr == self.network.broadcast_address:
            raise ValueError(f"subnet {self.network} exhausted")
        self._next_host += 1
        return addr

    def with_prefixlen(self, addr: ipaddress.IPv4Address) -> str:
        return f"{addr}/{self.network.prefixlen}"


class LinkManager:
    """Creates and tears down veth pairs connecting two namespaces, or a
    namespace and an OVS bridge port."""

    def __init__(self) -> None:
        self._links: list[tuple[str, str]] = []

    def create_veth_pair(self, name_a: str, name_b: str) -> None:
        subprocess.run(
            ["ip", "link", "add", name_a, "type", "veth", "peer", "name", name_b],
            check=True,
        )
        self._links.append((name_a, name_b))

    def move_to_namespace(self, iface_name: str, namespace: str) -> None:
        subprocess.run(["ip", "link", "set", iface_name, "netns", namespace], check=True)

    def attach_link(
        self,
        node_a: Node,
        iface_a: str,
        node_b: Node,
        iface_b: str,
        subnet: Subnet,
        mtu: int = STANDARD_MTU,
    ) -> tuple[str, str]:
        """Wires node_a<->node_b with a veth pair and assigns the next two
        addresses out of `subnet` to each end. Used for router-router links."""
        self.create_veth_pair(iface_a, iface_b)
        self.move_to_namespace(iface_a, node_a.namespace)
        self.move_to_namespace(iface_b, node_b.namespace)

        addr_a = subnet.allocate()
        addr_b = subnet.allocate()

        for node, iface, addr in ((node_a, iface_a, addr_a), (node_b, iface_b, addr_b)):
            node.add_interface(Interface(name=iface, mtu=mtu))
            node.assign_ip(iface, subnet.with_prefixlen(addr))
            node.set_interface_mtu(iface, mtu)
            node.bring_interface_up(iface)

        return str(addr_a), str(addr_b)

    def attach_to_bridge(
        self,
        node: Node,
        iface_node: str,
        bridge_iface: str,
        bridge: Switch,
        subnet: Subnet,
        mtu: int = STANDARD_MTU,
    ) -> str:
        """Wires `node` into an OVS bridge port. Used for host-switch links."""
        self.create_veth_pair(iface_node, bridge_iface)
        self.move_to_namespace(iface_node, node.namespace)
        bridge.add_port(bridge_iface)
        subprocess.run(["ip", "link", "set", bridge_iface, "up"], check=True)

        addr = subnet.allocate()
        node.add_interface(Interface(name=iface_node, mtu=mtu))
        node.assign_ip(iface_node, subnet.with_prefixlen(addr))
        node.set_interface_mtu(iface_node, mtu)
        node.bring_interface_up(iface_node)
        return str(addr)

    def set_link_state(self, node: Node, iface_name: str, up: bool) -> None:
        """Fault injection primitive: toggle a link up/down to test
        route re-convergence."""
        if up:
            node.bring_interface_up(iface_name)
        else:
            node.bring_interface_down(iface_name)

    def teardown_all(self) -> None:
        for name_a, _ in self._links:
            subprocess.run(["ip", "link", "del", name_a], check=False)
        self._links.clear()
