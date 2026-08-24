# Learnings & Bugs

This is an honest log of what actually broke while building and testing
this project, plus the subtler correctness details that would have
turned into bugs if they had been gotten wrong on the first pass. It is
split into two sections so the record stays accurate. Only one thing
actually failed a test run.

---

## Bugs caught by the test suite

### 1. The lease-expiration test failed because the client's own renewal timer renewed the lease first

**Symptom**

```
tests/test_dhcp_lifecycle.py::test_lease_expiration_releases_ip FAILED

    time.sleep(1.2)
    expired = server.sweep_expired()
>   assert len(expired) == 1
E   assert 0 == 1
```

The test gave a client a 1-second lease, then slept for 1.2 seconds,
past the lease time, and then asked the server to sweep expired leases.
It expected to find one expired lease. It found zero.

**Investigation**

The store was checked directly, instead of trusting the assertion:

```python
l = server.store.get_by_mac(client.mac)
print(l.bound_at, l.expiry)          # bound_at=...339.18, expiry=...340.18
time.sleep(1.2)
l2 = server.store.get_by_mac(client.mac)
print(l2.bound_at, l2.is_expired())  # bound_at=...340.19, moved forward, and is_expired is False
```

`bound_at` on the stored lease had shifted forward by about a second
during the sleep. Something had re-committed the lease with a fresh
timestamp while the test was asleep.

**Root cause**

This was not a bug in the implementation. The implementation was doing
exactly what it was built to do. `DHCPClient._schedule_timers()` sets a
renewal timer at T1, which is 50% of the lease time, in
`protocols/dhcp_client.py`. With a 1-second lease, T1 fires at the 0.5
second mark. The client's background timer thread sent a real
DHCPREQUEST, the server acknowledged it and rewrote `bound_at` to "now,"
and the lease's expiry moved a full second further out, before the
test's `sleep(1.2)` was even finished.

The test's premise, that a lease with no renewal activity expires on
schedule, was correct. What was wrong is that the test itself was
running a live client that kept renewing, so there genuinely was renewal
activity happening. In a way, this shows the timers working correctly
enough to defeat the test that was trying to check what happens when
they are absent.

**Fix**

The test was made explicit about the scenario it is actually trying to
cover: a client that goes dark, for example by powering off or losing
network access, and stops renewing. This was done by cancelling the
client's timers right after the handshake, before the sleep:

```python
lease = client.run_handshake()
...
# Simulate the client going dark (e.g. powered off) so it never sends
# the T1/T2 renewal REQUESTs that would otherwise keep the lease alive.
client._cancel_timers()

time.sleep(1.2)
expired = server.sweep_expired()
```

**Why this is worth remembering**

Any test that combines a wall-clock sleep with a component that runs its
own background timers needs to account for what those timers do during
the sleep, not just what the test wants to happen. The fix is not a
workaround. Cancelling the client's timers is the correct way to model
"this client is gone" as an explicit starting condition, and it is a
real DHCP scenario. `sweep_expired()` exists specifically to reclaim
addresses from clients that never sent a RELEASE and never renewed.

---

## Correctness details that would have been bugs if rushed

These did not fail anything. They are documented here because getting
them wrong produces incorrect behavior silently, rather than a crash, so
they are easy to reintroduce during a refactor.

### BOOTP header byte offsets have to match exactly, or parsing silently reads garbage

The 236-byte BOOTP header is packed with `struct.pack("!BBBBI HH
4s4s4s4s 16s 64s 128s", ...)`. The `struct` module allows whitespace in
format strings purely for readability. It does not affect the byte
layout. But every field still has to be counted precisely. `chaddr`, the
client MAC, sits at byte offset 28, right after four 4-byte address
fields, `ciaddr`, `yiaddr`, `siaddr`, and `giaddr`, which follow the
12-byte op/htype/hlen/hops/xid/secs/flags block. If that offset is off
by even one field, `parse_dhcp_packet()` does not throw an error. It
just silently hands back the wrong 6 bytes as the client's MAC address,
and every downstream lease gets attributed to a garbage identity. There
is no way to catch this just by reading the code carefully. It can only
really be checked by round-tripping a packet through
`build_dhcp_packet()` and `parse_dhcp_packet()` and confirming the
fields survive the trip. That is exactly what
`test_discover_offer_request_ack_flow` does, end to end.

### `heapq` tie-breaking in Dijkstra affects which equal-cost path wins

`dijkstra()` pushes `(distance, node_id)` tuples onto a min-heap. When
two paths have equal distance, Python's heap compares the second tuple
element, the node ID string. So among equal-cost candidates, the router
ID that sorts first alphabetically gets expanded first. This is harmless
for correctness, since both paths are genuinely equal cost. But it means
`build_routing_table()`'s choice of next hop among ties is deterministic,
yet arbitrary, driven by naming rather than any real preference. The
fault-recovery tests deliberately assert `table["R3"] in ("R2", "R4")`,
instead of pinning one specific next hop, for exactly this reason.
Pinning it would make the test depend on router-naming order instead of
on actual routing correctness.

### Dataclass inheritance requires every subclass field to have a default once the base class does

`Router(Node)` and `Host(Node)` both add their own fields,
`forwarding_enabled` and `dhcp_client`, after inheriting `name`,
`namespace`, and `interfaces` from `Node`. Because `Node.interfaces`
already has a default, `field(default_factory=dict)`, every field added
after it, whether in `Node` itself or in any subclass, also needs a
default. Otherwise Python raises `TypeError: non-default argument
follows default argument` at class-definition time. This is a one-time
rule to know about, not a recurring problem, but it is why every field
added to `Router`, `Host`, or `Interface` in this codebase is declared
with `= False`, `= None`, or a `field(default_factory=...)`.

### SQLite's `:memory:` database is still transactional, which is what makes the "ACID lease store" claim true

It would be easy to assume in-memory SQLite behaves like a plain
dictionary with extra steps, and skip wrapping it in a transaction. That
assumption would be wrong. The `with self._conn:` block around the
`INSERT ... ON CONFLICT ... DO UPDATE` statement in `commit_lease()`
still gives a real atomic commit-or-rollback boundary, even against
`:memory:`. This matters once `DHCPServer` is driven from multiple
threads, which happens once `serve_forever()` and a timer-driven
`sweep_expired()` loop are both touching the store at the same time.
Swapping the `:memory:` path for a real file path is the only change
needed to persist leases across a server restart. Nothing else about
`LeaseStore` assumes one or the other.
