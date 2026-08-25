"""MTU/PMTUD fragmentation handling: standard vs. jumbo frame constants,
plus a root-gated integration test that verifies oversized DF-set packets
trigger an ICMP Type 3, Code 4 (Fragmentation Needed) reply."""
from __future__ import annotations

from conftest import requires_root_linux

from topology.link_manager import JUMBO_MTU, STANDARD_MTU


# Checks that the standard and jumbo MTU constants have the right values.
def test_mtu_constants():
    assert STANDARD_MTU == 1500
    assert JUMBO_MTU == 9000


# Checks that a packet too big for the link gets an ICMP fragmentation-needed reply.
@requires_root_linux
def test_oversized_df_packet_triggers_fragmentation_needed(built_topology):
    from telemetry.packet_collector import PacketCapture, find_fragmentation_needed, ping

    hosts = built_topology.nodes
    r1 = hosts["R1"]
    h1 = hosts["H1"]
    r2_ip = str(hosts["R2"].interfaces["r2-r1"].ip.ip)

    r1.set_interface_mtu("r1-r2", 1400)  # deliberately below the 1500 default

    with PacketCapture(interface="r1-r2", output_file="/tmp/pmtud.pcap", namespace=r1.namespace):
        ping(h1.namespace, r2_ip, df=True, size=1472)  # 1472 + 28-byte ICMP/IP header = 1500 > 1400 MTU

    frag_needed = find_fragmentation_needed("/tmp/pmtud.pcap")
    assert frag_needed, "expected an ICMP fragmentation-needed reply"
