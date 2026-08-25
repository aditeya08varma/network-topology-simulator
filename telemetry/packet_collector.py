"""Pcap capture, ICMP fragmentation-needed log parsing, and
convergence/packet-loss tracking for fault-injection tests."""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


class PacketCapture:
    """Wraps tcpdump to capture traffic on one interface (optionally inside
    a network namespace) to a pcap file for later inspection."""

    # Remembers which interface to record and where to save the capture file.
    def __init__(self, interface: str, output_file: str, namespace: Optional[str] = None, snaplen: int = 262144):
        self.interface = interface
        self.output_file = output_file
        self.namespace = namespace
        self.snaplen = snaplen
        self._proc: Optional[subprocess.Popen] = None

    # Builds the tcpdump command used to start the capture.
    def _cmd(self) -> list[str]:
        cmd = ["tcpdump", "-i", self.interface, "-s", str(self.snaplen), "-w", self.output_file, "-U"]
        if self.namespace:
            cmd = ["ip", "netns", "exec", self.namespace] + cmd
        return cmd

    # Starts tcpdump running in the background.
    def start(self) -> None:
        self._proc = subprocess.Popen(self._cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)  # let tcpdump attach before traffic flows

    # Stops the running tcpdump process.
    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None

    # Starts the capture automatically when used in a `with` block.
    def __enter__(self) -> "PacketCapture":
        self.start()
        return self

    # Stops the capture automatically when the `with` block ends.
    def __exit__(self, *exc) -> None:
        self.stop()


ICMP_FRAG_NEEDED_RE = re.compile(r"unreachable.*need to frag", re.IGNORECASE)


# Searches a capture file for ICMP replies that say a packet was too big to forward.
def find_fragmentation_needed(pcap_path: str) -> list[str]:
    """Reads a capture file and returns lines that are ICMP Type 3 Code 4
    (Fragmentation Needed, DF set) replies. This is the PMTUD signal."""
    result = subprocess.run(["tcpdump", "-r", pcap_path, "-nn", "-v"], check=True, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if ICMP_FRAG_NEEDED_RE.search(line)]


@dataclass
class ConvergenceTracker:
    """Measures how long a topology takes to reroute traffic after a
    programmatic link failure, and the packet loss incurred in between."""

    link_down_at: Optional[float] = None
    recovered_at: Optional[float] = None
    probes_sent: int = 0
    probes_lost: int = 0

    # Records the moment a link went down.
    def mark_link_down(self) -> None:
        self.link_down_at = time.time()

    # Records whether one reachability check succeeded or failed.
    def record_probe(self, success: bool) -> None:
        self.probes_sent += 1
        if not success:
            self.probes_lost += 1
        elif self.link_down_at is not None and self.recovered_at is None:
            self.recovered_at = time.time()

    # Works out how long it took to recover after the link went down.
    @property
    def convergence_seconds(self) -> Optional[float]:
        if self.link_down_at is None or self.recovered_at is None:
            return None
        return self.recovered_at - self.link_down_at

    # Works out what percentage of probes were lost.
    @property
    def packet_loss_pct(self) -> float:
        if self.probes_sent == 0:
            return 0.0
        return 100.0 * self.probes_lost / self.probes_sent


# Pings a target from inside a namespace and reports whether it succeeded.
def ping(namespace: str, target: str, count: int = 1, timeout: float = 1.0,
         df: bool = False, size: Optional[int] = None) -> bool:
    """Runs `ping` inside a namespace, returns True on success. `df` sets
    -M do (Don't Fragment) for PMTUD tests; `size` sets the payload size to
    test oversized packets against a link's MTU."""
    cmd = ["ip", "netns", "exec", namespace, "ping", "-c", str(count), "-W", str(timeout)]
    if df:
        cmd += ["-M", "do"]
    if size is not None:
        cmd += ["-s", str(size)]
    cmd.append(target)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0
