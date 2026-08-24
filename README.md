# Distributed Virtual Network Topology & Protocol Simulator

An automated network lab emulation environment built on Linux network
namespaces (`ip netns`) and Open vSwitch, featuring a from-scratch DHCP
lease state machine and a link-state routing protocol implementation.

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

The topology/link/telemetry layers shell out to `ip netns`, `ovs-vsctl`,
and `tcpdump`, so building and running the *real* lab requires:

- **Linux**, run as **root** (namespace, veth, and sysctl operations are privileged)
- `iproute2`, `tcpdump` (usually preinstalled)
- Open vSwitch: `sudo apt-get install openvswitch-switch`
- Mininet, if you'd rather drive the topology through its Python API instead
  of raw `ip netns`: `sudo apt-get install mininet`

The **protocol logic itself — the DHCP state machine and the link-state
Dijkstra routing engine — is plain Python with no OS dependencies**, and
its tests run on any platform (see [Running tests](#running-tests) below).

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Running the real lab

```bash
sudo ./scripts/run_testbed.sh
```

This checks for root, Linux, and a running OVS daemon before invoking
`pytest`, which then also exercises the root-gated integration tests
(namespace creation, veth wiring, real ping/PMTUD checks).

To drive the topology directly instead:

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

Every test file is split into:

- **Unit tests** (no root/Linux needed) — the DHCP Discover→Offer→Request→Ack
  handshake, lease collision/expiration, the Dijkstra shortest-path resolver,
  LSA sequence-number handling, and subnet allocation. These run anywhere,
  including this repo's own macOS dev environment.
- **Integration tests** (`@requires_root_linux`) — real namespace/OVS
  provisioning, ping-mesh reachability, PMTUD fragmentation checks, and
  live link-failure re-convergence. These auto-skip unless run as root on
  Linux, per `tests/conftest.py`.

## Module map

| Path | Responsibility |
|---|---|
| `topology/nodes.py` | `Router`, `Host`, `Switch` — namespace + OVS wrappers |
| `topology/link_manager.py` | veth pair creation, subnet allocation, MTU, link up/down |
| `topology/builder.py` | assembles the R1/R2/SW1/H1/H2/DHCPD lab topology |
| `protocols/dhcp_server.py` | DHCP wire codec, lease state machine, SQLite ACID lease store |
| `protocols/dhcp_client.py` | handshake runner, T1/T2/expiry renewal timers |
| `protocols/routing_engine.py` | LSA database, Dijkstra, kernel route injection |
| `telemetry/packet_collector.py` | pcap capture, PMTUD log parsing, convergence/loss tracking |
| `tests/` | pytest suite — reachability, DHCP lifecycle, MTU/PMTUD, fault recovery |

## Design notes

- **DHCP packets are hand-encoded/decoded** against the RFC 2131 BOOTP wire
  format (`protocols/dhcp_server.py:build_dhcp_packet` /
  `parse_dhcp_packet`) rather than depending on a packet-crafting library,
  so the state machine has zero non-stdlib dependencies and can be unit
  tested without a socket or root at all — see `DHCPClient`'s
  `LoopbackTransport`, which wires a client directly to a server instance
  in-process.
- **Renewal timers** follow the standard T1 = 0.5×lease, T2 = 0.875×lease
  schedule (`protocols/dhcp_client.py`).
- **Route injection** uses `ip route replace` via `subprocess`
  (`KernelRouteInjector`) rather than `pyroute2`, to keep the hard
  dependency list at just `pytest`. Swapping in a netlink-based injector is
  a drop-in replacement of that one class.
