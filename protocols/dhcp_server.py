"""Custom DHCP server: RFC 2131 4-way handshake state machine + lease
allocator, with an ACID (SQLite-transaction backed) lease store.

Packet encode/decode is hand-rolled against the BOOTP/DHCP wire format so
the state machine can be unit tested without root or a real socket — the
protocol handlers are pure functions of (packet in) -> (packet out).
"""
from __future__ import annotations

import enum
import ipaddress
import logging
import socket
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68

# DHCP message type (option 53) values, RFC 2131 section 3.
OP_DHCPDISCOVER = 1
OP_DHCPOFFER = 2
OP_DHCPREQUEST = 3
OP_DHCPDECLINE = 4
OP_DHCPACK = 5
OP_DHCPNAK = 6
OP_DHCPRELEASE = 7

# Option numbers.
OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_END = 255

MAGIC_COOKIE = bytes([99, 130, 83, 99])


class LeaseState(enum.Enum):
    FREE = "free"
    OFFERED = "offered"
    BOUND = "bound"
    RENEWING = "renewing"
    REBINDING = "rebinding"
    EXPIRED = "expired"
    RELEASED = "released"


@dataclass
class Lease:
    mac: str
    ip: str
    state: LeaseState = LeaseState.FREE
    lease_time: int = 3600
    bound_at: float = 0.0

    @property
    def t1(self) -> float:
        """Renewal timer: 50% of the lease elapsed."""
        return self.bound_at + 0.5 * self.lease_time

    @property
    def t2(self) -> float:
        """Rebinding timer: 87.5% of the lease elapsed."""
        return self.bound_at + 0.875 * self.lease_time

    @property
    def expiry(self) -> float:
        return self.bound_at + self.lease_time

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return self.state in (LeaseState.BOUND, LeaseState.RENEWING, LeaseState.REBINDING) and now >= self.expiry


class LeaseStore:
    """ACID lease persistence via sqlite3 transactions. Defaults to an
    in-memory database; pass a file path to persist across restarts."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                mac TEXT PRIMARY KEY,
                ip TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL,
                lease_time INTEGER NOT NULL,
                bound_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def commit_lease(self, lease: Lease) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO leases (mac, ip, state, lease_time, bound_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip=excluded.ip, state=excluded.state,
                    lease_time=excluded.lease_time, bound_at=excluded.bound_at
                """,
                (lease.mac, lease.ip, lease.state.value, lease.lease_time, lease.bound_at),
            )

    def get_by_mac(self, mac: str) -> Optional[Lease]:
        with self._lock:
            row = self._conn.execute(
                "SELECT mac, ip, state, lease_time, bound_at FROM leases WHERE mac=?", (mac,)
            ).fetchone()
        return self._row_to_lease(row) if row else None

    def get_by_ip(self, ip: str) -> Optional[Lease]:
        with self._lock:
            row = self._conn.execute(
                "SELECT mac, ip, state, lease_time, bound_at FROM leases WHERE ip=?", (ip,)
            ).fetchone()
        return self._row_to_lease(row) if row else None

    def all_leases(self) -> list[Lease]:
        with self._lock:
            rows = self._conn.execute("SELECT mac, ip, state, lease_time, bound_at FROM leases").fetchall()
        return [self._row_to_lease(r) for r in rows]

    def release(self, mac: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE leases SET state=? WHERE mac=?", (LeaseState.RELEASED.value, mac))

    @staticmethod
    def _row_to_lease(row) -> Lease:
        mac, ip, state, lease_time, bound_at = row
        return Lease(mac=mac, ip=ip, state=LeaseState(state), lease_time=lease_time, bound_at=bound_at)


@dataclass
class DHCPPacket:
    op: int  # BOOTP op: 1 = BOOTREQUEST (client->server), 2 = BOOTREPLY
    xid: int
    chaddr: str  # MAC as 'aa:bb:cc:dd:ee:ff'
    ciaddr: str = "0.0.0.0"
    yiaddr: str = "0.0.0.0"
    siaddr: str = "0.0.0.0"
    msg_type: int = 0  # DHCP option 53 value
    options: dict[int, bytes] = field(default_factory=dict)


def _mac_to_bytes(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def _mac_from_bytes(b: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in b[:6])


def build_dhcp_packet(pkt: DHCPPacket) -> bytes:
    header = struct.pack(
        "!BBBBI HH 4s4s4s4s 16s 64s 128s",
        pkt.op, 1, 6, 0, pkt.xid,
        0, 0,
        socket.inet_aton(pkt.ciaddr),
        socket.inet_aton(pkt.yiaddr),
        socket.inet_aton(pkt.siaddr),
        socket.inet_aton("0.0.0.0"),
        _mac_to_bytes(pkt.chaddr).ljust(16, b"\x00"),
        b"\x00" * 64,
        b"\x00" * 128,
    )
    options = bytearray(MAGIC_COOKIE)
    options += bytes([OPT_MSG_TYPE, 1, pkt.msg_type])
    for code, value in pkt.options.items():
        options += bytes([code, len(value)]) + value
    options += bytes([OPT_END])
    return header + bytes(options)


def parse_dhcp_packet(data: bytes) -> DHCPPacket:
    op, _htype, _hlen, _hops, xid, _secs, _flags = struct.unpack("!BBBBIHH", data[:12])
    ciaddr = socket.inet_ntoa(data[12:16])
    yiaddr = socket.inet_ntoa(data[16:20])
    siaddr = socket.inet_ntoa(data[20:24])
    chaddr = _mac_from_bytes(data[28:34])

    options_raw = data[240:]
    options: dict[int, bytes] = {}
    msg_type = 0
    i = 0
    while i < len(options_raw):
        code = options_raw[i]
        if code == OPT_END:
            break
        if code == 0:  # padding
            i += 1
            continue
        length = options_raw[i + 1]
        value = bytes(options_raw[i + 2:i + 2 + length])
        if code == OPT_MSG_TYPE:
            msg_type = value[0]
        else:
            options[code] = value
        i += 2 + length

    return DHCPPacket(op=op, xid=xid, chaddr=chaddr, ciaddr=ciaddr, yiaddr=yiaddr, siaddr=siaddr,
                       msg_type=msg_type, options=options)


@dataclass
class DHCPPool:
    network: ipaddress.IPv4Network
    lease_time: int = 3600
    router: str = ""
    dns: str = "8.8.8.8"
    _allocated: set[str] = field(default_factory=set)

    def available_ips(self):
        for host in self.network.hosts():
            addr = str(host)
            if addr not in self._allocated and addr != self.router:
                yield addr

    def reserve(self, ip: str) -> None:
        self._allocated.add(ip)

    def free(self, ip: str) -> None:
        self._allocated.discard(ip)


class DHCPServer:
    """The 4-way handshake state machine. `handle_*` methods are pure
    packet-in/packet-out transforms; `serve_forever` is the thin real-socket
    loop around them (needs root to bind :67)."""

    def __init__(self, pool: DHCPPool, store: Optional[LeaseStore] = None, server_id: str = "10.0.1.1"):
        self.pool = pool
        self.store = store or LeaseStore()
        self.server_id = server_id
        self._sock: Optional[socket.socket] = None

    def handle_discover(self, pkt: DHCPPacket) -> DHCPPacket:
        existing = self.store.get_by_mac(pkt.chaddr)
        if existing and existing.state in (LeaseState.BOUND, LeaseState.OFFERED) and not existing.is_expired():
            offer_ip = existing.ip
        else:
            offer_ip = next(self.pool.available_ips(), None)
            if offer_ip is None:
                raise RuntimeError("DHCP pool exhausted")

        lease = Lease(mac=pkt.chaddr, ip=offer_ip, state=LeaseState.OFFERED,
                       lease_time=self.pool.lease_time, bound_at=time.time())
        self.store.commit_lease(lease)
        self.pool.reserve(offer_ip)
        return self._build_reply(pkt, OP_DHCPOFFER, offer_ip)

    def handle_request(self, pkt: DHCPPacket) -> DHCPPacket:
        requested_raw = pkt.options.get(OPT_REQUESTED_IP)
        requested_ip = socket.inet_ntoa(requested_raw) if requested_raw else pkt.ciaddr

        conflicting = self.store.get_by_ip(requested_ip)
        if conflicting and conflicting.mac != pkt.chaddr and not conflicting.is_expired():
            return self._build_reply(pkt, OP_DHCPNAK, "0.0.0.0")

        lease = self.store.get_by_mac(pkt.chaddr)
        if lease is None or lease.ip != requested_ip:
            lease = Lease(mac=pkt.chaddr, ip=requested_ip, lease_time=self.pool.lease_time)

        lease.state = LeaseState.BOUND
        lease.bound_at = time.time()
        self.store.commit_lease(lease)
        self.pool.reserve(requested_ip)
        return self._build_reply(pkt, OP_DHCPACK, requested_ip)

    def handle_release(self, pkt: DHCPPacket) -> None:
        lease = self.store.get_by_mac(pkt.chaddr)
        self.store.release(pkt.chaddr)
        if lease:
            self.pool.free(lease.ip)

    def sweep_expired(self) -> list[Lease]:
        """Releases any bound lease whose expiry has passed. Call this
        periodically (e.g. from a timer loop alongside serve_forever)."""
        expired = []
        for lease in self.store.all_leases():
            if lease.is_expired():
                lease.state = LeaseState.EXPIRED
                self.store.commit_lease(lease)
                self.pool.free(lease.ip)
                expired.append(lease)
        return expired

    def _build_reply(self, request: DHCPPacket, msg_type: int, your_ip: str) -> DHCPPacket:
        options = {
            OPT_SUBNET_MASK: socket.inet_aton(str(self.pool.network.netmask)),
            OPT_ROUTER: socket.inet_aton(self.pool.router or "0.0.0.0"),
            OPT_DNS: socket.inet_aton(self.pool.dns),
            OPT_LEASE_TIME: struct.pack("!I", self.pool.lease_time),
            OPT_SERVER_ID: socket.inet_aton(self.server_id),
        }
        return DHCPPacket(
            op=2, xid=request.xid, chaddr=request.chaddr,
            yiaddr=your_ip if msg_type != OP_DHCPNAK else "0.0.0.0",
            siaddr=self.server_id, msg_type=msg_type, options=options,
        )

    def serve_forever(self, bind_addr: str = "0.0.0.0") -> None:
        """Real UDP:67 listener. Requires root (privileged port) and should
        be run inside the DHCPD node's network namespace."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind((bind_addr, DHCP_SERVER_PORT))
        logger.info("DHCP server listening on %s:%d", bind_addr, DHCP_SERVER_PORT)
        try:
            while True:
                data, _addr = self._sock.recvfrom(4096)
                self._dispatch(data)
        finally:
            self._sock.close()

    def _dispatch(self, data: bytes) -> None:
        pkt = parse_dhcp_packet(data)
        reply: Optional[DHCPPacket] = None
        if pkt.msg_type == OP_DHCPDISCOVER:
            reply = self.handle_discover(pkt)
        elif pkt.msg_type == OP_DHCPREQUEST:
            reply = self.handle_request(pkt)
        elif pkt.msg_type == OP_DHCPRELEASE:
            self.handle_release(pkt)

        if reply and self._sock:
            self._sock.sendto(build_dhcp_packet(reply), ("255.255.255.255", DHCP_CLIENT_PORT))
