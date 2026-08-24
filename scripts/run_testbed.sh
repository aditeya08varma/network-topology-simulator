#!/usr/bin/env bash
# Pre-flight checks + test runner for the network topology simulator.
# Must run as root on Linux since it manipulates network namespaces and OVS.
set -euo pipefail

fail() { echo "ERROR: $1" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
    fail "this testbed requires Linux (network namespaces, ip route, tcpdump)."
fi

if [[ "${EUID}" -ne 0 ]]; then
    fail "this testbed requires root privileges (creates netns, veth pairs, OVS bridges). Re-run with sudo."
fi

for bin in ip ovs-vsctl tcpdump ping python3; do
    command -v "$bin" >/dev/null 2>&1 || fail "required binary '$bin' not found on PATH."
done

if ! ovs-vsctl show >/dev/null 2>&1; then
    fail "Open vSwitch daemon (ovsdb-server/ovs-vswitchd) is not running. Start it before continuing."
fi

echo "pre-flight checks passed. running full test suite (including root-gated integration tests)..."
python3 -m pytest "$(dirname "$0")/../tests" -v
