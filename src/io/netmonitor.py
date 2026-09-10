"""H4 - out-of-process network observer.

The second of two layers behind the sovereignty claim (see `src/io/netguard.py`
for the first). This one polls `lsof -i` against our own process tree from
outside the interpreter, so it catches what in-process socket patching cannot:
a subprocess (the sandbox, a future native dependency) opening a connection
directly, bypassing Python's socket module entirely.

Neither layer alone is a complete answer. `netguard` is deterministic but can
only see what happens inside this interpreter. `netmonitor` sees the whole
process tree but samples on an interval, so a connection opened and closed
between polls could be missed - `lsof` only reports what is open *now*.
Together: enforcement plus independent, external corroboration.

This module is read-only. It cannot block anything; it only reports.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from src import config
from src.contracts import AuditEventType
from src.io import audit
from src.io.netguard import is_local

# lsof -i NAME column: "local_addr:local_port->remote_addr:remote_port (STATE)"
_NAME_PATTERN = re.compile(r"->(?P<host>[^:]+):(?P<port>\d+)\s*\((?P<state>[A-Z_]+)\)")


@dataclass
class ObservedConnection:
    pid: int
    command: str
    remote_host: str
    remote_port: int
    state: str


@dataclass
class PollResult:
    connections: list[ObservedConnection] = field(default_factory=list)
    external: list[ObservedConnection] = field(default_factory=list)
    error: str | None = None


def _process_tree_pids_posix(root_pid: int | None = None) -> list[int]:
    """Our own PID plus every descendant, via pgrep."""
    root_pid = root_pid or os.getpid()
    try:
        output = subprocess.run(
            ["pgrep", "-P", str(root_pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        children = [int(pid) for pid in output.split() if pid.strip()]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        children = []

    pids = [root_pid]
    for child in children:
        pids.extend(_process_tree_pids_posix(child))
    return pids


def _poll_lsof() -> PollResult:
    """One `lsof -i` snapshot of connections held by our process tree."""
    pids = _process_tree_pids_posix()
    pid_args: list[str] = []
    for pid in pids:
        pid_args += ["-p", str(pid)]

    try:
        completed = subprocess.run(
            ["lsof", "-i", "-n", "-P", "-a", *pid_args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return PollResult(error="lsof is not installed - the monitor cannot corroborate netguard")
    except subprocess.TimeoutExpired:
        return PollResult(error="lsof poll timed out")

    connections: list[ObservedConnection] = []
    for line in completed.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue
        command, pid_str = parts[0], parts[1]
        # The NAME column itself ("addr:port->addr:port (STATE)") contains a
        # space before "(STATE)", so it is not a single whitespace-split token -
        # search the whole line rather than trust field position.
        match = _NAME_PATTERN.search(line)
        if not match:
            continue  # LISTEN sockets etc. have no "->remote" - not an active egress
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        connections.append(
            ObservedConnection(
                pid=pid,
                command=command,
                remote_host=match.group("host"),
                remote_port=int(match.group("port")),
                state=match.group("state"),
            )
        )

    external = [conn for conn in connections if not is_local(conn.remote_host)]
    return PollResult(connections=connections, external=external)


# ---------------------------------------------------------------------------
# Windows backend
#
# `lsof` and `pgrep` do not exist on Windows, so without this the observer
# returns an error and contributes nothing - on a Windows demo machine the
# second evidence layer would be silently dead, which is the worst way for it
# to fail. `netstat -ano` is present on every Windows install and reports the
# owning PID, which is all this layer needs.
# ---------------------------------------------------------------------------

# netstat -ano row: proto, local, foreign, state, pid  (UDP rows omit state)
_NETSTAT_ROW = re.compile(
    r"^\s*(?P<proto>TCP|UDP)\s+(?P<local>\S+)\s+(?P<foreign>\S+)\s+"
    r"(?:(?P<state>[A-Z_]+)\s+)?(?P<pid>\d+)\s*$"
)


def _windows_process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent_pid, name), read once per poll via CIM."""
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | "
                "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.Name)\" }",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {}

    table: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split("	")
        if len(parts) != 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
        except ValueError:
            continue
    return table


def _process_tree_pids_windows(table: dict[int, tuple[int, str]]) -> set[int]:
    """Our PID plus every descendant, walked from the CIM parent map."""
    root = os.getpid()
    children: dict[int, list[int]] = {}
    for pid, (parent, _name) in table.items():
        children.setdefault(parent, []).append(pid)

    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue          # defensive: PID reuse can make the map cyclic
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _split_host_port(endpoint: str) -> tuple[str, int] | None:
    """Split netstat's "addr:port", allowing for bracketed IPv6."""
    if endpoint.startswith("["):                       # [::1]:443
        host, _, port = endpoint.rpartition("]:")
        return (host.lstrip("["), int(port)) if port.isdigit() else None
    host, _, port = endpoint.rpartition(":")
    return (host, int(port)) if port.isdigit() and host else None


def _poll_netstat() -> PollResult:
    """One `netstat -ano` snapshot, filtered to our own process tree."""
    table = _windows_process_table()
    if not table:
        return PollResult(error="could not read the Windows process table - monitor cannot corroborate netguard")
    our_pids = _process_tree_pids_windows(table)

    try:
        completed = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return PollResult(error="netstat is not available - the monitor cannot corroborate netguard")
    except subprocess.TimeoutExpired:
        return PollResult(error="netstat poll timed out")

    connections: list[ObservedConnection] = []
    for line in completed.stdout.splitlines():
        match = _NETSTAT_ROW.match(line)
        if not match:
            continue

        pid = int(match.group("pid"))
        if pid not in our_pids:
            continue                                   # someone else's socket

        state = match.group("state") or "UDP"
        if state == "LISTENING":
            continue                                   # not egress; matches the lsof path

        remote = _split_host_port(match.group("foreign"))
        if remote is None or remote[1] == 0:
            continue                                   # 0.0.0.0:0 = no peer

        connections.append(
            ObservedConnection(
                pid=pid,
                command=table.get(pid, (0, "unknown"))[1],
                remote_host=remote[0],
                remote_port=remote[1],
                state=state,
            )
        )

    external = [conn for conn in connections if not is_local(conn.remote_host)]
    return PollResult(connections=connections, external=external)


def poll_once() -> PollResult:
    """One snapshot of the connections our process tree currently holds.

    Dispatches to whichever observer this platform has. Both backends report the
    same shape, so `NetworkMonitor` and the audit trail do not care which ran.
    """
    if sys.platform == "win32":
        return _poll_netstat()
    return _poll_lsof()


class NetworkMonitor:
    """Background poller. Records any externally-observed connection to the audit log."""

    def __init__(self, interval_s: float | None = None):
        self.interval_s = interval_s or config.get("audit.monitor_interval_s", 2.0)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_result = PollResult()
        self._started_at: float | None = None
        self._seen_keys: set[tuple[int, str, int]] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="netmonitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            result = poll_once()
            with self._lock:
                self._last_result = result
            for conn in result.external:
                key = (conn.pid, conn.remote_host, conn.remote_port)
                if key in self._seen_keys:
                    continue
                self._seen_keys.add(key)
                audit.emit(
                    AuditEventType.NETWORK,
                    f"lsof observed external connection: {conn.command} (pid {conn.pid}) "
                    f"-> {conn.remote_host}:{conn.remote_port} [{conn.state}]",
                    actor="netmonitor",
                    external=True,
                    pid=conn.pid,
                    command=conn.command,
                    remote_host=conn.remote_host,
                    remote_port=conn.remote_port,
                    state=conn.state,
                )
            self._stop.wait(self.interval_s)

    @property
    def last_result(self) -> PollResult:
        with self._lock:
            return self._last_result

    @property
    def offline_duration_s(self) -> int:
        if self._started_at is None:
            return 0
        return int(time.monotonic() - self._started_at)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


_monitor: NetworkMonitor | None = None


def get_monitor() -> NetworkMonitor:
    global _monitor
    if _monitor is None:
        _monitor = NetworkMonitor()
    return _monitor
