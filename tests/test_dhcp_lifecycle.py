"""Discover -> Offer -> Request -> Ack flow, lease collision, and expiration.

Runs entirely in-process via LoopbackTransport, so no root/socket access
is needed. The DHCP state machine is exercised directly.
"""
from __future__ import annotations

import ipaddress
import socket
import time

from protocols.dhcp_client import DHCPClient, LoopbackTransport
from protocols.dhcp_server import (
    DHCPPacket,
    DHCPPool,
    DHCPServer,
    LeaseState,
    OPT_REQUESTED_IP,
    OP_DHCPNAK,
    OP_DHCPREQUEST,
)


# Creates a DHCP server with a small test address pool.
def make_server(lease_time: int = 3600) -> DHCPServer:
    pool = DHCPPool(network=ipaddress.IPv4Network("10.0.1.0/24"), lease_time=lease_time, router="10.0.1.1")
    return DHCPServer(pool, server_id="10.0.1.1")


# Checks that a client can complete the full handshake and end up with a bound lease.
def test_discover_offer_request_ack_flow():
    server = make_server()
    client = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))

    lease = client.run_handshake()

    assert lease is not None
    assert lease.ip.startswith("10.0.1.")
    assert lease.router == "10.0.1.1"
    stored = server.store.get_by_mac(client.mac)
    assert stored.state == LeaseState.BOUND


# Checks that two different clients never get handed the same address.
def test_two_clients_receive_distinct_ips():
    server = make_server()
    client1 = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))
    client2 = DHCPClient(mac="aa:bb:cc:dd:ee:02", transport=LoopbackTransport(server))

    lease1 = client1.run_handshake()
    lease2 = client2.run_handshake()

    assert lease1 is not None and lease2 is not None
    assert lease1.ip != lease2.ip


# Checks that the server refuses a request for an address someone else already holds.
def test_lease_collision_is_nakd():
    server = make_server()
    client1 = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))
    lease1 = client1.run_handshake()
    assert lease1 is not None

    # A rogue second client crafts a REQUEST for the IP already bound to client1.
    rogue_request = DHCPPacket(
        op=1, xid=999, chaddr="aa:bb:cc:dd:ee:99", msg_type=OP_DHCPREQUEST,
        options={OPT_REQUESTED_IP: socket.inet_aton(lease1.ip)},
    )
    reply = server.handle_request(rogue_request)
    assert reply.msg_type == OP_DHCPNAK


# Checks that a lease's address is freed once it expires and nobody renews it.
def test_lease_expiration_releases_ip():
    server = make_server(lease_time=1)  # 1-second lease for a fast test
    client = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))
    lease = client.run_handshake()
    assert lease is not None
    leased_ip = lease.ip

    # Simulate the client going dark (e.g. powered off) so it never sends
    # the T1/T2 renewal REQUESTs that would otherwise keep the lease alive.
    client._cancel_timers()

    time.sleep(1.2)
    expired = server.sweep_expired()

    assert len(expired) == 1
    assert expired[0].mac == client.mac
    assert leased_ip not in server.pool._allocated


# Checks that releasing a lease frees its address.
def test_release_frees_the_lease():
    server = make_server()
    client = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))
    lease = client.run_handshake()
    assert lease is not None
    leased_ip = lease.ip

    server.handle_release(DHCPPacket(op=1, xid=1, chaddr=client.mac, msg_type=7))

    assert leased_ip not in server.pool._allocated
    assert server.store.get_by_mac(client.mac).state == LeaseState.RELEASED


# Checks that the renewal and rebind timers land at the right percentages of the lease.
def test_renewal_timers_reflect_t1_and_t2():
    server = make_server(lease_time=1000)
    client = DHCPClient(mac="aa:bb:cc:dd:ee:01", transport=LoopbackTransport(server))
    lease = client.run_handshake()
    assert lease is not None

    assert lease.t1 == lease.bound_at + 500
    assert lease.t2 == lease.bound_at + 875
    assert lease.expiry == lease.bound_at + 1000
