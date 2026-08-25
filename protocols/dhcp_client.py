"""Emulated DHCP client: runs the Discover -> Offer -> Request -> Ack
handshake over a pluggable Transport, and manages T1/T2/expiry renewal
timers once bound.

The Transport abstraction lets the exact same handshake/timer logic run
against a real UDP broadcast socket (needs root + a namespace) or against
an in-process LoopbackTransport wired directly to a DHCPServer, which is
what the test suite uses.
"""
from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from protocols.dhcp_server import (
    DHCP_CLIENT_PORT,
    DHCP_SERVER_PORT,
    DHCPPacket,
    OPT_LEASE_TIME,
    OPT_REQUESTED_IP,
    OPT_ROUTER,
    OPT_SUBNET_MASK,
    OP_DHCPACK,
    OP_DHCPDISCOVER,
    OP_DHCPNAK,
    OP_DHCPOFFER,
    OP_DHCPRELEASE,
    OP_DHCPREQUEST,
    build_dhcp_packet,
    parse_dhcp_packet,
)

logger = logging.getLogger(__name__)


class Transport(Protocol):
    # Sends raw bytes out, however the specific transport does that.
    def send(self, data: bytes) -> None: ...
    # Waits for raw bytes to arrive, however the specific transport does that.
    def recv(self, timeout: float) -> Optional[bytes]: ...


class UDPBroadcastTransport:
    """Real transport: broadcasts DISCOVER/REQUEST to 255.255.255.255:67
    and listens on :68. Requires root (privileged port) and should run
    inside the client host's network namespace."""

    # Opens a real UDP socket that can send and receive DHCP traffic.
    def __init__(self, iface: Optional[str] = None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if iface is not None and hasattr(socket, "SO_BINDTODEVICE"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        self._sock.bind(("0.0.0.0", DHCP_CLIENT_PORT))

    # Broadcasts a packet to the DHCP server port.
    def send(self, data: bytes) -> None:
        self._sock.sendto(data, ("255.255.255.255", DHCP_SERVER_PORT))

    # Waits for a reply packet, giving up after the timeout.
    def recv(self, timeout: float) -> Optional[bytes]:
        self._sock.settimeout(timeout)
        try:
            data, _addr = self._sock.recvfrom(4096)
            return data
        except socket.timeout:
            return None


class LoopbackTransport:
    """In-process transport used by tests: wires a DHCPClient directly to a
    DHCPServer instance's handlers, bypassing real sockets entirely."""

    # Connects this transport directly to a DHCPServer object instead of a real socket.
    def __init__(self, server) -> None:
        self._server = server
        self._inbox: list[bytes] = []

    # Hands a packet straight to the server's handler and stores the reply.
    def send(self, data: bytes) -> None:
        pkt = parse_dhcp_packet(data)
        reply = None
        if pkt.msg_type == OP_DHCPDISCOVER:
            reply = self._server.handle_discover(pkt)
        elif pkt.msg_type == OP_DHCPREQUEST:
            reply = self._server.handle_request(pkt)
        elif pkt.msg_type == OP_DHCPRELEASE:
            self._server.handle_release(pkt)
        if reply is not None:
            self._inbox.append(build_dhcp_packet(reply))

    # Returns the next reply that's waiting, if there is one.
    def recv(self, timeout: float) -> Optional[bytes]:
        if self._inbox:
            return self._inbox.pop(0)
        return None


# Reads the lease time number out of a DHCP option's raw bytes.
def _unpack_lease_time(raw: Optional[bytes]) -> int:
    return struct.unpack("!I", raw)[0] if raw else 3600


@dataclass
class LeaseInfo:
    ip: str
    subnet_mask: str
    router: str
    lease_time: int
    bound_at: float = field(default_factory=time.time)

    # Works out when this client should try to renew its lease.
    @property
    def t1(self) -> float:
        return self.bound_at + 0.5 * self.lease_time

    # Works out when this client should try to rebind its lease.
    @property
    def t2(self) -> float:
        return self.bound_at + 0.875 * self.lease_time

    # Works out when this lease stops being valid.
    @property
    def expiry(self) -> float:
        return self.bound_at + self.lease_time


class DHCPClient:
    # Sets up a new client with its MAC address and how it will talk to the server.
    def __init__(self, mac: str, transport: Transport, retries: int = 3, timeout: float = 2.0):
        self.mac = mac
        self.transport = transport
        self.retries = retries
        self.timeout = timeout
        self.lease: Optional[LeaseInfo] = None

        self._renew_timer: Optional[threading.Timer] = None
        self._rebind_timer: Optional[threading.Timer] = None
        self._expire_timer: Optional[threading.Timer] = None

        self.on_bound: Optional[Callable[[LeaseInfo], None]] = None
        self.on_expired: Optional[Callable[[], None]] = None

    # Runs the full Discover, Offer, Request, Ack handshake to get an IP address.
    def run_handshake(self) -> Optional[LeaseInfo]:
        xid = random.getrandbits(32)
        discover = DHCPPacket(op=1, xid=xid, chaddr=self.mac, msg_type=OP_DHCPDISCOVER)
        offer = self._send_and_wait(discover, expect_types=(OP_DHCPOFFER,))
        if offer is None:
            logger.warning("no DHCPOFFER received for %s", self.mac)
            return None

        request = DHCPPacket(
            op=1, xid=xid, chaddr=self.mac, msg_type=OP_DHCPREQUEST,
            options={OPT_REQUESTED_IP: socket.inet_aton(offer.yiaddr)},
        )
        ack = self._send_and_wait(request, expect_types=(OP_DHCPACK, OP_DHCPNAK))
        if ack is None or ack.msg_type == OP_DHCPNAK:
            logger.warning("DHCPREQUEST rejected for %s", self.mac)
            return None

        self.lease = LeaseInfo(
            ip=ack.yiaddr,
            subnet_mask=socket.inet_ntoa(ack.options[OPT_SUBNET_MASK]) if OPT_SUBNET_MASK in ack.options else "255.255.255.0",
            router=socket.inet_ntoa(ack.options[OPT_ROUTER]) if OPT_ROUTER in ack.options else "",
            lease_time=_unpack_lease_time(ack.options.get(OPT_LEASE_TIME)),
        )
        self._schedule_timers()
        if self.on_bound:
            self.on_bound(self.lease)
        return self.lease

    # Sends a packet and waits for a matching reply, retrying a few times if needed.
    def _send_and_wait(self, pkt: DHCPPacket, expect_types: tuple[int, ...]) -> Optional[DHCPPacket]:
        for _ in range(self.retries):
            self.transport.send(build_dhcp_packet(pkt))
            raw = self.transport.recv(self.timeout)
            if raw is None:
                continue
            reply = parse_dhcp_packet(raw)
            if reply.xid == pkt.xid and reply.msg_type in expect_types:
                return reply
        return None

    # Sets alarms for when to renew, rebind, and expire the current lease.
    def _schedule_timers(self) -> None:
        self._cancel_timers()
        if not self.lease:
            return
        now = time.time()
        self._renew_timer = threading.Timer(max(self.lease.t1 - now, 0), self._renew)
        self._rebind_timer = threading.Timer(max(self.lease.t2 - now, 0), self._rebind)
        self._expire_timer = threading.Timer(max(self.lease.expiry - now, 0), self._expire)
        for t in (self._renew_timer, self._rebind_timer, self._expire_timer):
            t.daemon = True
            t.start()

    # Tries to renew the current lease before it runs out.
    def _renew(self) -> None:
        if not self.lease:
            return
        xid = random.getrandbits(32)
        request = DHCPPacket(
            op=1, xid=xid, chaddr=self.mac, ciaddr=self.lease.ip, msg_type=OP_DHCPREQUEST,
            options={OPT_REQUESTED_IP: socket.inet_aton(self.lease.ip)},
        )
        ack = self._send_and_wait(request, expect_types=(OP_DHCPACK, OP_DHCPNAK))
        if ack and ack.msg_type == OP_DHCPACK:
            self.lease.bound_at = time.time()
            self._schedule_timers()

    # Tries again to renew the lease, this time by asking any server.
    def _rebind(self) -> None:
        self._renew()

    # Gives up the lease because renewing it never worked.
    def _expire(self) -> None:
        self.lease = None
        if self.on_expired:
            self.on_expired()

    # Tells the server this client is done with its address and cancels the timers.
    def release(self) -> None:
        if not self.lease:
            return
        release_pkt = DHCPPacket(op=1, xid=random.getrandbits(32), chaddr=self.mac,
                                  ciaddr=self.lease.ip, msg_type=OP_DHCPRELEASE)
        self.transport.send(build_dhcp_packet(release_pkt))
        self._cancel_timers()
        self.lease = None

    # Stops all the renewal, rebind, and expiry alarms.
    def _cancel_timers(self) -> None:
        for t in (self._renew_timer, self._rebind_timer, self._expire_timer):
            if t:
                t.cancel()
