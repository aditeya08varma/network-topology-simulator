# Learnings & Bugs

An honest log of what actually broke while building and testing this
project, plus the subtler correctness details that would have become
bugs if they'd been gotten wrong on the first pass. Split into two
sections so the record stays accurate: only one thing actually failed a
test run.

---

## Bugs caught by the test suite

### 1. Lease-expiration test failed because the client's own renewal timer renewed the lease first

**Symptom**

```
tests/test_dhcp_lifecycle.py::test_lease_expiration_releases_ip FAILED

    time.sleep(1.2)
    expired = server.sweep_expired()
>   assert len(expired) == 1
E   assert 0 == 1
```

The test gave a client a 1-second lease, slept 1.2 seconds — past the
lease time — then asked the server to sweep expired leases. It expected
to find one. It found zero.

**Investigation**

Instrumented the store directly instead of trusting the assertion:

```python
l = server.store.get_by_mac(client.mac)
print(l.bound_at, l.expiry)          # bound_at=...339.18, expiry=...340.18
time.sleep(1.2)
l2 = server.store.get_by_mac(client.mac)
print(l2.bound_at, l2.is_expired())  # bound_at=...340.19  <- moved forward!  False
```

`bound_at` on the *stored* lease had shifted forward by about a second
during the sleep — something had re-committed the lease with a fresh
timestamp while the test was asleep.

**Root cause**

Not a bug in the implementation — the implementation was doing exactly
what it was built to do. `DHCPClient._schedule_timers()` sets a renewal
timer at **T1 = 50% of the lease** (`protocols/dhcp_client.py`). With a
1-second lease, T1 fires at the 0.5s mark: the client's background timer
thread sent a real DHCPREQUEST, the server ACKed it and rewrote
`bound_at` to "now," and the lease's expiry moved a full second further
out — before the test's `sleep(1.2)` was even done sleeping.

The test's premise — "a lease with no renewal activity expires on
schedule" — was correct. What was wrong was that the *test itself* was
running a live client that kept renewing, so there genuinely was renewal
activity. This is arguably the timers working correctly enough to defeat
the test that was trying to verify what happens in their absence.

**Fix**

Made the test explicit about the scenario it's actually trying to cover
(a client that goes dark — powers off, loses network, whatever — and
stops renewing) by cancelling the client's timers right after the
handshake, before the sleep:

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

Any test that mixes "wall-clock sleep" with "a component that has its
own background timers" needs to account for what those timers do during
the sleep, not just what the test wants to happen. The fix isn't a
workaround — cancelling the client's timers is the correct way to model
"this client is gone" as an explicit precondition, which is a real
DHCP scenario (`sweep_expired()` exists specifically to reclaim
addresses from clients that never sent a RELEASE and never renewed).

---

## Correctness details that would have been bugs if rushed

These didn't fail anything — they're documented here because getting
them wrong silently produces incorrect behavior rather than a crash, so
they're easy to reintroduce during a refactor.

### BOOTP header byte offsets have to match exactly, or parsing silently reads garbage

The 236-byte BOOTP header is packed with `struct.pack("!BBBBI HH
4s4s4s4s 16s 64s 128s", ...)`. `struct` allows whitespace in format
strings purely for readability — it doesn't affect the byte layout — but
every field still has to be counted precisely: `chaddr` (the client MAC)
sits at byte offset 28, right after four 4-byte address fields
(`ciaddr`, `yiaddr`, `siaddr`, `giaddr`) following the 12-byte
op/htype/hlen/hops/xid/secs/flags block. Get that offset off by even one
field and `parse_dhcp_packet()` doesn't throw — it just silently hands
back the wrong 6 bytes as the client's MAC address, and every downstream
lease gets attributed to a garbage identity. There's no way to verify
this against a byte string during casual review; it can only really be
checked by round-tripping a packet through `build_dhcp_packet()` and
`parse_dhcp_packet()` and asserting the fields survive, which is exactly
what `test_discover_offer_request_ack_flow` does end-to-end.

### `heapq` tie-breaking in Dijkstra affects which equal-cost path wins

`dijkstra()` pushes `(distance, node_id)` tuples onto a min-heap. When
two paths have equal distance, Python's heap compares the second tuple
element — the node ID string — so among equal-cost candidates the
lexicographically smaller router ID gets expanded first. That's harmless
for correctness (both paths are genuinely equal cost), but it means
`build_routing_table()`'s choice of next-hop among ties is deterministic
but arbitrary, driven by naming rather than any real preference. The
fault-recovery tests deliberately assert `table["R3"] in ("R2", "R4")`
rather than pinning one specific next hop for that reason — pinning it
would make the test coupled to router-naming order rather than to actual
routing correctness.

### Dataclass inheritance requires every subclass field to have a default once the base class does

`Router(Node)` and `Host(Node)` both add their own fields
(`forwarding_enabled`, `dhcp_client`) after inheriting `name`, `namespace`,
and `interfaces` from `Node`. Because `Node.interfaces` already has a
default (`field(default_factory=dict)`), every field added afterward —
in `Node` itself or any subclass — has to have a default too, or Python
raises `TypeError: non-default argument follows default argument` at
class-definition time. This is a one-time constraint to know about, not
a recurring gotcha, but it's the reason every field added to `Router`/
`Host`/`Interface` in this codebase is declared with `= False`, `=
None`, or a `field(default_factory=...)`.

### SQLite's `:memory:` database is still transactional, which is what makes the "ACID lease store" claim true

It would be easy to assume in-memory SQLite is just a dict with extra
steps and skip the transaction wrapping. It isn't — `with self._conn:`
around the `INSERT ... ON CONFLICT ... DO UPDATE` in `commit_lease()`
still gives a real atomic commit-or-rollback boundary even against
`:memory:`, which matters once `DHCPServer` is driven from multiple
threads (as it is once `serve_forever()` and a timer-driven
`sweep_expired()` loop are both touching the store). Swapping the
`:memory:` path for a real file is the only change needed to persist
leases across a server restart — nothing else about `LeaseStore` assumes
one or the other.
