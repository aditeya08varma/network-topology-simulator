# Project Explanation

This doc explains what this simulator does, what technologies it uses,
and how each piece actually works.

---

## 1. What this project is

This project is a software-only network lab. Instead of buying routers
and switches, it uses Linux kernel features to create isolated virtual
network namespaces. These namespaces behave like real hosts and routers.
The project wires them together with virtual cables, then runs two real
network protocols against that fake topology: a DHCP server and client,
and a link-state routing protocol. This lets you test protocol behavior,
failure recovery, and MTU edge cases the same way you would on physical
gear, without needing any physical gear.

```
                      [Virtual Topology Controller]
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
  [Router R1 (Netns)] ── (Veth Pair / Subnet A) ── [Switch SW1 (Open vSwitch)]
         │                                                   ├── [Host H1 (DHCP Client)]
         │ (Routing Protocol / Dijkstra)                     └── [Host H2 (DHCP Client)]
         ▼
  [Router R2 (Netns)] ── (Veth Pair / Subnet B) ── [DHCP Server Daemon (Port 67)]
```

Three things make this more than just a topology script:

1. The DHCP server and client are hand-written protocol implementations.
   They are not wrappers around `dnsmasq`. The actual DISCOVER, OFFER,
   REQUEST, and ACK wire format is encoded and decoded by hand.
2. Routing is computed, not configured. Routers exchange link-state
   advertisements and run Dijkstra themselves, the same way OSPF does,
   instead of having static routes typed in.
3. The protocol logic is kept separate from the OS layer on purpose. This
   means the interesting parts, the state machines, can be unit tested on
   any machine. The networking parts, namespaces, OVS, and real sockets,
   are isolated behind thin wrappers and only run on Linux as root.

---

## 2. Core technologies, explained

### Linux network namespaces (`ip netns`)

A network namespace is a kernel feature that gives a process its own
completely separate networking stack. It gets its own interfaces, its
own routing table, its own firewall rules, and its own port space. Two
processes in different namespaces can both bind port 67 without
conflicting, because as far as the kernel is concerned, they are on
different machines.

This project uses one namespace per simulated node: `ns-r1`, `ns-r2`,
`ns-h1`, `ns-h2`, and `ns-dhcpd`. Every command that should run on a
given node, such as assigning an IP, bringing up an interface, or
running `ping`, gets wrapped in `ip netns exec <namespace> <command>`.
See [`topology/nodes.py`](topology/nodes.py)'s `Node.exec()`. Every
other namespace-aware method calls through this one function.

### Veth pairs (virtual ethernet)

A veth pair is two virtual network interfaces that stay permanently
connected to each other. Anything sent into one end comes out the other
end, like a virtual patch cable. To connect two namespaces, you first
create a veth pair outside any namespace, then move one end into each
namespace with `ip link set <iface> netns <namespace>`.

[`topology/link_manager.py`](topology/link_manager.py)'s
`LinkManager.attach_link()` does this for router-to-router links.
`attach_to_bridge()` does the same thing for host-to-switch links: one
end goes into the host's namespace, and the other becomes a port on the
OVS bridge.

### Open vSwitch (OVS)

OVS is a software switch. It is a userspace and kernel hybrid
implementation of what a physical Ethernet switch does: it learns MAC
addresses and forwards traffic between ports on the same broadcast
domain. `SW1` in the topology is an OVS bridge, created with `ovs-vsctl
add-br`. Each host or router gets a veth end added as a bridge port with
`ovs-vsctl add-port`. This setup lets H1 and H2 sit on the same subnet
and reach each other directly, without going through a router.

### IP forwarding (`sysctl net.ipv4.ip_forward=1`)

By default, a Linux box drops packets that arrive on one interface
addressed to a different subnet than its own. It only acts as a host,
not a router. Setting `net.ipv4.ip_forward=1` inside a namespace turns
that namespace into a router. It will now forward packets between its
interfaces based on its routing table.
`Router.enable_ip_forwarding()` in
[`topology/nodes.py`](topology/nodes.py) sets this for R1 and R2. This
is what makes them act as actual routers, instead of just hosts with
more than one interface.

### MTU and jumbo frames

MTU, or Maximum Transmission Unit, is the largest packet size an
interface will send without breaking it into fragments. Standard
Ethernet uses 1500 bytes. "Jumbo frames," commonly 9000 bytes, trade
compatibility for less overhead per packet, on links that support them.
This project lets you set MTU per interface with
`Node.set_interface_mtu()`. This exists specifically so the test suite
can deliberately create an MTU mismatch between two hops, which is what
makes the PMTUD tests possible. See below.

### DHCP: the 4-way handshake, from bytes up

DHCP packets are BOOTP packets (RFC 951) with an options block added at
the end. RFC 2131 layers DHCP behavior on top of that older format. The
fixed header is 236 bytes long, laid out as: `op, htype, hlen, hops,
xid, secs, flags, ciaddr, yiaddr, siaddr, giaddr, chaddr[16], sname[64],
file[128]`. After that comes a 4-byte "magic cookie" (`99.130.83.99`),
followed by a sequence of `(option_code, length, value)` groups that end
with option 255.

[`protocols/dhcp_server.py`](protocols/dhcp_server.py) implements
`build_dhcp_packet()` and `parse_dhcp_packet()` against this exact
layout, using Python's `struct` module. It does not use a packet
library or scapy. This was a deliberate choice. It keeps the DHCP logic
free of dependencies, and more importantly, it means the entire
handshake can be tested by building and parsing byte strings directly,
without ever opening a socket.

The handshake itself works like this:

1. **DHCPDISCOVER.** The client broadcasts a request asking if anyone
   has an IP for it, using its MAC address as the only identifying
   information.
2. **DHCPOFFER.** The server picks a free IP from its pool, reserves it
   provisionally as `LeaseState.OFFERED`, and replies with that IP, plus
   Option 1 (subnet mask), Option 3 (router/gateway), Option 6 (DNS),
   and Option 51 (lease time).
3. **DHCPREQUEST.** The client says it will take that IP. This step
   exists so that if two servers offered the same client an IP, only one
   offer gets accepted.
4. **DHCPACK.** The server commits the lease as `LeaseState.BOUND` and
   confirms it to the client.

Each of these steps is a pure function inside `DHCPServer`, for example
`handle_discover(pkt) -> DHCPPacket` and `handle_request(pkt) ->
DHCPPacket`. Each one takes a parsed packet in and returns a parsed
packet out, with no I/O involved. The only place a real socket shows up
is `serve_forever()`, a thin loop that reads bytes, parses them, calls
the right handler, and sends the reply.

### Lease state machine and ACID storage

Each lease moves through a clear set of states: `FREE`, `OFFERED`,
`BOUND`, `RENEWING`, `REBINDING`, and then `EXPIRED` or `RELEASED`. This
is modeled as a `LeaseState` enum on a `Lease` dataclass in
[`protocols/dhcp_server.py`](protocols/dhcp_server.py).

Leases are stored through `LeaseStore`, which is backed by SQLite, even
for the in-memory (`:memory:`) case. This is not overkill. It is what
gives lease commits their ACID guarantee for free. `commit_lease()`
wraps its `INSERT ... ON CONFLICT ... DO UPDATE` statement inside a
transaction (`with self._conn:`), so a lease write either fully happens
or does not happen at all, even under concurrent access from multiple
client threads.

### Renewal timers (T1 and T2)

A real DHCP client does not wait for its lease to expire before asking
to keep it. It renews partway through, ahead of time. The RFC 2131
standard schedule works like this:

- **T1, at 50% of the lease.** The client sends a unicast DHCPREQUEST to
  renew.
- **T2, at 87.5% of the lease.** If renewal at T1 failed, the client
  broadcasts a DHCPREQUEST to any server. This step is called rebinding.
- **At 100%, the lease expires.** If that also failed, the client gives
  up the address entirely.

`DHCPClient._schedule_timers()` in
[`protocols/dhcp_client.py`](protocols/dhcp_client.py) sets three
`threading.Timer` objects at exactly those offsets from `bound_at`. This
is also the exact mechanism behind the one real bug found during
development. See [LEARNINGS.md](LEARNINGS.md).

### Link-state routing and Dijkstra

Instead of a person typing in static routes, each router floods
**Link-State Advertisements (LSAs)**. These describe its directly
connected neighbors and the cost to reach them
(`LinkStateAdvertisement(router_id, neighbors, sequence)`). Every router
that receives an LSA newer than the one it already has, checked using
the `sequence` number, updates its local view of the whole topology
graph, and recomputes the shortest paths from itself. This is exactly
how OSPF and IS-IS work in real networks, just without the flooding
protocol itself. In this project, LSAs are installed directly through
`install_lsa()`, instead of being carried over the wire.

`protocols/routing_engine.py`'s `dijkstra()` is a standard
priority-queue implementation. It keeps a min-heap of `(distance,
node)` pairs, always expands the closest unvisited node, and relaxes
that node's neighbors' distances. `build_routing_table()` then walks the
predecessor chain that Dijkstra produces, back from each destination, to
find the next hop. The next hop is the one piece a router actually needs
to forward a packet, rather than the whole path.

### Kernel route injection

Once a router knows the next hop for a destination, that knowledge has
to become a real kernel routing table entry, or it stays just data
sitting in Python. `KernelRouteInjector.inject()` runs `ip route replace
<destination> via <next-hop>`, optionally inside a namespace using `ip
netns exec`. Using `replace` instead of `add` is deliberate. It is
idempotent, so re-running the same computed table after a reconvergence
does not error out with "route already exists."

### PMTUD: Path MTU Discovery

When a packet is too big for a link and has the DF (Don't Fragment) bit
set, the router that would need to fragment it instead drops the packet
and sends back ICMP Type 3, Code 4, meaning "Fragmentation Needed." This
message tells the sender the MTU of the link it hit, so the sender can
shrink future packets. This is how TCP connections adapt to the smallest
MTU on a path, without needing to probe every hop manually.

The test harness reproduces this directly. `Node.set_interface_mtu()`
deliberately shrinks one hop's MTU below 1500. Then
[`telemetry/packet_collector.py`](telemetry/packet_collector.py)'s
`ping(..., df=True, size=1472)` sends an oversized, DF-set ping across
that hop, while a `PacketCapture`, a `tcpdump` wrapper, records the wire
traffic to a `.pcap` file. `find_fragmentation_needed()` then searches
the capture for the "need to frag" ICMP response.

### The pytest harness split

Roughly half of this codebase, the parts using namespaces, OVS, real
sockets, `ping`, and `tcpdump`, simply cannot run without Linux and
root. Rather than make the whole test suite fail to run outside that
environment, every test file is split into two groups:

- **Unit tests.** These exercise the DHCP state machine and routing
  engine directly, as plain Python objects and functions. No subprocess,
  no socket.
- **Integration tests.** These are marked `@requires_root_linux`, in
  `tests/conftest.py`. This is a `pytest.mark.skipif` that checks
  `platform.system() == "Linux" and os.geteuid() == 0`. On any other
  platform, such as this repo's own macOS environment, they skip
  cleanly with a clear reason instead of failing.

---

## 3. Module map

| Module | Responsibility |
|---|---|
| [`topology/nodes.py`](topology/nodes.py) | `Node`, `Router`, `Host`, `Switch` classes. Namespace and OVS command wrappers. |
| [`topology/link_manager.py`](topology/link_manager.py) | veth creation, subnet allocation (`Subnet`), MTU, link up/down |
| [`topology/builder.py`](topology/builder.py) | `TopologyController`, which assembles R1, R2, SW1, H1, H2, and DHCPD end to end |
| [`protocols/dhcp_server.py`](protocols/dhcp_server.py) | BOOTP/DHCP wire codec, `DHCPServer` handshake handlers, `LeaseStore` |
| [`protocols/dhcp_client.py`](protocols/dhcp_client.py) | `DHCPClient` handshake runner, T1/T2/expiry timers, `Transport` abstraction |
| [`protocols/routing_engine.py`](protocols/routing_engine.py) | `TopologyGraph`, `dijkstra()`, `RoutingEngine`, `KernelRouteInjector` |
| [`telemetry/packet_collector.py`](telemetry/packet_collector.py) | `PacketCapture` (tcpdump wrapper), PMTUD log parsing, `ConvergenceTracker` |
| [`tests/`](tests/) | unit and root-gated integration tests for all of the above |

---

## 4. End-to-end walkthroughs

### Building the topology

1. `TopologyController.build()` creates five namespaces and one OVS
   bridge.
2. R1 and R2 both get `sysctl net.ipv4.ip_forward=1` set. They are now
   routers.
3. Three `Subnet` objects are carved out: `10.0.1.0/24`, `10.0.2.0/24`,
   and `10.0.3.0/24`, one per link or LAN.
4. `LinkManager` creates veth pairs, moves each end into its namespace or
   onto the OVS bridge, assigns the next free address from the relevant
   subnet, sets MTU, and brings the interface up.
5. The controller also exposes `.lsas()`, which converts the physical
   R1-to-R2 link into an initial pair of `LinkStateAdvertisement`
   objects. This is the seed data a `RoutingEngine` needs to start
   computing routes.

### Acquiring a DHCP lease

1. `DHCPClient.run_handshake()` builds a DHCPDISCOVER packet with
   `build_dhcp_packet()` and sends it through its `Transport`.
2. `DHCPServer.handle_discover()` picks the next free IP from
   `DHCPPool`, writes a `Lease` in state `OFFERED` to the SQLite-backed
   `LeaseStore`, and returns a DHCPOFFER.
3. The client sends a DHCPREQUEST that echoes that IP back, using
   Option 50, Requested IP.
4. `DHCPServer.handle_request()` checks for a MAC/IP conflict, then
   commits the lease as `BOUND` and replies with DHCPACK. If another
   live lease already holds that IP, it replies with DHCPNAK instead.
5. Once the client receives the ACK, it starts its T1, T2, and expiry
   timers, based on the lease time the server returned.

### Recovering from a link failure

1. `RoutingEngine.handle_link_failure(neighbor)` removes that edge from
   the router's local `TopologyGraph`.
2. It immediately recomputes `build_routing_table()`. Dijkstra reruns
   from scratch over the reduced graph, so any destination that had an
   alternate path now routes through it.
3. The call returns a `ConvergenceResult`, which carries how long the
   recompute took. This is what the fault-recovery tests check.
4. In the live-lab version of this, the root-gated integration test,
   `ConvergenceTracker` also times end-to-end recovery. It marks when
   the link went down, keeps probing with `ping`, and records the first
   successful probe as the recovery point. This gives both the
   control-plane convergence time from Dijkstra and the data-plane
   recovery time, meaning when traffic actually starts flowing again.

### Testing MTU fragmentation

1. A link's MTU is dropped below 1500 using `set_interface_mtu()`.
2. A `PacketCapture` starts recording on that interface.
3. A DF-set ping larger than the shrunk MTU is sent across it.
4. The router at that hop cannot fragment the packet, since DF is set,
   and cannot forward it as-is, since it exceeds the MTU. So it drops
   the packet and returns ICMP Type 3, Code 4.
5. `find_fragmentation_needed()` confirms that reply shows up in the
   capture.
