"""The defect tests for hard rules #1 and #4.

PROPOSAL.md ss2: an `anthropic` / `openai` / hosted-inference reference anywhere
in the shipped code is a defect, not a dependency, and no runtime code may reach
a host off this machine.

Harkamal's Step-0 suite checks that no provider SDK is *imported at runtime*.
These check the stronger property: that none is present in the source at all, so
the build fails the moment one is added rather than the first time it executes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

BANNED_IMPORTS = [
    "anthropic",
    "openai",
    "google.generativeai",
    "google_generativeai",
    "cohere",
    "mistralai",
    "replicate",
    "together",
    "groq",
    "litellm",
    "langchain_openai",
    "langchain_anthropic",
]

BANNED_HOSTS = [
    "api.openai.com",
    "api.anthropic.com",
    "api-inference.huggingface.co",
    "generativelanguage.googleapis.com",
    "api.cohere.ai",
    "api.mistral.ai",
]

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "vendor", "models", "data", "wheelhouse"}
SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".txt", ".toml", ".js", ".ts", ".jsx", ".tsx"}


def _source_files(include_tests: bool = True) -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if path.name == Path(__file__).name:
            continue  # this file names the banned strings on purpose
        if not include_tests and "tests" in path.parts:
            continue
        files.append(path)
    return files


def test_no_provider_sdk_imports():
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(" + "|".join(re.escape(m) for m in BANNED_IMPORTS) + r")\b",
        re.MULTILINE,
    )
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {match.group(0).strip()}"
        for path in _source_files()
        if path.suffix == ".py"
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, "Cloud LLM SDK imports found (hard rule #1):\n" + "\n".join(offenders)


def test_no_provider_sdks_in_requirements():
    requirements = REPO_ROOT / "requirements.txt"
    if not requirements.exists():
        pytest.skip("no requirements.txt")

    banned = set(BANNED_IMPORTS) | {name.replace("_", "-") for name in BANNED_IMPORTS}
    offenders = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#")[0].strip()
        if not stripped:
            continue
        if re.split(r"[=<>!\[\s]", stripped, maxsplit=1)[0].lower() in banned:
            offenders.append(stripped)
    assert not offenders, f"Cloud LLM SDKs in requirements.txt: {offenders}"


def test_no_hosted_inference_hosts_in_shipped_source():
    """Tests are excluded: they name these hosts as the cases they reject."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {host}"
        for path in _source_files(include_tests=False)
        for host in BANNED_HOSTS
        if host in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, "Hosted inference endpoints referenced:\n" + "\n".join(offenders)


def test_application_code_never_clears_the_offline_flags():
    """Only ``scripts/fetch_models.py`` may go online, and it is never imported.

    Anything under ``src/`` that unsets HF_HUB_OFFLINE would let the embedding
    model reach for the network on a cold start - exactly the failure the
    Wi-Fi-off rehearsal is meant to catch, but silently.
    """
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if 'pop("HF_HUB_OFFLINE"' in text or 'HF_HUB_OFFLINE"] = "0"' in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"Offline flags are cleared inside src/: {offenders}"


def test_configured_endpoint_is_local():
    """The endpoint that ships in config.yaml must be loopback."""
    from src import config
    from src.core.llm import endpoint_is_local

    endpoint = config.get("inference.endpoint")
    assert endpoint, "inference.endpoint must be set"
    assert endpoint_is_local(endpoint), f"config.yaml points at a non-local endpoint: {endpoint}"


def test_allowed_hosts_contains_no_public_host():
    from src import config
    from src.core.llm import endpoint_is_local

    for host in config.get("audit.allowed_hosts", []) or []:
        # IPv6 literals must be bracketed to parse as a URL authority.
        authority = f"[{host}]" if ":" in host else host
        assert endpoint_is_local(f"http://{authority}", allowed_hosts=[]), (
            f"audit.allowed_hosts contains a non-local host: {host}"
        )


def test_sandbox_denies_egress_by_default():
    """config.yaml must ship with sandbox networking off."""
    from src import config

    assert config.get("sandbox.network") is False
