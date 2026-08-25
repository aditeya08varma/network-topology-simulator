"""Node type definitions for the virtual topology: routers, switches, hosts.

Each Node owns a Linux network namespace and the interfaces living inside it.
All namespace manipulation goes through `ip netns exec`, so these classes
only work on Linux and generally need root. That's isolated here so the
rest of the codebase (DHCP, routing) can be exercised without either.
"""
from __future__ import annotations

import ipaddress
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# Runs a shell command inside a given network namespace.
def _netns_exec(namespace: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ip", "netns", "exec", namespace, *cmd],
        check=check, capture_output=True, text=True,
    )


@dataclass
class Interface:
    name: str
    mac: Optional[str] = None
    ip: Optional[ipaddress.IPv4Interface] = None
    mtu: int = 1500
    up: bool = False


@dataclass
class Node:
    """Base class for anything that lives in its own Linux network namespace."""

    name: str
    namespace: str
    interfaces: dict[str, Interface] = field(default_factory=dict)

    # Creates the Linux network namespace for this node.
    def create_namespace(self) -> None:
        subprocess.run(["ip", "netns", "add", self.namespace], check=True)

    # Deletes this node's network namespace, and ignores it if it's already gone.
    def delete_namespace(self) -> None:
        subprocess.run(["ip", "netns", "del", self.namespace], check=False)

    # Runs a command inside this node's own network namespace.
    def exec(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        return _netns_exec(self.namespace, *cmd, check=check)

    # Remembers a network interface as belonging to this node.
    def add_interface(self, iface: Interface) -> None:
        self.interfaces[iface.name] = iface

    # Changes an interface's MTU and updates the stored record to match.
    def set_interface_mtu(self, iface_name: str, mtu: int) -> None:
        self.exec("ip", "link", "set", "dev", iface_name, "mtu", str(mtu))
        self.interfaces[iface_name].mtu = mtu

    # Turns an interface on and marks it as up.
    def bring_interface_up(self, iface_name: str) -> None:
        self.exec("ip", "link", "set", "dev", iface_name, "up")
        self.interfaces[iface_name].up = True

    # Turns an interface off and marks it as down.
    def bring_interface_down(self, iface_name: str) -> None:
        self.exec("ip", "link", "set", "dev", iface_name, "down")
        self.interfaces[iface_name].up = False

    # Gives an interface an IP address and remembers it.
    def assign_ip(self, iface_name: str, address: str) -> None:
        self.exec("ip", "addr", "add", address, "dev", iface_name)
        self.interfaces[iface_name].ip = ipaddress.IPv4Interface(address)


@dataclass
class Router(Node):
    forwarding_enabled: bool = False

    # Turns this node into a router by letting it forward packets between interfaces.
    def enable_ip_forwarding(self) -> None:
        self.exec("sysctl", "-w", "net.ipv4.ip_forward=1")
        self.forwarding_enabled = True

    # Adds or updates one route in this router's routing table.
    def replace_route(self, destination: str, via: Optional[str] = None, dev: Optional[str] = None) -> None:
        cmd = ["ip", "route", "replace", destination]
        if via:
            cmd += ["via", via]
        if dev:
            cmd += ["dev", dev]
        self.exec(*cmd)

    # Removes one route from this router's routing table.
    def delete_route(self, destination: str) -> None:
        self.exec("ip", "route", "del", destination, check=False)


@dataclass
class Host(Node):
    """A leaf endpoint. `dhcp_client` is set once the host has run its
    handshake (see protocols/dhcp_client.py)."""

    dhcp_client: Optional[object] = None


@dataclass
class Switch:
    """An Open vSwitch bridge, e.g. SW1 in the lab topology."""

    name: str
    ports: list[str] = field(default_factory=list)

    # Creates the Open vSwitch bridge.
    def create(self) -> None:
        subprocess.run(["ovs-vsctl", "add-br", self.name], check=True)

    # Deletes the Open vSwitch bridge, and ignores it if it's already gone.
    def delete(self) -> None:
        subprocess.run(["ovs-vsctl", "del-br", self.name], check=False)

    # Plugs one more interface into the switch as a port.
    def add_port(self, iface_name: str) -> None:
        subprocess.run(["ovs-vsctl", "add-port", self.name, iface_name], check=True)
        self.ports.append(iface_name)
