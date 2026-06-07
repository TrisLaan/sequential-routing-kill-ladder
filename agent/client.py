"""LLM client wrapper + deterministic mock.

Both clients expose the SAME method:

    complete(model, messages, *, reasoning_effort=None) -> LLMResponse

so the baseline loop is agnostic to which one it's driving (duck typing).

Reasoning-model handling (gpt-5.4): when `reasoning_effort` is provided we send
it and OMIT `temperature` entirely — gpt-5.4 returns a 400 "temperature
unsupported" if temperature is sent. The returned usage.completion_tokens from
Chat Completions already includes reasoning tokens, so logging it verbatim is
correct. Non-reasoning callers (reasoning_effort=None) get temperature=0; that
path is unused by baseline_strong but kept for the cheap model in later phases.
"""
from __future__ import annotations

import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# OpenRouter is OpenAI Chat Completions-compatible; out-of-family models route
# through it with a single key. Default callers (gpt-5.4/gpt-4.1-mini) never use
# this path — they hit OpenAI directly via OPENAI_API_KEY.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _read_env_var(name: str) -> str | None:
    """Return `name` from the process env, else from a project-root .env file.

    Zero-dependency (python-dotenv not installed). Process env wins so an
    exported value can override the file. Blank/whitespace values count as unset
    so an untouched placeholder line fails loudly rather than sending an empty
    key. Looks for .env next to the repo root (parent of this agent/ package).
    """
    val = os.environ.get(name)
    if val and val.strip():
        return val.strip()
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() == name:
                cleaned = raw.strip().strip('"').strip("'")
                return cleaned or None
    return None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int


class OpenAIClient:
    """Thin wrapper over the OpenAI Python SDK, Chat Completions surface."""

    def __init__(
        self,
        *,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        # Imported lazily so the mock-only $0 validation path never needs the
        # SDK installed or a key present.
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            OpenAI,
            RateLimitError,
        )

        # SDK's own retries disabled (max_retries=0) — we own the retry loop so
        # backoff is explicit and logged, with no hidden double-retrying.
        # base_url/api_key default to None -> the existing behavior: OpenAI's own
        # endpoint, key from OPENAI_API_KEY (gpt-5.4 / gpt-4.1-mini path, UNCHANGED).
        # When set (out-of-family via OpenRouter), they point the SAME SDK surface
        # elsewhere — no other code path changes.
        if base_url is not None:
            self._client = OpenAI(max_retries=0, base_url=base_url, api_key=api_key)
        else:
            self._client = OpenAI(max_retries=0)  # reads OPENAI_API_KEY from env
        # Retry only transient faults. Auth (401) and bad-request (400, e.g. an
        # unsupported param) are NOT retryable — they'd fail identically forever.
        self._retryable = (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        )
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay

    @classmethod
    def for_openrouter(cls, **kw: Any) -> "OpenAIClient":
        """Client pointed at OpenRouter, key read from env/.env (never hardcoded).

        Raises loudly if OPENROUTER_API_KEY is missing/blank so a probe run can
        never silently fall back to the OpenAI key or send an empty credential.
        """
        key = _read_env_var("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is missing or blank. Paste your key into the "
                ".env file at the project root (OPENROUTER_API_KEY=sk-or-...)."
            )
        return cls(base_url=OPENROUTER_BASE_URL, api_key=key, **kw)

    @staticmethod
    def _retry_after_s(exc: Exception) -> float | None:
        """Server-suggested wait from a Retry-After header, if present."""
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        val = headers.get("retry-after")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
            # temperature deliberately omitted for reasoning models.
        else:
            kwargs["temperature"] = 0

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                usage = resp.usage
                return LLMResponse(
                    text=content,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
            except self._retryable as e:
                if attempt == self._max_retries:
                    raise
                # exponential backoff with jitter; honor Retry-After if given
                backoff = min(self._max_delay, self._base_delay * (2 ** attempt))
                delay = self._retry_after_s(e) or backoff + random.uniform(0, backoff * 0.25)
                print(
                    f"  [retry {attempt + 1}/{self._max_retries}] "
                    f"{type(e).__name__}: {e} — sleeping {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
        # Unreachable: the loop either returns or raises on the final attempt.
        raise RuntimeError("retry loop exited without returning")


class MockClient:
    """Returns a fixed, pre-scripted sequence of responses — no network, $0.

    `script` is a list of (text, prompt_tokens, completion_tokens) tuples,
    returned one per complete() call in order. Each call is also recorded in
    `self.calls` so tests can assert on what the loop sent.
    """

    def __init__(self, script: list[tuple[str, int, int]]) -> None:
        self._script = list(script)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        if self._i >= len(self._script):
            raise AssertionError(
                f"MockClient script exhausted after {self._i} calls; "
                f"loop made more LLM calls than scripted."
            )
        text, pt, ct = self._script[self._i]
        self._i += 1
        self.calls.append(
            {"model": model, "reasoning_effort": reasoning_effort, "messages": messages}
        )
        return LLMResponse(text=text, prompt_tokens=pt, completion_tokens=ct)
