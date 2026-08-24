# Project Explanation

What this simulator does, the technologies it's built on, and how each
piece actually works under the hood.

---

## 1. What this project is

A **software-only network lab**: instead of buying routers and switches,
it uses Linux kernel features to create isolated virtual network
namespaces that behave like real hosts and routers, wires them together
with virtual cables, and then runs two real network protocols against
that fake topology — a DHCP server/client and a link-state routing
protocol — so you can test protocol behavior, failure recovery, and MTU
edge cases the same way you would on physical gear, without physical gear.

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

Three things make this interesting rather than just a topology script:

1. **The DHCP server/client are hand-written protocol implementations**,
   not `dnsmasq` wrappers — the actual DISCOVER/OFFER/REQUEST/ACK wire
   format is encoded and decoded by hand.
2. **Routing is computed, not configured** — routers exchange link-state
   advertisements and run Dijkstra themselves, the same way OSPF does,
   rather than having static routes typed in.
3. **The protocol logic is deliberately decoupled from the OS layer**, so
   the interesting parts (the state machines) can be unit tested on any
   machine, while the networking parts (namespaces, OVS, real sockets)
   are isolated behind thin wrappers and only run on Linux as root.

---

## 2. Core technologies, explained

### Linux network namespaces (`ip netns`)

A network namespace is a kernel feature that gives a process its own
completely separate networking stack: its own interfaces, routing table,
iptables rules, and port space. Two processes in different namespaces can
both bind port 67 without conflicting, because as far as the kernel is
concerned they're on different machines.

This project uses one namespace per simulated node (`ns-r1`, `ns-r2`,
`ns-h1`, `ns-h2`, `ns-dhcpd`). Every command that should "run on that
node" — assigning an IP, bringing up an interface, running `ping` — gets
wrapped in `ip netns exec <namespace> <command>`. See
[`topology/nodes.py`](topology/nodes.py)'s `Node.exec()`, which every
other namespace-aware method funnels through.

### Veth pairs (virtual ethernet)

A veth pair is two virtual network interfaces that are permanently
connected to each other — anything sent into one end comes out the
other, like a virtual patch cable. To connect two namespaces, you create
a veth pair *outside* any namespace, then move one end into each
namespace with `ip link set <iface> netns <namespace>`.

[`topology/link_manager.py`](topology/link_manager.py)'s
`LinkManager.attach_link()` does exactly this for router-router links,
and `attach_to_bridge()` does it for host-switch links (one end goes into
the host's namespace, the other becomes a port on the OVS bridge).

### Open vSwitch (OVS)

OVS is a software switch — a userspace/kernel-hybrid implementation of
what a physical Ethernet switch does (MAC learning, forwarding between
ports on the same broadcast domain). `SW1` in the topology is an OVS
bridge (`ovs-vsctl add-br`), and each host/router gets a veth end added
as a bridge port (`ovs-vsctl add-port`). This is what lets H1 and H2 be
on the same subnet and reach each other without going through a router.

### IP forwarding (`sysctl net.ipv4.ip_forward=1`)

By default, a Linux box drops packets that arrive on one interface
addressed to a different subnet than its own — it only acts as a host,
not a router. Setting `net.ipv4.ip_forward=1` inside a namespace turns
that namespace into a router: it will now forward packets between its
interfaces based on its routing table.
`Router.enable_ip_forwarding()` in
[`topology/nodes.py`](topology/nodes.py) sets this for R1 and R2, which
is what makes them actual routers instead of just multi-homed hosts.

### MTU and jumbo frames

MTU (Maximum Transmission Unit) is the largest packet size an interface
will send without fragmenting. Standard Ethernet is 1500 bytes; "jumbo
frames" (commonly 9000) trade compatibility for less per-packet overhead
on links that support it. This project lets you set MTU per-interface
(`Node.set_interface_mtu()`) specifically so the test suite can
deliberately create an MTU *mismatch* between two hops — which is what
makes the PMTUD tests possible (see below).

### DHCP — the 4-way handshake, from bytes up

DHCP packets are BOOTP packets (RFC 951) with an options block on the end
(RFC 2131 layers DHCP semantics on top of that older format). The fixed
header is 236 bytes, laid out as: `op, htype, hlen, hops, xid, secs,
flags, ciaddr, yiaddr, siaddr, giaddr, chaddr[16], sname[64], file[128]`.
After that comes a 4-byte "magic cookie" (`99.130.83.99`) and then a
sequence of `(option_code, length, value)` triplets terminated by option
255.

[`protocols/dhcp_server.py`](protocols/dhcp_server.py) implements
`build_dhcp_packet()` / `parse_dhcp_packet()` against this exact layout
using Python's `struct` module — no packet library, no scapy. This was a
deliberate choice: it keeps the DHCP logic dependency-free and, more
importantly, means the *entire* handshake can be tested by constructing
and parsing byte strings directly, without ever opening a socket.

The handshake itself:

1. **DHCPDISCOVER** — client broadcasts "does anyone have an IP for me?"
   with its MAC address as the only identifying info.
2. **DHCPOFFER** — server picks a free IP from its pool, reserves it
   provisionally (`LeaseState.OFFERED`), and replies with that IP plus
   Option 1 (subnet mask), Option 3 (router/gateway), Option 6 (DNS), and
   Option 51 (lease time).
3. **DHCPREQUEST** — client says "yes, I'll take that IP" (this step
   exists so that if two servers offered the same client an IP, only one
   offer gets accepted).
4. **DHCPACK** — server commits the lease as `LeaseState.BOUND` and
   confirms.

Each of these is a pure function in `DHCPServer` —
`handle_discover(pkt) -> DHCPPacket`,
`handle_request(pkt) -> DHCPPacket` — that takes a parsed packet in and
returns a parsed packet out, with no I/O. The only place a real socket
appears is `serve_forever()`, a thin loop that reads bytes, parses them,
calls the handler, and sends the reply.

### Lease state machine & ACID storage

Each lease moves through explicit states: `FREE → OFFERED → BOUND →
RENEWING → REBINDING → EXPIRED/RELEASED`. This is modeled as a
`LeaseState` enum on a `Lease` dataclass in
[`protocols/dhcp_server.py`](protocols/dhcp_server.py).

Leases are persisted through `LeaseStore`, backed by **SQLite** even for
the in-memory (`:memory:`) case. This isn't overkill — it's what gives
lease commits their ACID guarantee for free: `commit_lease()` wraps its
`INSERT ... ON CONFLICT ... DO UPDATE` in a transaction (`with
self._conn:`), so a lease write either fully happens or fully doesn't,
even under concurrent access from multiple client threads.

### Renewal timers (T1 / T2)

A real DHCP client doesn't wait for its lease to expire before asking to
keep it — it proactively renews partway through. The RFC 2131 standard
schedule is:

- **T1 = 50% of the lease** — client sends a unicast DHCPREQUEST to renew.
- **T2 = 87.5% of the lease** — if renewal at T1 failed, client
  broadcasts a DHCPREQUEST to *any* server (rebinding).
- **100% (expiry)** — if that also failed, the client gives up the
  address entirely.

`DHCPClient._schedule_timers()` in
[`protocols/dhcp_client.py`](protocols/dhcp_client.py) sets three
`threading.Timer`s at exactly those offsets from `bound_at`. This is also
exactly the mechanism that caused the one real bug during development —
see [LEARNINGS.md](LEARNINGS.md).

### Link-state routing & Dijkstra

Instead of a human typing static routes, each router floods **Link-State
Advertisements (LSAs)** describing its directly-connected neighbors and
the cost to reach them (`LinkStateAdvertisement(router_id, neighbors,
sequence)`). Every router that receives an LSA newer than what it already
has (`sequence` number check) updates its local view of the whole
topology graph and recomputes shortest paths from itself — this is
exactly how OSPF/IS-IS work in real networks, just without the flooding
protocol itself (LSAs are installed directly via `install_lsa()` here
rather than being carried over the wire).

`protocols/routing_engine.py`'s `dijkstra()` is a textbook
priority-queue implementation: maintain a min-heap of `(distance, node)`,
always expand the closest unvisited node, relax its neighbors' distances.
`build_routing_table()` then walks the predecessor chain Dijkstra
produces back from each destination to find the *next hop* — the piece a
router actually needs to forward a packet — rather than the whole path.

### Kernel route injection

Once a router knows the next hop for a destination, that has to become a
real kernel routing table entry or it's just data sitting in Python.
`KernelRouteInjector.inject()` shells out to `ip route replace
<destination> via <next-hop>` (optionally inside a namespace via `ip
netns exec`). `replace` rather than `add` is deliberate — it's idempotent,
so re-running the same computed table after a reconvergence doesn't error
on "route already exists."

### PMTUD — Path MTU Discovery

When a packet is too big for a link and has the **DF (Don't Fragment)**
bit set, the router that would have needed to fragment it instead drops
it and sends back **ICMP Type 3, Code 4** ("Fragmentation Needed"),
which tells the sender the MTU of the link it hit so it can shrink future
packets — this is how TCP connections adapt to the smallest MTU on a path
without every hop needing to be probed manually.

The test harness reproduces this directly: `Node.set_interface_mtu()`
deliberately shrinks one hop's MTU below 1500, then
[`telemetry/packet_collector.py`](telemetry/packet_collector.py)'s
`ping(..., df=True, size=1472)` sends an oversized, DF-set ping across
it, while a `PacketCapture` (a `tcpdump` wrapper) records the wire
traffic to a `.pcap` file. `find_fragmentation_needed()` then greps the
capture for the "need to frag" ICMP response.

### The pytest harness split

Roughly half of this codebase (namespaces, OVS, real sockets, `ping`,
`tcpdump`) simply cannot run without Linux and root. Rather than make the
whole suite un-runnable outside that environment, every test file is
split:

- **Unit tests** exercise the DHCP state machine and routing engine
  directly as Python objects/functions — no subprocess, no socket.
- **Integration tests** are marked `@requires_root_linux`
  (`tests/conftest.py`), a `pytest.mark.skipif` that checks
  `platform.system() == "Linux" and os.geteuid() == 0`. On any other
  platform (this repo was built and its unit tests verified on macOS),
  they skip cleanly with a clear reason instead of failing.

---

## 3. Module map

| Module | Responsibility |
|---|---|
| [`topology/nodes.py`](topology/nodes.py) | `Node`/`Router`/`Host`/`Switch` — namespace and OVS command wrappers |
| [`topology/link_manager.py`](topology/link_manager.py) | veth creation, subnet allocation (`Subnet`), MTU, link up/down |
| [`topology/builder.py`](topology/builder.py) | `TopologyController` — assembles R1/R2/SW1/H1/H2/DHCPD end to end |
| [`protocols/dhcp_server.py`](protocols/dhcp_server.py) | BOOTP/DHCP wire codec, `DHCPServer` handshake handlers, `LeaseStore` |
| [`protocols/dhcp_client.py`](protocols/dhcp_client.py) | `DHCPClient` handshake runner, T1/T2/expiry timers, `Transport` abstraction |
| [`protocols/routing_engine.py`](protocols/routing_engine.py) | `TopologyGraph`, `dijkstra()`, `RoutingEngine`, `KernelRouteInjector` |
| [`telemetry/packet_collector.py`](telemetry/packet_collector.py) | `PacketCapture` (tcpdump wrapper), PMTUD log parsing, `ConvergenceTracker` |
| [`tests/`](tests/) | unit + root-gated integration tests for all of the above |

---

## 4. End-to-end walkthroughs

### Building the topology

1. `TopologyController.build()` creates five namespaces and one OVS
   bridge.
2. R1 and R2 get `sysctl net.ipv4.ip_forward=1` — they're now routers.
3. Three `Subnet` objects are carved out (`10.0.1.0/24`, `.2.0/24`,
   `.3.0/24`), one per link/LAN.
4. `LinkManager` creates veth pairs, moves each end into its namespace or
   onto the OVS bridge, assigns the next free address from the relevant
   subnet, sets MTU, and brings the interface up.
5. The controller also exposes `.lsas()`, converting the physical
   R1↔R2 link into an initial `LinkStateAdvertisement` pair — the seed
   data a `RoutingEngine` needs to start computing routes.

### Acquiring a DHCP lease

1. `DHCPClient.run_handshake()` builds a DHCPDISCOVER packet
   (`build_dhcp_packet`) and sends it through its `Transport`.
2. `DHCPServer.handle_discover()` picks the next free IP from `DHCPPool`,
   writes a `Lease` in state `OFFERED` to the SQLite-backed `LeaseStore`,
   and returns a DHCPOFFER.
3. The client sends DHCPREQUEST echoing that IP back (Option 50,
   Requested IP).
4. `DHCPServer.handle_request()` checks for a MAC/IP conflict, then
   commits the lease as `BOUND` and replies DHCPACK — or DHCPNAK if
   another live lease already holds that IP.
5. On receiving the ACK, the client starts its T1/T2/expiry timers based
   on the lease time the server returned.

### Recovering from a link failure

1. `RoutingEngine.handle_link_failure(neighbor)` removes that edge from
   the router's local `TopologyGraph`.
2. It immediately recomputes `build_routing_table()` — Dijkstra reruns
   from scratch over the reduced graph, so any destination that had an
   alternate path now routes through it.
3. The call returns a `ConvergenceResult` carrying how long the
   recompute took, which is what the fault-recovery tests assert against.
4. In the live-lab version of this (the root-gated integration test),
   `ConvergenceTracker` additionally times *end-to-end* recovery: it
   marks when the link went down, keeps probing with `ping`, and records
   the first successful probe as the recovery point — giving both
   control-plane convergence time (Dijkstra) and data-plane recovery time
   (when traffic actually flows again).

### Testing MTU fragmentation

1. A link's MTU is dropped below 1500 with `set_interface_mtu()`.
2. A `PacketCapture` starts recording on that interface.
3. A DF-set ping larger than the shrunk MTU is sent across it.
4. The router at that hop can't fragment (DF is set) and can't forward
   as-is (packet exceeds its MTU), so it drops the packet and returns
   ICMP Type 3 Code 4.
5. `find_fragmentation_needed()` confirms that reply shows up in the
   capture.
