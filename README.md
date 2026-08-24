# Distributed Virtual Network Topology & Protocol Simulator

This project is an automated network lab. It runs on Linux network
namespaces (`ip netns`) and Open vSwitch. It includes a DHCP lease state
machine built from scratch and a link-state routing protocol.

## Architecture

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

## Requirements

The topology, link, and telemetry code calls `ip netns`, `ovs-vsctl`, and
`tcpdump` directly. To build and run the real lab, you need:

- **Linux**, run as **root**. Namespace, veth, and sysctl operations all need root.
- `iproute2` and `tcpdump`. Most Linux systems already have these.
- Open vSwitch. Install it with `sudo apt-get install openvswitch-switch`.
- Mininet, if you'd rather drive the topology through its Python API
  instead of raw `ip netns`. Install it with `sudo apt-get install mininet`.

The protocol logic itself is different. The DHCP state machine and the
link-state Dijkstra routing engine are plain Python with no OS
dependencies. Their tests run on any platform. See
[Running tests](#running-tests) below.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Running the real lab

```bash
sudo ./scripts/run_testbed.sh
```

This script checks for root, Linux, and a running OVS daemon first. Then
it runs `pytest`, which also runs the root-gated integration tests:
namespace creation, veth wiring, and real ping/PMTUD checks.

You can also drive the topology directly:

```python
from topology.builder import TopologyController

controller = TopologyController()
controller.build()   # creates R1, R2, SW1, H1, H2, DHCPD
...
controller.teardown()
```

## Running tests

```bash
pytest tests/ -v
```

Every test file has two kinds of tests:

- **Unit tests.** These need no root and no Linux. They cover the DHCP
  Discover, Offer, Request, Ack handshake, lease collisions, lease
  expiration, the Dijkstra shortest-path resolver, LSA sequence-number
  handling, and subnet allocation. These run anywhere, including on this
  repo's own macOS dev environment.
- **Integration tests.** These are marked `@requires_root_linux`. They
  cover real namespace and OVS setup, ping-mesh reachability, PMTUD
  fragmentation checks, and live link-failure recovery. They skip
  automatically unless you run them as root on Linux. See
  `tests/conftest.py` for the skip logic.

## Module map

| Path | Responsibility |
|---|---|
| `topology/nodes.py` | `Router`, `Host`, `Switch` classes. Namespace and OVS wrappers. |
| `topology/link_manager.py` | veth pair creation, subnet allocation, MTU, link up/down |
| `topology/builder.py` | assembles the R1/R2/SW1/H1/H2/DHCPD lab topology |
| `protocols/dhcp_server.py` | DHCP wire codec, lease state machine, SQLite ACID lease store |
| `protocols/dhcp_client.py` | handshake runner, T1/T2/expiry renewal timers |
| `protocols/routing_engine.py` | LSA database, Dijkstra, kernel route injection |
| `telemetry/packet_collector.py` | pcap capture, PMTUD log parsing, convergence/loss tracking |
| `tests/` | pytest suite covering reachability, DHCP lifecycle, MTU/PMTUD, fault recovery |

## Design notes

- DHCP packets are encoded and decoded by hand, following the RFC 2131
  BOOTP wire format (`protocols/dhcp_server.py:build_dhcp_packet` /
  `parse_dhcp_packet`). This project does not use a packet-crafting
  library. Because of this, the state machine has zero non-stdlib
  dependencies and can be unit tested without a socket or root at all.
  See `DHCPClient`'s `LoopbackTransport`, which wires a client directly
  to a server instance in the same process.
- Renewal timers follow the standard schedule: T1 is 50% of the lease
  time, and T2 is 87.5% of the lease time. See `protocols/dhcp_client.py`.
- Route injection uses `ip route replace` through `subprocess`
  (`KernelRouteInjector`), instead of `pyroute2`. This keeps the only
  hard dependency at `pytest`. You can swap in a netlink-based injector
  by replacing that one class.
