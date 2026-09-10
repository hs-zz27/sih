"""M1 - local inference client.

A thin HTTP client against a **local** inference server. There is no provider
SDK here and there never will be: Ollama and the OpenAI-compatible servers
(vLLM, llama.cpp, LM Studio) both speak plain JSON over HTTP, so `httpx` is the
whole dependency.

Two rules this module enforces at runtime, not just in prose:

1. The endpoint and model name come from ``config.yaml``. Swapping the laptop
   Ollama for the GPU box is a one-line config change; no application code moves.
2. The endpoint must resolve to a host on ``audit.allowed_hosts`` (or a private
   LAN address). Point it at a public host and the client raises before a single
   byte leaves the machine. This is the sovereign claim made executable.
"""

from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from src import config


class SovereigntyError(RuntimeError):
    """Raised when the configured endpoint is not local.

    This is a P0 bug class: it means the system was about to talk to something
    off this machine. Fail loudly rather than degrade quietly.
    """


class InferenceError(RuntimeError):
    """The local model server is unreachable or returned an error."""


@dataclass
class Message:
    """One chat turn. Deliberately not a Pydantic contract - internal to core."""

    role: str  # system | user | assistant
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Completion:
    """What one model call produced, plus what it cost us."""

    text: str
    model: str
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Endpoint sovereignty check
# ---------------------------------------------------------------------------


def endpoint_is_local(endpoint: str, allowed_hosts: list[str] | None = None) -> bool:
    """True when ``endpoint``'s host is loopback, private-LAN, or allow-listed.

    A GPU box on the same LAN is acceptable - it is still inside the air gap.
    Anything routable on the public internet is not.
    """
    host = (urlparse(endpoint).hostname or "").strip().lower()
    if not host:
        return False

    allowed = {str(h).strip().lower() for h in (allowed_hosts if allowed_hosts is not None else config.get("audit.allowed_hosts", []) or [])}
    if host in allowed:
        return True

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal. Only bare hostnames and .local names are trusted;
        # anything with a public-looking suffix is rejected.
        return host in {"localhost"} or host.endswith(".local")

    return address.is_loopback or address.is_private or address.is_link_local


def assert_local_endpoint(endpoint: str) -> None:
    if not endpoint_is_local(endpoint):
        raise SovereigntyError(
            f"Refusing to use non-local inference endpoint {endpoint!r}. "
            f"Allowed hosts: {config.get('audit.allowed_hosts', [])}. "
            "The sovereign claim forbids any call off this machine."
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Config-driven client over a local inference server.

    ``api_style`` selects the wire format:

    * ``ollama``            -> ``POST {endpoint}/api/chat``
    * ``openai-compatible`` -> ``POST {endpoint}/v1/chat/completions`` (vLLM et al.)

    Nothing else in the codebase knows which one is in use.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        api_style: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.endpoint = (endpoint or config.get("inference.endpoint", "")).rstrip("/")
        self.api_style = (api_style or config.get("inference.api_style", "ollama")).strip().lower()
        self.timeout_s = float(timeout_s if timeout_s is not None else config.get("inference.request_timeout_s", 120))

        assert_local_endpoint(self.endpoint)

        if self.api_style not in {"ollama", "openai-compatible"}:
            raise ValueError(
                f"Unknown inference.api_style {self.api_style!r}; expected 'ollama' or 'openai-compatible'"
            )

        # trust_env=False stops httpx picking up HTTP_PROXY/HTTPS_PROXY from the
        # environment. A proxy would route our 'local' call off the machine.
        self._client = httpx.Client(timeout=self.timeout_s, trust_env=False)
        # (checked_at, available, models) - see probe()
        self._probe_cache: tuple[float, bool, list[str]] | None = None

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- health -----------------------------------------------------------

    def probe(self, max_age_s: float = 5.0) -> tuple[bool, list[str]]:
        """Reachability and the models the local server has pulled, in one call.

        Cached briefly. A refused connection on Windows loopback costs about two
        seconds, so a UI polling ``/api/health`` while Ollama is down would stall
        its panel on every tick; and asking twice for what one request answers
        doubled that for no reason.
        """
        now = time.monotonic()
        if self._probe_cache is not None and (now - self._probe_cache[0]) < max_age_s:
            return self._probe_cache[1], self._probe_cache[2]

        models: list[str] = []
        available = False
        try:
            if self.api_style == "ollama":
                response = self._client.get(f"{self.endpoint}/api/tags", timeout=3.0)
                response.raise_for_status()
                models = [m.get("name", "") for m in response.json().get("models", [])]
            else:
                response = self._client.get(f"{self.endpoint}/v1/models", timeout=3.0)
                response.raise_for_status()
                models = [m.get("id", "") for m in response.json().get("data", [])]
            available = True
        except (httpx.HTTPError, ValueError):
            available, models = False, []

        self._probe_cache = (now, available, models)
        return available, models

    def is_available(self) -> bool:
        """Cheap reachability probe. Used to report status, never to retry."""
        return self.probe()[0]

    def available_models(self) -> list[str]:
        """Model names the local server currently has loaded or pulled."""
        return self.probe()[1]

    # -- generation -------------------------------------------------------

    def chat(
        self,
        messages: list[Message],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> Completion:
        """One non-streaming completion."""
        started = time.perf_counter()
        payload = self._build_payload(messages, model, temperature, max_tokens, stop, stream=False)
        url = self._chat_url()

        try:
            response = self._client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"Local model server returned {exc.response.status_code} for model {model!r}. "
                f"Is it pulled? Try: ollama pull {model}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InferenceError(
                f"Cannot reach local inference server at {self.endpoint}. Is it running? ({exc})"
            ) from exc

        return self._parse_completion(body, model, int((time.perf_counter() - started) * 1000))

    def stream(
        self,
        messages: list[Message],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> Iterator[str]:
        """Yield text chunks as the local model produces them."""
        payload = self._build_payload(messages, model, temperature, max_tokens, stop, stream=True)
        url = self._chat_url()

        try:
            with self._client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    chunk = self._parse_stream_line(line)
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise InferenceError(f"Streaming failed against {self.endpoint}: {exc}") from exc

    # -- wire format ------------------------------------------------------

    def _chat_url(self) -> str:
        if self.api_style == "ollama":
            return f"{self.endpoint}/api/chat"
        return f"{self.endpoint}/v1/chat/completions"

    def _build_payload(
        self,
        messages: list[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        stop: list[str] | None,
        stream: bool,
    ) -> dict[str, Any]:
        if temperature is None:
            temperature = float(config.get("agent.temperature", 0.2))
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "stream": stream,
        }

        if self.api_style == "ollama":
            options: dict[str, Any] = {"temperature": temperature}
            if max_tokens is not None:
                options["num_predict"] = max_tokens
            if stop:
                options["stop"] = stop
            body["options"] = options
            # Keep the weights resident between steps so the demo has no reload stall.
            body["keep_alive"] = config.get("inference.keep_alive", "30m")
        else:
            body["temperature"] = temperature
            if max_tokens is not None:
                body["max_tokens"] = max_tokens
            if stop:
                body["stop"] = stop

        return body

    def _parse_completion(self, body: dict[str, Any], model: str, duration_ms: int) -> Completion:
        if self.api_style == "ollama":
            return Completion(
                text=(body.get("message") or {}).get("content", ""),
                model=body.get("model", model),
                duration_ms=duration_ms,
                prompt_tokens=int(body.get("prompt_eval_count", 0) or 0),
                completion_tokens=int(body.get("eval_count", 0) or 0),
                raw=body,
            )

        choices = body.get("choices") or [{}]
        return Completion(
            text=(choices[0].get("message") or {}).get("content", ""),
            model=body.get("model", model),
            duration_ms=duration_ms,
            prompt_tokens=int((body.get("usage") or {}).get("prompt_tokens", 0) or 0),
            completion_tokens=int((body.get("usage") or {}).get("completion_tokens", 0) or 0),
            raw=body,
        )

    def _parse_stream_line(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""

        if self.api_style == "openai-compatible":
            if not line.startswith("data:"):
                return ""
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                return ""
            try:
                delta = (json.loads(data).get("choices") or [{}])[0].get("delta") or {}
            except json.JSONDecodeError:
                return ""
            return delta.get("content", "") or ""

        try:  # Ollama streams bare JSON objects, one per line
            return (json.loads(line).get("message") or {}).get("content", "") or ""
        except json.JSONDecodeError:
            return ""


_client: LLMClient | None = None


def get_client() -> LLMClient:
    """Process-wide client. Built on first use so import never touches the network."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_client() -> None:
    """Drop the cached client - used by tests and after a config change."""
    global _client
    if _client is not None:
        _client.close()
    _client = None
