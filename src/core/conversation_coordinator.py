"""Conversation coordination and observability utilities.

This module centralizes the logic that toggles audio capture, tracks
conversation state transitions, and exposes Prometheus metrics so the
engine can make gating decisions with a single source of truth.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional, TYPE_CHECKING

import structlog
from prometheus_client import Counter, Gauge

from .models import CallSession
from .session_store import SessionStore

if TYPE_CHECKING:  # pragma: no cover
    from .playback_manager import PlaybackManager

logger = structlog.get_logger(__name__)

# Prometheus metrics are defined at module scope so they are registered
# exactly once even if the coordinator is instantiated multiple times.
_TTS_GATING_GAUGE = Gauge(
    "ai_agent_tts_gating_active",
    "Whether TTS gating is currently active for a call (1 = gated)",
    labelnames=("call_id",),
)
_AUDIO_CAPTURE_GAUGE = Gauge(
    "ai_agent_audio_capture_enabled",
    "Whether upstream audio capture is enabled for a call (1 = enabled)",
    labelnames=("call_id",),
)
_CONVERSATION_STATE_GAUGE = Gauge(
    "ai_agent_conversation_state",
    "Conversation state indicator gauge (1 = state active)",
    labelnames=("call_id", "state"),
)
_BARGE_IN_COUNTER = Counter(
    "ai_agent_barge_in_events_total",
    "Count of barge-in attempts detected while TTS playback is active",
    labelnames=("call_id",),
)

# Accepted conversation states for the simple state gauge.
_CONVERSATION_STATES = ("greeting", "listening", "processing")


class ConversationCoordinator:
    """Central coordinator for conversation state and observability."""

    def __init__(self, session_store: SessionStore, playback_manager: Optional["PlaybackManager"] = None):
        self._session_store = session_store
        self._playback_manager = playback_manager
        self._capture_fallback_tasks: Dict[str, asyncio.Task] = {}
        self._barge_in_seen: Dict[str, bool] = {}
        self._barge_in_totals: Dict[str, int] = {}

    def set_playback_manager(self, playback_manager: "PlaybackManager") -> None:
        """Attach the playback manager after initialisation."""
        self._playback_manager = playback_manager

    async def register_call(self, session: CallSession) -> None:
        """Initialise metrics for a newly tracked call session."""
        logger.debug("ConversationCoordinator registering call", call_id=session.call_id)
        _AUDIO_CAPTURE_GAUGE.labels(session.call_id).set(1 if session.audio_capture_enabled else 0)
        _TTS_GATING_GAUGE.labels(session.call_id).set(1 if session.tts_playing else 0)
        self._set_state_metric(session.call_id, session.conversation_state)
        self._barge_in_seen[session.call_id] = False
        self._barge_in_totals.setdefault(session.call_id, 0)
        # Ensure we do not leak old fallback tasks
        await self._cancel_capture_fallback(session.call_id)

    async def unregister_call(self, call_id: str) -> None:
        """Remove metrics and timers associated with a call session."""
        logger.debug("ConversationCoordinator unregistering call", call_id=call_id)
        await self._cancel_capture_fallback(call_id)
        self._barge_in_seen.pop(call_id, None)
        self._barge_in_totals.pop(call_id, None)
        try:
            _AUDIO_CAPTURE_GAUGE.remove(call_id)
        except KeyError:
            pass
        try:
            _TTS_GATING_GAUGE.remove(call_id)
        except KeyError:
            pass
        for state in _CONVERSATION_STATES:
            try:
                _CONVERSATION_STATE_GAUGE.remove(call_id, state)
            except KeyError:
                pass

    async def sync_from_session(self, session: CallSession) -> None:
        """Synchronise gauges to reflect the latest session values."""
        _AUDIO_CAPTURE_GAUGE.labels(session.call_id).set(1 if session.audio_capture_enabled else 0)
        _TTS_GATING_GAUGE.labels(session.call_id).set(1 if session.tts_playing else 0)
        self._set_state_metric(session.call_id, session.conversation_state)

    async def on_tts_start(self, call_id: str, playback_id: str) -> bool:
        """Disable audio capture for the duration of a TTS playback."""
        logger.info("🔇 ConversationCoordinator gating audio", call_id=call_id, playback_id=playback_id)
        success = await self._session_store.set_gating_token(call_id, playback_id)
        if success:
            _TTS_GATING_GAUGE.labels(call_id).set(1)
            _AUDIO_CAPTURE_GAUGE.labels(call_id).set(0)
            self._barge_in_seen[call_id] = False
        return success

    async def on_tts_end(self, call_id: str, playback_id: str, reason: str = "playback-finished") -> bool:
        """Re-enable audio capture after a TTS playback finishes."""
        logger.info(
            "🔊 ConversationCoordinator clearing gating",
            call_id=call_id,
            playback_id=playback_id,
            reason=reason,
        )
        success = await self._session_store.clear_gating_token(call_id, playback_id)
        if success:
            _TTS_GATING_GAUGE.labels(call_id).set(0)
            session = await self._session_store.get_by_call_id(call_id)
            capture_enabled = True
            if session:
                capture_enabled = session.audio_capture_enabled
            _AUDIO_CAPTURE_GAUGE.labels(call_id).set(1 if capture_enabled else 0)
            self._barge_in_seen[call_id] = False
        return success

    async def cancel_tts(self, call_id: str, playback_id: str) -> None:
        """Clear gating when playback fails to start."""
        await self.on_tts_end(call_id, playback_id, reason="playback-cancelled")

    def note_audio_during_tts(self, call_id: str) -> None:
        """Record a barge-in attempt if audio arrives while TTS plays."""
        if call_id not in self._barge_in_seen:
            self._barge_in_seen[call_id] = False
            self._barge_in_totals.setdefault(call_id, 0)
        if not self._barge_in_seen[call_id]:
            logger.debug("🎧 Barge-in attempt detected", call_id=call_id)
            _BARGE_IN_COUNTER.labels(call_id).inc()
            self._barge_in_totals[call_id] = self._barge_in_totals.get(call_id, 0) + 1
            self._barge_in_seen[call_id] = True

    async def update_conversation_state(self, call_id: str, state: str) -> None:
        """Update session conversation state and reflect it in gauges."""
        if state not in _CONVERSATION_STATES:
            logger.debug("Unknown conversation state requested", call_id=call_id, state=state)
            return
        session = await self._session_store.get_by_call_id(call_id)
        if not session:
            return
        if session.conversation_state == state:
            return
        session.conversation_state = state
        await self._session_store.upsert_call(session)
        self._set_state_metric(call_id, state)

    async def schedule_capture_fallback(self, call_id: str, delay: float) -> None:
        """Ensure audio capture is eventually re-enabled after a delay."""
        await self._cancel_capture_fallback(call_id)

        async def _task():
            try:
                await asyncio.sleep(delay)
                session = await self._session_store.get_by_call_id(call_id)
                if not session:
                    return
                if session.audio_capture_enabled:
                    return
                session.audio_capture_enabled = True
                await self._session_store.upsert_call(session)
                _AUDIO_CAPTURE_GAUGE.labels(call_id).set(1)
                logger.info("🎤 ConversationCoordinator fallback re-enabled capture", call_id=call_id)
            except asyncio.CancelledError:
                logger.debug("ConversationCoordinator capture fallback cancelled", call_id=call_id)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("ConversationCoordinator capture fallback failed", call_id=call_id)

        task = asyncio.create_task(_task())
        self._capture_fallback_tasks[call_id] = task

    async def get_summary(self) -> Dict[str, Optional[int]]:
        """Summarise conversation metrics for health reporting."""
        sessions = await self._session_store.get_all_sessions()
        gating_active = sum(1 for session in sessions if session.tts_playing)
        capture_disabled = sum(1 for session in sessions if not session.audio_capture_enabled)
        barge_in_total = sum(self._barge_in_totals.values())
        return {
            "gating_active": gating_active,
            "capture_disabled": capture_disabled,
            "barge_in_total": barge_in_total,
        }

    async def _cancel_capture_fallback(self, call_id: str) -> None:
        task = self._capture_fallback_tasks.pop(call_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _set_state_metric(self, call_id: str, state: str) -> None:
        for known_state in _CONVERSATION_STATES:
            gauge = _CONVERSATION_STATE_GAUGE.labels(call_id, known_state)
            gauge.set(1 if known_state == state else 0)

__all__ = ["ConversationCoordinator"]
