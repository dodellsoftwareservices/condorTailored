"""Custom OpenAI-compatible provider model fetcher.

Hits GET {base_url}/models with an optional Bearer key — the standard model
list endpoint every OpenAI-compatible API exposes (Venice AI, Together,
Fireworks, vLLM, TGI, ...). A successful fetch doubles as validation of both
the URL and the API key during the custom-endpoint setup flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

log = logging.getLogger(__name__)

# Per-candidate budget. Candidates are probed concurrently, so this is also
# roughly the worst case for the whole fetch — the user is staring at a
# "validating..." message the entire time.
FETCH_TIMEOUT_SECONDS = 8

# Hosts that hand out cloud instance credentials to anything that can issue an
# HTTP GET. Always refused: no legitimate model endpoint lives here.
BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

# Opt-in for multi-tenant deployments: also refuse loopback and RFC1918
# targets. Off by default because pointing at a local Ollama/vLLM/LM Studio
# server is the single most common use of this feature.
_BLOCK_PRIVATE_ENV = "CUSTOM_LLM_BLOCK_PRIVATE_URLS"

# /models entries whose declared type isn't one of these are not chat models.
# Providers disagree on the field name (`type` on Venice/Together, `object` on
# stock OpenAI) and on the vocabulary, so accept every spelling of "text in,
# text out" we've seen and treat other declared values as non-chat.
CHAT_MODEL_TYPES = frozenset({"text", "chat", "language", "llm"})

# Values that technically occupy the type field but say nothing about
# modality — stock OpenAI stamps object="model" on embeddings, Whisper and
# DALL·E alike. Treat these as "not declared" and fall through to the id.
UNINFORMATIVE_MODEL_TYPES = frozenset({"model", "list", "object"})

# Fallback for providers that return no type at all: drop ids that are
# unmistakably a non-chat modality.
_NON_CHAT_MARKERS = (
    "embed",
    "rerank",
    "whisper",
    "tts",
    "text-to-speech",
    "speech",
    "voice",
    "moderation",
    "guard",
    "upscal",
    "stable-diffusion",
    "flux",
    "dall-e",
    "sdxl",
    "image",
    "vision-encoder",
    "clip",
    "ocr",
)


# urlparse is happy to call "not a url at all" a netloc, so the hostname needs
# its own shape check: dotted labels, or an IPv6 literal in brackets.
_HOSTNAME_RE = re.compile(
    r"^(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?|\[[0-9A-Fa-f:.]+\])$"
)


class CustomProviderError(Exception):
    """Raised when the endpoint can't be validated. Message is user-facing."""


def _block_private() -> bool:
    return os.environ.get(_BLOCK_PRIVATE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _reject_if_blocked(host: str) -> None:
    """Raise ValueError when `host` points somewhere we refuse to fetch from.

    Link-local addresses (the 169.254.169.254 cloud metadata range) are always
    refused. Loopback and private ranges are allowed by default so local model
    servers work, and can be refused too by setting
    CUSTOM_LLM_BLOCK_PRIVATE_URLS=true on shared/multi-tenant deployments.

    This catches obvious bad input before making a request. The probe's
    resolver checks the actual DNS answers again at connection time.
    """
    lowered = host.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES:
        raise ValueError(f"'{host}' is not an allowed endpoint host.")

    strict = _block_private()
    addresses: list[str] = []
    try:
        ipaddress.ip_address(lowered.strip("[]"))
        addresses = [lowered.strip("[]")]
    except ValueError:
        # DNS is resolved by the connector, where its actual socket addresses
        # can be checked without blocking the event loop or racing another
        # resolution.
        pass

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_link_local:
            raise ValueError(
                f"'{host}' resolves to a link-local address, which is blocked "
                "(cloud instance metadata lives there)."
            )
        if strict and (ip.is_loopback or ip.is_private or ip.is_reserved):
            raise ValueError(
                f"'{host}' resolves to a private address and "
                f"{_BLOCK_PRIVATE_ENV} is enabled."
            )


class _ProbeResolver(AbstractResolver):
    """Reject forbidden DNS answers where the socket address is selected.

    Validating a hostname before aiohttp resolves it leaves a DNS rebinding
    gap. The connector uses these checked answers directly for its sockets.
    """

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        _reject_if_blocked(host)
        answers = await self._resolver.resolve(host, port, family)
        strict = _block_private()
        for answer in answers:
            raw = answer["host"]
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise OSError("Provider DNS returned an invalid address") from exc
            if ip.is_link_local or (strict and not ip.is_global):
                raise OSError("Provider DNS returned a blocked address")
        return answers

    async def close(self):
        await self._resolver.close()


def normalize_base_url(raw: str) -> str:
    """Coerce user input into a usable OpenAI-compatible base URL.

    Adds https:// when no scheme is given and strips trailing slashes and
    endpoint suffixes users commonly paste (/chat/completions, /models).
    Raises ValueError for input that can't be a URL or that targets a
    blocked host.
    """
    url = raw.strip()
    if not url:
        raise ValueError("URL is empty.")
    if "://" not in url:
        url = f"https://{url}"
    url = url.rstrip("/")

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError(f"'{raw.strip()}' is not a valid http(s) URL.") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or not hostname
        or not _HOSTNAME_RE.match(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"'{raw.strip()}' is not a valid http(s) URL.")
    _reject_if_blocked(hostname)

    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def _is_chat_model(entry: dict) -> bool:
    """Best-effort guess at whether a /models entry can serve chat completions.

    Providers return embeddings, rerankers, TTS and image models from the same
    endpoint. Picking one of those succeeds here and then fails at prompt time
    with an opaque upstream error, so filter them out while we still have the
    metadata to do it.
    """
    declared = entry.get("type") or entry.get("object")
    if isinstance(declared, str) and declared.strip():
        normalized = declared.strip().lower()
        if normalized not in UNINFORMATIVE_MODEL_TYPES:
            return normalized in CHAT_MODEL_TYPES

    capabilities = entry.get("capabilities")
    if isinstance(capabilities, dict):
        for flag in ("chat", "chat_completions", "supportsChatCompletions"):
            if flag in capabilities:
                return bool(capabilities[flag])

    model_id = entry.get("id", "").lower()
    return not any(marker in model_id for marker in _NON_CHAT_MARKERS)


def _extract_model_ids(payload: object, chat_only: bool = True) -> list[str]:
    """Pull model ids from an OpenAI-format /models response.

    Expects {"data": [{"id": "..."}, ...]}. Returns sorted, deduped ids;
    empty list when the payload doesn't match the expected shape. With
    ``chat_only`` (the default) non-chat modalities are dropped.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    ids = {
        entry["id"].strip()
        for entry in data
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry["id"].strip()
        and (not chat_only or _is_chat_model(entry))
    }
    return sorted(ids, key=str.lower)


async def _probe(
    session: aiohttp.ClientSession, candidate: str, headers: dict
) -> tuple[list[str], CustomProviderError | None]:
    """Fetch one candidate's model list. Returns (models, error)."""
    models_url = f"{candidate}/models"
    try:
        async with session.get(
            models_url, headers=headers, allow_redirects=False
        ) as resp:
            if 300 <= resp.status < 400:
                return [], CustomProviderError(
                    f"{models_url} redirected (HTTP {resp.status}). "
                    "Enter the provider's final /v1 URL directly."
                )
            if resp.status in (401, 403):
                return [], CustomProviderError(
                    f"the provider at {candidate} rejected the API key "
                    f"(HTTP {resp.status}). Check the key and try again."
                )
            if resp.status == 404:
                return [], CustomProviderError(
                    f"no /models endpoint at {candidate} (HTTP 404). "
                    "Check the base URL — it usually ends in /v1."
                )
            if resp.status != 200:
                return [], CustomProviderError(
                    f"the provider at {candidate} returned HTTP {resp.status}."
                )
            payload = await resp.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return [], CustomProviderError(
            f"could not reach {models_url} ({type(e).__name__}). "
            "Check the URL and that the server is up."
        )

    model_ids = _extract_model_ids(payload)
    if not model_ids:
        # An endpoint that answered but listed only embeddings/image models is
        # a different failure from one that isn't OpenAI-shaped at all.
        if _extract_model_ids(payload, chat_only=False):
            return [], CustomProviderError(
                f"{models_url} returned models, but none of them are chat "
                "models (only embeddings/image/audio)."
            )
        return [], CustomProviderError(
            f"{models_url} responded but did not return an "
            "OpenAI-compatible model list."
        )
    return model_ids, None


async def fetch_models(base_url: str, api_key: str = "") -> tuple[str, list[str]]:
    """Validate an OpenAI-compatible endpoint and return its chat model list.

    Probes {base_url}/models and {base_url}/v1/models concurrently — the second
    covers users who pasted the host root (e.g. https://api.venice.ai instead
    of https://api.venice.ai/api/v1) — and prefers the first candidate that
    works. Returns (resolved_base_url, model_ids).

    Raises CustomProviderError with a user-facing message on any failure —
    unreachable host, rejected key, or a non-OpenAI-shaped response.
    """
    try:
        base_url = normalize_base_url(base_url)
    except ValueError as exc:
        raise CustomProviderError(str(exc)) from None

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    candidates = [base_url]
    if not base_url.endswith("/v1"):
        candidates.append(f"{base_url}/v1")

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(resolver=_ProbeResolver())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        results = await asyncio.gather(
            *(_probe(session, candidate, headers) for candidate in candidates)
        )

    # Candidate order is preference order: the URL the user actually typed wins
    for candidate, (model_ids, _) in zip(candidates, results):
        if model_ids:
            log.info(
                "Custom provider validated: %s (%d chat models)",
                candidate,
                len(model_ids),
            )
            return candidate, model_ids

    for _, error in results:
        if error:
            raise error
    raise CustomProviderError("no reachable endpoint found.")


def model_token(model_id: str) -> str:
    """Short stable token for a model id, for use in callback_data.

    Telegram caps callback_data at 64 bytes and model ids are unbounded. A
    content-derived token also survives the model list being refetched between
    render and tap, which a bare list index does not.
    """
    return hashlib.blake2s(model_id.encode("utf-8"), digest_size=4).hexdigest()


def find_by_token(model_ids: list[str], token: str) -> str | None:
    """Resolve a token produced by :func:`model_token` back to its model id."""
    for model_id in model_ids:
        if model_token(model_id) == token:
            return model_id
    return None


def format_model_label(model_id: str, limit: int = 38) -> str:
    """Short label for inline keyboard buttons.

    Truncates through the middle: for ids like
    ``meta-llama/Llama-3.3-70B-Instruct-Turbo-Free`` the tail is what
    distinguishes one variant from another, so cutting it off is exactly the
    wrong end to lose.
    """
    if len(model_id) <= limit:
        return model_id
    keep = limit - 1  # room for the ellipsis
    head = (keep + 1) // 2
    tail = keep - head
    return f"{model_id[:head]}…{model_id[-tail:]}"
