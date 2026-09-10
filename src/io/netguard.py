"""H4 - in-process egress guard.

Wraps the socket layer so that any connection to a host outside
``audit.allowed_hosts`` is **refused and recorded**. This is the first of the
two layers behind the sovereignty claim:

* This module is *enforcement*: our own code physically cannot open a non-local
  connection while it is installed, and every attempt is written to the audit
  log before the exception is raised.
* ``src/io/netmonitor.py`` is *observation*: an out-of-process ``lsof`` poll
  that also sees subprocesses this module cannot reach.

The two are independent on purpose. One polling loop can miss a short-lived
connection between samples; one in-process patch cannot see a child process.
Together they answer "how do you know?" with something better than an
assertion.

Unlike the sandbox (which blocks *all* egress), this guard must let loopback
through: the whole system depends on reaching a local inference server on
127.0.0.1. "Local is allowed, everything else is refused" is the actual claim,
and it is what this enforces.

Following ``src/core/sandbox.py``, this patches socket *methods* rather than
replacing ``socket.socket`` itself - ``ssl.py`` does ``class SSLSocket(socket)``
at import time, so swapping the class breaks TLS imports in confusing ways.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any, Callable

from src import config
from src.contracts import AuditEventType
from src.io import audit


class ExternalCallBlocked(RuntimeError):
    """Raised when the application attempts a non-local network connection."""


_LOCK = threading.Lock()
_installed = False
_originals: dict[str, Callable[..., Any]] = {}

# Populated on install so the monitor and /api/network can report the same set.
_allowed_hosts: set[str] = set()


def _load_allowed() -> set[str]:
    configured = config.get("audit.allowed_hosts", []) or []
    allowed = {str(host).strip().lower() for host in configured if str(host).strip()}
    # Loopback is always permitted - the local inference server lives there, and
    # a config that forgot to list it would break the system rather than secure it.
    allowed.update({"127.0.0.1", "::1", "localhost"})
    return allowed


def is_local(host: str | None) -> bool:
    """True if `host` is loopback or explicitly allow-listed."""
    if not host:
        return False

    candidate = str(host).strip().lower()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]  # bracketed IPv6 literal

    if candidate in _allowed_hosts:
        return True

    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Not an IP literal. Hostnames are only allowed by exact match above -
        # we deliberately do not resolve them, because resolving is itself a
        # network operation and a DNS answer is attacker-influenced.
        return False


def _host_from_address(address: Any) -> str | None:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    if isinstance(address, str):
        return address
    return None


def _refuse(host: str | None, operation: str) -> None:
    """Record the attempt, then raise. Recording happens first, deliberately."""
    audit.emit(
        AuditEventType.NETWORK,
        f"BLOCKED external {operation} to {host!r}",
        actor="netguard",
        external=True,
        host=host,
        operation=operation,
    )
    raise ExternalCallBlocked(
        f"External network call to {host!r} was blocked by the egress guard "
        f"(operation: {operation}). Allowed: {', '.join(sorted(_allowed_hosts))}."
    )


def install() -> None:
    """Patch the socket layer. Idempotent."""
    global _installed
    with _LOCK:
        if _installed:
            return

        _allowed_hosts.clear()
        _allowed_hosts.update(_load_allowed())

        _originals["connect"] = socket.socket.connect
        _originals["connect_ex"] = socket.socket.connect_ex
        _originals["create_connection"] = socket.create_connection
        _originals["getaddrinfo"] = socket.getaddrinfo

        def guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
            host = _host_from_address(address)
            if not is_local(host):
                _refuse(host, "connect")
            return _originals["connect"](self, address, *args, **kwargs)

        def guarded_connect_ex(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
            host = _host_from_address(address)
            if not is_local(host):
                _refuse(host, "connect_ex")
            return _originals["connect_ex"](self, address, *args, **kwargs)

        def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
            host = _host_from_address(address)
            if not is_local(host):
                _refuse(host, "create_connection")
            return _originals["create_connection"](address, *args, **kwargs)

        def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            # DNS resolution for a non-local name is itself egress, and is the
            # first thing an accidental cloud SDK call would do.
            if not is_local(host):
                _refuse(str(host) if host is not None else None, "getaddrinfo")
            return _originals["getaddrinfo"](host, *args, **kwargs)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

        _installed = True


def uninstall() -> None:
    """Restore the original socket layer. Used by tests."""
    global _installed
    with _LOCK:
        if not _installed:
            return
        socket.socket.connect = _originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = _originals["connect_ex"]  # type: ignore[method-assign]
        socket.create_connection = _originals["create_connection"]  # type: ignore[assignment]
        socket.getaddrinfo = _originals["getaddrinfo"]  # type: ignore[assignment]
        _originals.clear()
        _installed = False


def is_installed() -> bool:
    return _installed


def allowed_hosts() -> list[str]:
    return sorted(_allowed_hosts) if _installed else sorted(_load_allowed())
