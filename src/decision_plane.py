"""Low-latency System-One decision plane for voice transcripts.

This module is intentionally advisory. It enriches an existing voice turn with
typed Laya/Jev-compatible decisions but never transfers, hangs up, emails, or
executes another telephony tool by itself. Existing tool policies remain the
only execution authority.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp

from .logging_config import get_logger

logger = get_logger(__name__)

RequestFn = Callable[[str, Dict[str, Any], Dict[str, str], float], Awaitable[Dict[str, Any]]]


class VoiceDecisionPlane:
    VALID_MODES = {"off", "shadow", "advisory"}

    def __init__(
        self,
        *,
        mode: str = "off",
        base_url: str = "",
        api_key: str = "",
        timeout_seconds: float = 1.0,
        request_fn: Optional[RequestFn] = None,
    ) -> None:
        normalized = (mode or "off").strip().lower()
        if normalized not in self.VALID_MODES:
            raise ValueError(f"Unsupported FREEPBX System-One mode: {mode!r}")
        self.mode = normalized
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._request_fn = request_fn

    @classmethod
    def from_env(cls) -> "VoiceDecisionPlane":
        return cls(
            mode=os.getenv("FREEPBX_SYSTEM_ONE_MODE", "off"),
            base_url=os.getenv("FREEPBX_SYSTEM_ONE_BASE_URL", ""),
            api_key=os.getenv("FREEPBX_SYSTEM_ONE_API_KEY", ""),
            timeout_seconds=float(os.getenv("FREEPBX_SYSTEM_ONE_TIMEOUT_SECONDS", "1.0")),
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.base_url)

    async def _http_post(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers, allow_redirects=False) as response:
                if response.status >= 400:
                    text = await response.text()
                    raise RuntimeError(f"System-One HTTP {response.status}: {text[:200]}")
                data = await response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("System-One response must be a JSON object")
                return data

    async def classify(
        self,
        transcript: str,
        *,
        call_id: str,
        pipeline: str,
        provider: str,
        caller_number: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        transcript = (transcript or "").strip()
        if not transcript or not self.enabled:
            return None

        questions = {
            "intent": {
                "type": "choice",
                "instructions": "What is the caller primarily trying to accomplish?",
                "criteria": {
                    "technical_support": "Report or solve a technical problem, outage, integration or service issue",
                    "billing": "Invoice, charge, payment, refund or account billing question",
                    "sales": "Pricing, product purchase, quote, demo or new business enquiry",
                    "customer_service": "General account, service or customer-support request",
                    "transfer_request": "Explicit request to reach a person, team, extension, queue or department",
                    "voicemail": "Request to leave or retrieve a voicemail/message",
                    "other": "None of the listed intents",
                },
            },
            "priority": {
                "type": "score",
                "instructions": "How urgent is the caller's request?",
                "criteria": [
                    "routine",
                    "needs attention soon",
                    "business-blocking",
                    "time-critical escalation",
                ],
            },
            "emergency": {
                "type": "noul",
                "instructions": "Does the caller describe an immediate emergency or danger requiring human escalation?",
            },
            "enterprise_account": {
                "type": "noul",
                "instructions": "Does the caller appear to be speaking about a business or enterprise account?",
            },
            "human_needed": {
                "type": "noul",
                "instructions": "Does this request appear to require a human agent rather than only an automated answer?",
            },
            "spam_or_abuse": {
                "type": "noul",
                "instructions": "Does the transcript look like spam, robocall abuse or malicious use?",
            },
        }
        payload = {
            "state": {
                "transcript": transcript[:8000],
                "call": {
                    "id": call_id,
                    "pipeline": pipeline,
                    "provider": provider,
                    "caller_number_present": bool(caller_number),
                },
                "policy": {
                    "advisory_only": True,
                    "telephony_tools_require_existing_tool_authority": True,
                    "no_automatic_transfer_or_hangup": True,
                },
            },
            "questions": questions,
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        started = time.perf_counter()
        request_fn = self._request_fn or self._http_post
        try:
            body = await asyncio.wait_for(
                request_fn(
                    f"{self.base_url}/v1/systemone",
                    payload,
                    headers,
                    self.timeout_seconds,
                ),
                timeout=self.timeout_seconds + 0.1,
            )
            answers = body.get("answers")
            if not isinstance(answers, dict):
                raise RuntimeError("System-One response has no answers object")
            result = {
                "provider": "laya",
                "mode": self.mode,
                "advisory_only": True,
                "answers": answers,
                "routing": body.get("routing") if isinstance(body.get("routing"), dict) else None,
                "usage": body.get("usage") if isinstance(body.get("usage"), dict) else None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
            logger.info(
                "voice_system_one_decision",
                call_id=call_id,
                mode=self.mode,
                latency_ms=result["latency_ms"],
                intent=(answers.get("intent") or {}).get("choice"),
            )
            return result
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning(
                "Voice System-One failed open; existing LLM pipeline continues",
                call_id=call_id,
                error=str(exc),
            )
            return None
