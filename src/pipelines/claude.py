"""
Claude LLM adapter for modular pipelines.

Uses the official Anthropics client to call Claude (Sonnet 4.5 / 3.5) models.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

try:
    import anthropic
except ImportError:  # pragma: no cover - optional dependency
    anthropic = None  # type: ignore

from ..config import AppConfig
from ..logging_config import get_logger
from .base import LLMComponent, LLMResponse

logger = get_logger(__name__)


class ClaudeLLMAdapter(LLMComponent):
    """
    Simple Claude chat adapter for pipeline use.

    Options:
      - model: Claude model name (default: claude-3-5-sonnet-20240620)
      - temperature: float (default 0.6)
      - max_tokens: int (default 256)
      - system: optional system prompt (falls back to config.llm.prompt)
      - api_key: override Anthropics API key (otherwise CL.AUDE_API_KEY env)
    """

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config or {}
        self._pipeline_defaults = options or {}
        self._client = None

    async def start(self) -> None:
        if anthropic is None:
            logger.warning("anthropic package not installed; Claude adapter will not run")
        else:
            logger.debug("Claude LLM adapter initialized", component=self.component_key)

    async def stop(self) -> None:
        # Anthropic client is stateless; nothing to close.
        self._client = None

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        # Client is created lazily on first generate; no per-call prep needed.
        return

    async def close_call(self, call_id: str) -> None:
        return

    def _build_client(self, api_key: Optional[str]):
        if anthropic is None:
            raise RuntimeError("anthropic package is not installed. Run `pip install anthropic`.")
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def _merge_options(self, runtime: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**self._provider_defaults, **self._pipeline_defaults, **(runtime or {})}
        merged.setdefault("model", "claude-3-5-sonnet-20240620")
        merged.setdefault("temperature", 0.6)
        merged.setdefault("max_tokens", 256)
        return merged

    async def generate(
        self,
        call_id: str,
        transcript: str,
        context: Dict[str, Any],
        options: Dict[str, Any],
    ) -> LLMResponse:
        merged = self._merge_options(options or {})
        api_key = merged.get("api_key") or self._provider_defaults.get("api_key")
        if not api_key:
            raise RuntimeError("Claude LLM requires an api_key (set CLAUDE_API_KEY or pipeline options.api_key)")

        client = self._build_client(api_key)

        system_prompt = merged.get("system") or getattr(getattr(self._app_config, "llm", None), "prompt", None)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": transcript})

        # Run the API call in a thread to avoid blocking the event loop.
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model=merged["model"],
                max_tokens=merged.get("max_tokens", 256),
                temperature=merged.get("temperature", 0.6),
                messages=messages,
            ),
        )

        text_parts = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))

        text = "".join(text_parts).strip()
        logger.debug("Claude response", call_id=call_id, model=merged["model"], preview=text[:80])
        return LLMResponse(text=text, tool_calls=[], metadata={"model": merged["model"]})
