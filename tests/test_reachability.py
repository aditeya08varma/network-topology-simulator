"""Subnet allocation logic, plus an end-to-end ping-mesh integration test
gated behind root+Linux since it stands up real network namespaces."""
from __future__ import annotations

from conftest import requires_root_linux

from topology.link_manager import Subnet


def test_subnet_allocates_sequential_hosts():
    subnet = Subnet.from_cidr("10.0.1.0/24")
    a = subnet.allocate()
    b = subnet.allocate()
    assert str(a) == "10.0.1.1"
    assert str(b) == "10.0.1.2"
    assert a in subnet.network and b in subnet.network


def test_subnet_with_prefixlen_formats_cidr():
    subnet = Subnet.from_cidr("10.0.2.0/24")
    addr = subnet.allocate()
    assert subnet.with_prefixlen(addr) == "10.0.2.1/24"


def test_subnet_raises_when_exhausted():
    import pytest

    subnet = Subnet.from_cidr("10.0.9.0/30")  # only .1 and .2 are usable hosts
    subnet.allocate()
    subnet.allocate()
    with pytest.raises(ValueError):
        subnet.allocate()


@requires_root_linux
def test_full_mesh_reachability(built_topology):
    """Brings up the real topology and pings every host pair across the
    shared subnet."""
    from telemetry.packet_collector import ping

    hosts = built_topology.nodes
    h1_ip = str(hosts["H1"].interfaces["h1-eth0"].ip.ip)
    h2_ip = str(hosts["H2"].interfaces["h2-eth0"].ip.ip)

    assert ping(hosts["H1"].namespace, h2_ip)
    assert ping(hosts["H2"].namespace, h1_ip)
