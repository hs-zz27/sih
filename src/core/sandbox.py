"""M5 - sandboxed Python execution.

Generated code runs in a subprocess with:

* a wall-clock timeout, so a runaway loop cannot stall the demo;
* a working directory it cannot escape (``sandbox.workdir``);
* stdout/stderr captured and truncated, never streamed to the console;
* **network access blocked from inside the interpreter**.

That last one is not just defence. It is sovereignty evidence: when a judge asks
whether generated code could phone home, the answer is that ``socket.socket``
raises before a connection is attempted, and the attempt is visible in the
returned result.

The block is a prelude injected ahead of the model's code. It is honest about
what it is: a guard against accidental egress from generated code, not a
security boundary against hostile code. We say exactly that in the pitch rather
than overclaiming a jail we did not build.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from src import config

# Injected ahead of every snippet. Neutralises the *connecting* half of the
# socket layer so any egress attempt raises a named error we can surface.
#
# Note what this does NOT do: it does not replace ``socket.socket`` itself.
# ``ssl.py`` does ``class SSLSocket(socket)`` at import time, so swapping the
# class out makes ``import urllib.request`` die with a confusing TypeError
# instead of a clear "network blocked". Patching the methods leaves every import
# working and still stops the connection.
_NETWORK_BLOCK_PRELUDE = '''import socket as _socket

class SandboxNetworkBlocked(RuntimeError):
    """Raised when sandboxed code attempts a network connection."""

def _blocked(*_args, **_kwargs):
    raise SandboxNetworkBlocked(
        "Network access is disabled inside the sandbox (config sandbox.network=false). "
        "This workbench makes no external calls."
    )

_socket.socket.connect = _blocked
_socket.socket.connect_ex = _blocked
_socket.socket.sendto = _blocked
_socket.create_connection = _blocked
_socket.getaddrinfo = _blocked
_socket.gethostbyname = _blocked
_socket.gethostbyname_ex = _blocked

del _socket, _blocked
'''

# Lines the prelude occupies before the model's own line 1. Computed rather than
# counted by hand so editing the prelude cannot silently desync tracebacks.
_HEADER_LINES = len((_NETWORK_BLOCK_PRELUDE + "\n").splitlines())


@dataclass
class SandboxResult:
    """Structured outcome the agent can reason about and retry on."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    timed_out: bool = False
    network_blocked: bool = False
    created_files: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.created_files is None:
            self.created_files = []

    def as_observation(self) -> str:
        """Render for the model. It has to be readable enough to debug from."""
        if self.timed_out:
            return (
                f"EXECUTION TIMED OUT after {self.duration_ms} ms. "
                "The code did not finish. Simplify it or remove long loops.\n"
                f"Partial stdout:\n{self.stdout.strip() or '(none)'}"
            )

        parts: list[str] = []
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout.rstrip()}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr.rstrip()}")
        if self.created_files:
            parts.append("files created: " + ", ".join(self.created_files))
        if not parts:
            parts.append("(no output - remember to print() the result you need)")

        status = "exit code 0" if self.ok else f"FAILED with exit code {self.exit_code}"
        return f"{status}\n" + "\n\n".join(parts)


class Sandbox:
    """Runs Python snippets under a timeout in a fixed working directory."""

    def __init__(
        self,
        workdir: Path | None = None,
        timeout_s: float | None = None,
        block_network: bool | None = None,
        max_output_chars: int = 8000,
    ) -> None:
        self.workdir = workdir or config.get_path("sandbox.workdir", "data/sandbox")
        self.timeout_s = float(timeout_s if timeout_s is not None else config.get("sandbox.timeout_s", 20))
        self.block_network = (
            block_network if block_network is not None else not config.get("sandbox.network", False)
        )
        self.max_output_chars = max_output_chars
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, code: str) -> SandboxResult:
        """Execute ``code`` and return a structured result. Never raises."""
        if not code.strip():
            return SandboxResult(ok=False, stderr="No code supplied.", exit_code=1)

        before = self._snapshot()
        source = (_NETWORK_BLOCK_PRELUDE + "\n" + code) if self.block_network else code

        # Written into the workdir (not the system temp dir) so relative paths in
        # generated code resolve where the agent expects them to.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=self.workdir, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            script_path = Path(handle.name)

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(script_path)],
                cwd=self.workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=self._child_env(),
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            stderr = self._rewrite_line_numbers(completed.stderr)

            return SandboxResult(
                ok=completed.returncode == 0,
                stdout=self._truncate(completed.stdout),
                stderr=self._truncate(stderr),
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                network_blocked="SandboxNetworkBlocked" in stderr,
                created_files=self._created_since(before),
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                ok=False,
                stdout=self._truncate(_decode(exc.stdout)),
                stderr=f"Timed out after {self.timeout_s}s.",
                exit_code=124,
                duration_ms=int(self.timeout_s * 1000),
                timed_out=True,
                created_files=self._created_since(before),
            )
        except OSError as exc:
            return SandboxResult(ok=False, stderr=f"Could not start sandbox subprocess: {exc}", exit_code=1)
        finally:
            script_path.unlink(missing_ok=True)

    # -- internals --------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        """Minimal environment. Proxy vars are stripped so nothing can tunnel out."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows needs this for sockets/stdlib
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.workdir),
            "TEMP": str(self.workdir),
            "TMP": str(self.workdir),
            # Belt and braces alongside the prelude: nothing gets a proxy to use.
            "NO_PROXY": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            # If anything does import HF/transformers in here, keep it offline.
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        return {k: v for k, v in env.items() if v}

    def _snapshot(self) -> set[str]:
        return {str(p.relative_to(self.workdir)) for p in self.workdir.rglob("*") if p.is_file()}

    def _created_since(self, before: set[str]) -> list[str]:
        return sorted(name for name in self._snapshot() - before if not name.endswith(".py"))

    def _rewrite_line_numbers(self, stderr: str) -> str:
        """Shift traceback line numbers back past the injected prelude.

        Without this the model debugs against line numbers that do not match the
        code it wrote, and burns iterations chasing a phantom offset.
        """
        if not self.block_network or not stderr:
            return stderr

        import re

        def shift(match: "re.Match[str]") -> str:
            reported = int(match.group(2))
            return f'{match.group(1)}{max(1, reported - _HEADER_LINES)}'

        return re.sub(r'(", line )(\d+)', shift, stderr)

    def _truncate(self, text: str) -> str:
        if text is None:
            return ""
        if len(text) <= self.max_output_chars:
            return text
        half = self.max_output_chars // 2
        omitted = len(text) - 2 * half
        return f"{text[:half]}\n...[{omitted} characters omitted]...\n{text[-half:]}"


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""  # type: ignore[return-value]


_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox()
    return _sandbox


def reset_sandbox() -> None:
    global _sandbox
    _sandbox = None
