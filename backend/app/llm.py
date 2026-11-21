"""Key pool with ordered failover + LiteLLM response cache."""
import logging
import os

import litellm

log = logging.getLogger("llm")

MODEL = os.getenv("LLM_MODEL", "gemini/gemini-3.5-flash-lite")
KEYS = [k.strip() for k in os.getenv("LLM_API_KEYS", "").split(",") if k.strip()]
TIMEOUT_S = 30
SIMULATABLE = {429, 500, 502, 503}

litellm.suppress_debug_info = True
# Disk cache lives on the /data volume in Docker. LiteLLM's cache key is built from the
# OpenAI-style request params (model + messages + ...); api_key is a LiteLLM-owned kwarg and
# is NOT part of the key, so an answer fetched via key_2 is a hit when retried via key_1.
litellm.cache = litellm.Cache(type="disk", disk_cache_dir=os.getenv("CACHE_DIR", ".cache/litellm"))


class AllKeysFailed(Exception):
    pass


class LLMRejected(Exception):
    """Non-retryable provider error (bad request, safety block, empty reply...)."""


def parse_simulation(header: str | None) -> dict[str, int]:
    """'key_1=429,key_2=500' / '*=429' -> {'key_1': 429, ...}. Raises ValueError if malformed."""
    if not header or not header.strip():
        return {}
    out = {}
    for part in header.split(","):
        key_id, sep, status = (s.strip() for s in part.partition("="))
        if not sep or not key_id or not status.isdigit() or int(status) not in SIMULATABLE:
            raise ValueError(part)
        out[key_id] = int(status)
    return out


def _simulated_error(status: int) -> Exception:
    msg, args = f"simulated HTTP {status}", ("gemini", MODEL)
    if status == 429:
        return litellm.RateLimitError(msg, *args)
    if status == 502:
        return litellm.BadGatewayError(msg, *args)
    if status == 503:
        return litellm.ServiceUnavailableError(msg, *args)
    return litellm.InternalServerError(msg, *args)


KEY_ERRORS = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.BadGatewayError,
    litellm.InternalServerError,
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.AuthenticationError,  # revoked/invalid key: also a per-key failure
    litellm.PermissionDeniedError,
)


def _is_key_failure(e: Exception) -> bool:
    if isinstance(e, KEY_ERRORS):
        return True
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and (status in (401, 403, 429) or status >= 500):
        return True
    text = str(e).lower()
    return any(s in text for s in ("quota", "resource_exhausted", "resource exhausted", "api key not valid"))


def complete(messages: list[dict], simulate: dict[str, int]) -> dict:
    """Try keys in order. Per attempt: simulated failure first, then LiteLLM (cache lookup, then provider)."""
    failures = 0
    for i, key in enumerate(KEYS, start=1):
        key_id = f"key_{i}"
        try:
            status = simulate.get(key_id, simulate.get("*"))
            if status:
                raise _simulated_error(status)
            # After a key has failed in this request, skip the cache *read* so the failover is
            # really exercised (and key_id/fallback_attempts are exact). The answer is still stored.
            resp = litellm.completion(
                model=MODEL, messages=messages, api_key=key, timeout=TIMEOUT_S,
                cache={"no-cache": failures > 0},
            )
        except Exception as e:
            if not _is_key_failure(e):
                log.warning("%s non-retryable %s", key_id, type(e).__name__)
                raise LLMRejected(type(e).__name__) from None
            failures += 1
            log.warning("%s failed (%s, status=%s); trying next key", key_id, type(e).__name__, getattr(e, "status_code", None))
            continue

        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            raise LLMRejected("empty reply")
        hit = bool((getattr(resp, "_hidden_params", None) or {}).get("cache_hit"))
        usage = resp.usage
        return {
            "reply": reply,
            "resolution_source": "LLM_CACHE" if hit else "LIVE_INFERENCE",
            "model": MODEL,
            "input_tokens": int(usage.prompt_tokens or 0),
            "output_tokens": int(usage.completion_tokens or 0),
            "key_id": None if hit else key_id,
            "fallback_attempts": 0 if hit else failures,
        }
    raise AllKeysFailed()
