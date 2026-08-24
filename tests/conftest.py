"""Shared fixtures and skip markers for the test suite.

Most of this codebase (DHCP state machine, Dijkstra routing) is plain
Python and runs anywhere. The parts that touch `ip netns`/OVS need Linux
+ root, so those tests are gated behind `requires_root_linux` and skip
cleanly on any other platform (e.g. this repo being authored on macOS).
"""
from __future__ import annotations

import os
import platform
import shutil

import pytest


def _is_linux_root() -> bool:
    return platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0


requires_root_linux = pytest.mark.skipif(
    not _is_linux_root(),
    reason="requires root privileges on Linux (network namespaces / veth / sysctl)",
)

requires_ovs = pytest.mark.skipif(
    shutil.which("ovs-vsctl") is None,
    reason="requires Open vSwitch tooling (ovs-vsctl)",
)


@pytest.fixture
def built_topology():
    from topology.builder import TopologyController

    controller = TopologyController()
    controller.build()
    try:
        yield controller
    finally:
        controller.teardown()
