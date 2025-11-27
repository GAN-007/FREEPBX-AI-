"""
# Milestone7: OpenAI cloud component adapters for configurable pipelines.

This module provides concrete STT, LLM, and TTS adapters that integrate with
OpenAI's Realtime WebSocket API, Chat Completions REST API, and audio.speech
endpoint. The adapters mirror the contract defined in `base.py` so that
`PipelineOrchestrator` can wire them into call flows alongside other providers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional

import aiohttp
import websockets
from websockets.client import WebSocketClientProtocol

from ..audio import convert_pcm16le_to_target_format, mulaw_to_pcm16le, resample_audio
from ..config import AppConfig, OpenAIProviderConfig
from ..logging_config import get_logger
from .base import LLMComponent, STTComponent, TTSComponent, LLMResponse
from ..tools.registry import tool_registry

logger = get_logger(__name__)


# Shared helpers -----------------------------------------------------------------


def _merge_dicts(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(base or {})
    if override:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge_dicts(merged[key], value)
            elif value is not None:
                merged[key] = value
    return merged


def _bytes_per_sample(encoding: str) -> int:
    fmt = (encoding or "").lower()
    if fmt in ("ulaw", "mulaw", "mu-law", "g711_ulaw"):
        return 1
    return 2


def _chunk_audio(audio_bytes: bytes, encoding: str, sample_rate: int, chunk_ms: int) -> Iterable[bytes]:
    if not audio_bytes:
        return
    bytes_per_sample = _bytes_per_sample(encoding)
    frame_size = max(bytes_per_sample, int(sample_rate * (chunk_ms / 1000.0) * bytes_per_sample))
    for idx in range(0, len(audio_bytes), frame_size):
        yield audio_bytes[idx : idx + frame_size]


def _make_ws_headers(options: Dict[str, Any]) -> Iterable[tuple[str, str]]:
    headers = [
        ("Authorization", f"Bearer {options['api_key']}"),
        ("OpenAI-Beta", "realtime=v1"),
        ("User-Agent", "Asterisk-AI-Voice-Agent/1.0"),
    ]
    if options.get("organization"):
        headers.append(("OpenAI-Organization", options["organization"]))
    if options.get("project"):
        headers.append(("OpenAI-Project", options["project"]))
    return headers


def _make_http_headers(options: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {options['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "Asterisk-AI-Voice-Agent/1.0",
    }
    if options.get("organization"):
        headers["OpenAI-Organization"] = options["organization"]
    if options.get("project"):
        headers["OpenAI-Project"] = options["project"]
    return headers


def _decode_audio_payload(raw_bytes: bytes) -> bytes:
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_bytes

    audio_b64 = payload.get("data") or payload.get("audio")
    if not audio_b64:
        return raw_bytes
    try:
        return base64.b64decode(audio_b64)
    except (base64.binascii.Error, TypeError):
        logger.warning("Failed to base64 decode OpenAI audio payload")
        return raw_bytes


@dataclass
class _RealtimeSessionState:
    websocket: WebSocketClientProtocol
    options: Dict[str, Any]
    session_id: str


# Milestone7: OpenAI Realtime STT Adapter ----------------------------------------


class OpenAISTTAdapter(STTComponent):
    """# Milestone7: OpenAI Realtime STT adapter streaming PCM16 audio for transcripts."""

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: OpenAIProviderConfig,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config
        self._pipeline_defaults = options or {}
        self._sessions: Dict[str, _RealtimeSessionState] = {}
        self._default_timeout = float(self._pipeline_defaults.get("response_timeout_sec", provider_config.response_timeout_sec))

    async def start(self) -> None:
        logger.debug(
            "OpenAI STT adapter initialized",
            component=self.component_key,
            default_model=self._provider_defaults.realtime_model,
        )

    async def stop(self) -> None:
        for call_id in list(self._sessions.keys()):
            await self.close_call(call_id)

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            raise RuntimeError("OpenAI STT requires an API key")

        ws_headers = list(_make_ws_headers(merged))
        websocket = await websockets.connect(
            merged["base_url"],
            extra_headers=ws_headers,
            max_size=16 * 1024 * 1024,
        )

        session_id = str(uuid.uuid4())
        session = _RealtimeSessionState(websocket=websocket, options=merged, session_id=session_id)
        self._sessions[call_id] = session

        session_payload = {
            "type": "session.create",
            "session": {
                "model": merged["model"],
                "modalities": merged.get("modalities"),
                "instructions": merged.get("instructions"),
                "input_audio_format": {
                    "type": "pcm16",
                    "sample_rate_hz": merged["input_sample_rate_hz"],
                },
            },
        }
        await websocket.send(json.dumps(session_payload))
        logger.info(
            "OpenAI STT session created",
            call_id=call_id,
            session_id=session_id,
            model=merged["model"],
        )

    async def close_call(self, call_id: str) -> None:
        session = self._sessions.pop(call_id, None)
        if not session:
            return
        try:
            await session.websocket.close()
        finally:
            logger.info("OpenAI STT session closed", call_id=call_id, session_id=session.session_id)

    async def transcribe(
        self,
        call_id: str,
        audio_pcm16: bytes,
        sample_rate_hz: int,
        options: Dict[str, Any],
    ) -> str:
        session = self._sessions.get(call_id)
        if not session:
            raise RuntimeError(f"OpenAI STT session not found for call {call_id}")

        merged = _merge_dicts(session.options, options or {})
        target_rate = int(merged["input_sample_rate_hz"])
        if sample_rate_hz != target_rate:
            audio_pcm16, _ = resample_audio(audio_pcm16, sample_rate_hz, target_rate)

        audio_payload = base64.b64encode(audio_pcm16).decode("ascii")
        events = [
            {"type": "input_audio_buffer.append", "audio": audio_payload},
            {"type": "input_audio_buffer.commit"},
            {
                "type": "response.create",
                "response": {"modalities": ["text"], "instructions": merged.get("prompt_override")},
            },
        ]

        for event in events:
            await session.websocket.send(json.dumps(event))

        timeout = float(merged.get("response_timeout_sec", self._default_timeout))
        transcript = await self._await_transcript(session.websocket, timeout, call_id)
        if transcript is None:
            raise asyncio.TimeoutError("OpenAI STT did not return a transcript in time")
        return transcript

    async def _await_transcript(
        self,
        websocket: WebSocketClientProtocol,
        timeout: float,
        call_id: str,
    ) -> Optional[str]:
        deadline = time.perf_counter()
        buffer: list[str] = []

        while True:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("OpenAI STT transcript timeout", call_id=call_id)
                return None

            if isinstance(message, bytes):
                continue

            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                logger.debug("OpenAI STT received non-JSON message", message_preview=message[:64])
                continue

            event_type = payload.get("type")
            if event_type == "response.output_text.delta":
                delta = payload.get("delta") or payload.get("text") or ""
                buffer.append(delta)
            elif event_type in ("response.output_text.done", "response.completed"):
                transcript = "".join(buffer).strip()
                latency_ms = (time.perf_counter() - deadline) * 1000.0
                logger.info(
                    "OpenAI STT transcript received",
                    call_id=call_id,
                    latency_ms=round(latency_ms, 2),
                    transcript_preview=transcript[:80],
                )
                return transcript
            elif event_type == "response.error":
                logger.error("OpenAI STT response error", call_id=call_id, error=payload.get("error"))
                return None

    def _compose_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        runtime_options = runtime_options or {}
        merged = {
            "api_key": runtime_options.get("api_key", self._pipeline_defaults.get("api_key", self._provider_defaults.api_key)),
            "organization": runtime_options.get("organization", self._pipeline_defaults.get("organization", self._provider_defaults.organization)),
            "project": runtime_options.get("project", self._pipeline_defaults.get("project", self._provider_defaults.project)),
            "base_url": runtime_options.get(
                "base_url",
                self._pipeline_defaults.get("base_url", self._provider_defaults.realtime_base_url),
            ),
            "model": runtime_options.get(
                "model",
                self._pipeline_defaults.get("model", self._provider_defaults.realtime_model),
            ),
            "modalities": runtime_options.get(
                "modalities",
                self._pipeline_defaults.get("modalities", self._provider_defaults.default_modalities or ["text"]),
            ),
            "instructions": runtime_options.get(
                "instructions",
                self._pipeline_defaults.get("instructions", None),
            ),
            "input_sample_rate_hz": int(
                runtime_options.get(
                    "input_sample_rate_hz",
                    self._pipeline_defaults.get("input_sample_rate_hz", self._provider_defaults.input_sample_rate_hz),
                )
            ),
            "response_timeout_sec": runtime_options.get(
                "response_timeout_sec",
                self._pipeline_defaults.get("response_timeout_sec", self._provider_defaults.response_timeout_sec),
            ),
            "prompt_override": runtime_options.get("prompt_override"),
        }
        # Fallback persona when instructions not provided
        try:
            instr = (merged.get("instructions") or "").strip()
        except Exception:
            instr = ""
        if not instr:
            try:
                merged["instructions"] = getattr(self._app_config.llm, "prompt", None)
            except Exception:
                merged["instructions"] = None
        return merged


# Milestone7: OpenAI Chat/Reatime LLM Adapter ------------------------------------


class OpenAILLMAdapter(LLMComponent):
    """# Milestone7: OpenAI LLM adapter supporting Chat Completions and Realtime."""

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: OpenAIProviderConfig,
        options: Optional[Dict[str, Any]] = None,
        *,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config
        self._pipeline_defaults = options or {}
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None
        self._default_timeout = float(self._pipeline_defaults.get("response_timeout_sec", provider_config.response_timeout_sec))

    async def start(self) -> None:
        logger.debug(
            "OpenAI LLM adapter initialized",
            component=self.component_key,
            default_model=self._provider_defaults.chat_model,
        )

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # validate_connectivity removed - uses smart generic base class implementation

    async def generate(
        self,
        call_id: str,
        transcript: str,
        context: Dict[str, Any],
        options: Dict[str, Any],
    ) -> str | LLMResponse:
        merged = self._compose_options(options)
        if not merged["api_key"]:
            raise RuntimeError("OpenAI LLM requires an API key")

        use_realtime = bool(merged.get("use_realtime"))
        if use_realtime:
            return await self._generate_realtime(call_id, transcript, context, merged)

        await self._ensure_session()
        assert self._session
        payload = self._build_chat_payload(transcript, context, merged)
        
        # Milestone7: Tool support
        tools_list = merged.get("tools")
        if tools_list and isinstance(tools_list, list):
            tool_schemas = []
            for tool_name in tools_list:
                tool = tool_registry.get(tool_name)
                if tool:
                    tool_schemas.append(tool.definition.to_openai_schema())
                else:
                    logger.warning("Tool not found in registry", tool=tool_name)
            
            if tool_schemas:
                payload["tools"] = tool_schemas
                payload["tool_choice"] = "auto"

        headers = _make_http_headers(merged)
        url = merged["chat_base_url"].rstrip("/") + "/chat/completions"

        logger.debug(
            "OpenAI chat completion request",
            call_id=call_id,
            model=payload.get("model"),
            temperature=payload.get("temperature"),
            tools_count=len(payload.get("tools", [])),
        )

        retries = 1
        for attempt in range(retries + 1):
            try:
                async with self._session.post(url, json=payload, headers=headers, timeout=merged["timeout_sec"]) as response:
                    body = await response.text()
                    if response.status >= 400:
                        logger.error(
                            "OpenAI chat completion failed",
                            call_id=call_id,
                            status=response.status,
                            body_preview=body[:128],
                        )
                        response.raise_for_status()

                    data = json.loads(body)
                    choices = data.get("choices") or []
                    if not choices:
                        logger.warning("OpenAI chat completion returned no choices", call_id=call_id)
                        return ""

                    message = choices[0].get("message") or {}
                    content = message.get("content", "")
                    tool_calls = message.get("tool_calls") or []
                    
                    # Log response
                    log_ctx = {
                        "call_id": call_id,
                        "model": payload.get("model"),
                        "preview": (content or "")[:80],
                    }
                    if tool_calls:
                        log_ctx["tool_calls"] = len(tool_calls)
                        # Parse tool calls into our standard dict format
                        parsed_tool_calls = []
                        for tc in tool_calls:
                            try:
                                func = tc.get("function", {})
                                name = func.get("name")
                                args = func.get("arguments", "{}")
                                parsed_tool_calls.append({
                                    "id": tc.get("id"),
                                    "name": name,
                                    "parameters": json.loads(args),
                                    "type": tc.get("type", "function")
                                })
                            except Exception as e:
                                logger.warning("Failed to parse tool call", error=str(e))
                        
                        logger.info("OpenAI chat completion received with tools", **log_ctx)
                        return LLMResponse(
                            text=content or "",
                            tool_calls=parsed_tool_calls,
                            metadata=data.get("usage", {})
                        )
                    
                    logger.info("OpenAI chat completion received", **log_ctx)
                    return content
            except aiohttp.ClientError as e:
                if self._session is None or self._session.closed:
                    logger.info("OpenAI LLM generation cancelled (session closed)", call_id=call_id)
                    return ""

                if attempt == retries:
                    logger.error("OpenAI LLM connection error", call_id=call_id, error=str(e))
                    raise

                logger.warning("OpenAI LLM connection error, retrying", call_id=call_id, error=str(e))
    
    async def _generate_realtime(
        self,
        call_id: str,
        transcript: str,
        context: Dict[str, Any],
        merged: Dict[str, Any],
    ) -> str:
        headers = list(_make_ws_headers(merged))
        websocket = await websockets.connect(
            merged["realtime_base_url"],
            extra_headers=headers,
            max_size=8 * 1024 * 1024,
        )

        session_payload = {
            "type": "session.create",
            "session": {
                "model": merged["realtime_model"],
                "modalities": merged.get("modalities"),
                "instructions": merged.get("system_prompt") or context.get("system_prompt"),
            },
        }
        await websocket.send(json.dumps(session_payload))

        messages = self._coalesce_messages(transcript, context, merged)
        request_payload = {
            "type": "response.create",
            "response": {
                "modalities": ["text"],
                "instructions": merged.get("instructions"),
                "metadata": {"component": self.component_key, "call_id": call_id},
                "conversation": {"messages": messages},
            },
        }
        await websocket.send(json.dumps(request_payload))

        buffer: list[str] = []
        try:
            while True:
                message = await asyncio.wait_for(websocket.recv(), timeout=merged["timeout_sec"])
                if isinstance(message, bytes):
                    continue
                payload = json.loads(message)
                event_type = payload.get("type")
                if event_type == "response.output_text.delta":
                    buffer.append(payload.get("delta") or "")
                elif event_type in ("response.output_text.done", "response.completed"):
                    response_text = "".join(buffer).strip()
                    logger.info("OpenAI realtime LLM response", call_id=call_id, preview=response_text[:80])
                    return response_text
                elif event_type == "response.error":
                    logger.error("OpenAI realtime LLM error", call_id=call_id, error=payload.get("error"))
                    break
        finally:
            await websocket.close()
        return ""

    async def _ensure_session(self) -> None:
        if self._session and not self._session.closed:
            return
        factory = self._session_factory or aiohttp.ClientSession
        self._session = factory()

    def _compose_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        runtime_options = runtime_options or {}
        merged = {
            "api_key": runtime_options.get("api_key", self._pipeline_defaults.get("api_key", self._provider_defaults.api_key)),
            "organization": runtime_options.get("organization", self._pipeline_defaults.get("organization", self._provider_defaults.organization)),
            "project": runtime_options.get("project", self._pipeline_defaults.get("project", self._provider_defaults.project)),
            "chat_base_url": runtime_options.get(
                "chat_base_url",
                self._pipeline_defaults.get("chat_base_url", self._provider_defaults.chat_base_url),
            ),
            "realtime_base_url": runtime_options.get(
                "realtime_base_url",
                self._pipeline_defaults.get("realtime_base_url", self._provider_defaults.realtime_base_url),
            ),
            "chat_model": runtime_options.get(
                "chat_model",
                self._pipeline_defaults.get("chat_model", self._provider_defaults.chat_model),
            ),
            "realtime_model": runtime_options.get(
                "realtime_model",
                self._pipeline_defaults.get("realtime_model", self._provider_defaults.realtime_model),
            ),
            "modalities": runtime_options.get(
                "modalities",
                self._pipeline_defaults.get("modalities", self._provider_defaults.default_modalities or ["text"]),
            ),
            "system_prompt": runtime_options.get("system_prompt", self._pipeline_defaults.get("system_prompt")),
            "instructions": runtime_options.get("instructions", self._pipeline_defaults.get("instructions")),
            "temperature": runtime_options.get("temperature", self._pipeline_defaults.get("temperature", 0.7)),
            "max_tokens": runtime_options.get("max_tokens", self._pipeline_defaults.get("max_tokens")),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
            "use_realtime": runtime_options.get("use_realtime", self._pipeline_defaults.get("use_realtime", False)),
            "tools": runtime_options.get("tools", self._pipeline_defaults.get("tools", [])),
        }
        # Fallback persona when missing
        try:
            sys_p = (merged.get("system_prompt") or "").strip()
        except Exception:
            sys_p = ""
        if not sys_p:
            try:
                merged["system_prompt"] = getattr(self._app_config.llm, "prompt", None)
            except Exception:
                merged["system_prompt"] = None
        try:
            instr = (merged.get("instructions") or "").strip()
        except Exception:
            instr = ""
        if not instr:
            try:
                merged["instructions"] = getattr(self._app_config.llm, "prompt", None)
            except Exception:
                merged["instructions"] = None
        return merged

    def _build_chat_payload(self, transcript: str, context: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
        messages = self._coalesce_messages(transcript, context, merged)
        payload: Dict[str, Any] = {
            "model": merged["chat_model"],
            "messages": messages,
        }
        if merged.get("temperature") is not None:
            payload["temperature"] = merged["temperature"]
        if merged.get("max_tokens") is not None:
            payload["max_tokens"] = merged["max_tokens"]
        return payload

    def _coalesce_messages(self, transcript: str, context: Dict[str, Any], merged: Dict[str, Any]) -> list[Dict[str, str]]:
        messages = context.get("messages")
        if messages:
            return messages

        conversation = []
        system_prompt = merged.get("system_prompt") or context.get("system_prompt")
        if system_prompt:
            conversation.append({"role": "system", "content": system_prompt})

        prior = context.get("prior_messages") or []
        conversation.extend(prior)

        if transcript:
            conversation.append({"role": "user", "content": transcript})
        return conversation


# Milestone7: OpenAI audio.speech TTS Adapter ------------------------------------


class OpenAITTSAdapter(TTSComponent):
    """# Milestone7: OpenAI TTS adapter calling the audio.speech REST API."""

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: OpenAIProviderConfig,
        options: Optional[Dict[str, Any]] = None,
        *,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config
        self._pipeline_defaults = options or {}
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None
        self._chunk_size_ms = int(self._pipeline_defaults.get("chunk_size_ms", provider_config.chunk_size_ms))

    async def start(self) -> None:
        logger.debug(
            "OpenAI TTS adapter initialized",
            component=self.component_key,
            default_model=self._provider_defaults.tts_model,
        )

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        await self._ensure_session()

    async def close_call(self, call_id: str) -> None:
        return

    async def synthesize(
        self,
        call_id: str,
        text: str,
        options: Dict[str, Any],
    ) -> AsyncIterator[bytes]:
        if not text:
            return  # Exit early - yields nothing (async generator)
            yield  # Unreachable but makes this an async generator
        await self._ensure_session()
        assert self._session

        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            raise RuntimeError("OpenAI TTS requires an API key")

        headers = _make_http_headers(merged)
        url = merged["tts_base_url"]

        payload = {
            "model": merged["tts_model"],
            "input": text,
            "voice": merged["voice"],
            "format": merged["source_format"]["encoding"],
            "sample_rate": merged["source_format"]["sample_rate"],
        }

        logger.info(
            "OpenAI TTS synthesis started",
            call_id=call_id,
            model=payload["model"],
            voice=payload["voice"],
            text_preview=text[:64],
        )

        async with self._session.post(url, json=payload, headers=headers, timeout=merged["timeout_sec"]) as response:
            data = await response.read()
            if response.status >= 400:
                body = data.decode("utf-8", errors="ignore")
                logger.error(
                    "OpenAI TTS synthesis failed",
                    call_id=call_id,
                    status=response.status,
                    body_preview=body[:128],
                )
                response.raise_for_status()

            audio_bytes = _decode_audio_payload(data)
            converted = self._convert_audio(
                audio_bytes,
                merged["source_format"]["encoding"],
                merged["source_format"]["sample_rate"],
                merged["target_format"]["encoding"],
                merged["target_format"]["sample_rate"],
            )

        logger.info(
            "OpenAI TTS synthesis completed",
            call_id=call_id,
            output_bytes=len(converted),
            target_encoding=merged["target_format"]["encoding"],
            target_sample_rate=merged["target_format"]["sample_rate"],
        )

        chunk_ms = int(merged.get("chunk_size_ms", self._chunk_size_ms))
        for chunk in _chunk_audio(
            converted,
            merged["target_format"]["encoding"],
            merged["target_format"]["sample_rate"],
            chunk_ms,
        ):
            if chunk:
                yield chunk

    async def _ensure_session(self) -> None:
        if self._session and not self._session.closed:
            return
        factory = self._session_factory or aiohttp.ClientSession
        self._session = factory()

    def _compose_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        runtime_options = runtime_options or {}
        source_defaults = self._pipeline_defaults.get("source_format", {})
        merged_source = {
            "encoding": runtime_options.get("source_format", {}).get(
                "encoding",
                source_defaults.get("encoding", self._provider_defaults.input_encoding),
            ),
            "sample_rate": int(
                runtime_options.get("source_format", {}).get(
                    "sample_rate",
                    source_defaults.get("sample_rate", self._provider_defaults.input_sample_rate_hz),
                )
            ),
        }

        format_defaults = self._pipeline_defaults.get("format", {})
        merged_target = {
            "encoding": runtime_options.get("format", {}).get(
                "encoding",
                format_defaults.get("encoding", self._provider_defaults.target_encoding),
            ),
            "sample_rate": int(
                runtime_options.get("format", {}).get(
                    "sample_rate",
                    format_defaults.get("sample_rate", self._provider_defaults.target_sample_rate_hz),
                )
            ),
        }

        merged = {
            "api_key": runtime_options.get("api_key", self._pipeline_defaults.get("api_key", self._provider_defaults.api_key)),
            "organization": runtime_options.get("organization", self._pipeline_defaults.get("organization", self._provider_defaults.organization)),
            "project": runtime_options.get("project", self._pipeline_defaults.get("project", self._provider_defaults.project)),
            "tts_base_url": runtime_options.get(
                "base_url",
                self._pipeline_defaults.get("base_url", self._provider_defaults.tts_base_url),
            ),
            "tts_model": runtime_options.get(
                "model",
                self._pipeline_defaults.get("model", self._provider_defaults.tts_model),
            ),
            "voice": runtime_options.get("voice", self._pipeline_defaults.get("voice", self._provider_defaults.voice)),
            "chunk_size_ms": runtime_options.get("chunk_size_ms", self._pipeline_defaults.get("chunk_size_ms", self._provider_defaults.chunk_size_ms)),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._provider_defaults.response_timeout_sec))),
            "source_format": merged_source,
            "target_format": merged_target,
        }
        return merged

    @staticmethod
    def _convert_audio(
        audio_bytes: bytes,
        source_encoding: str,
        source_rate: int,
        target_encoding: str,
        target_rate: int,
    ) -> bytes:
        if not audio_bytes:
            return b""

        fmt = (source_encoding or "").lower()
        if fmt in ("ulaw", "mulaw", "mu-law", "g711_ulaw"):
            pcm_bytes = mulaw_to_pcm16le(audio_bytes)
        else:
            pcm_bytes = audio_bytes

        if source_rate != target_rate:
            pcm_bytes, _ = resample_audio(pcm_bytes, source_rate, target_rate)

        return convert_pcm16le_to_target_format(pcm_bytes, target_encoding)


__all__ = [
    "OpenAISTTAdapter",
    "OpenAILLMAdapter",
    "OpenAITTSAdapter",
]