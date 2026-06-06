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

import random
import sys
import time
from dataclasses import dataclass
from typing import Any


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
