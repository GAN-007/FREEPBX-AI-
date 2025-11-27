import asyncio
import pytest

from src.config import AppConfig
from src.engine import Engine
from src.pipelines.base import STTComponent, LLMComponent, TTSComponent


class _StubSTT(STTComponent):
    async def transcribe(self, call_id, audio_pcm16, sample_rate_hz, options):
        return "hi"


class _StubLLM(LLMComponent):
    async def generate(self, call_id, transcript, context, options):
        return "hello"


class _StubTTS(TTSComponent):
    async def synthesize(self, call_id, text, options):
        yield b"ulaw-bytes"


class _StubResolution:
    def __init__(self):
        self.pipeline_name = "stub"
        self.stt_adapter = _StubSTT()
        self.llm_adapter = _StubLLM()
        self.tts_adapter = _StubTTS()
        self.stt_options = {}
        self.llm_options = {}
        self.tts_options = {}
        self.prepared = True

    def component_summary(self):
        return {"stt": "stub", "llm": "stub", "tts": "stub"}


@pytest.mark.asyncio
async def test_pipeline_runner_lifecycle(monkeypatch):
    # Minimal AppConfig, orchestrator presence is enough; we will stub its output
    config_data = {
        "default_provider": "local",
        "providers": {"local": {"enabled": True}},
        "asterisk": {"host": "127.0.0.1", "port": 8088, "username": "u", "password": "p", "app_name": "ai-voice-agent"},
        "llm": {"initial_greeting": "hi", "prompt": "You are helpful", "model": "gpt-4o"},
        "pipelines": {"local_only": {}},
        "active_pipeline": "local_only",
        "audio_transport": "externalmedia",
    }
    app_config = AppConfig(**config_data)

    engine = Engine(app_config)
    engine.pipeline_orchestrator._started = True

    # Stub orchestrator to return a fake resolution with in-memory adapters
    def fake_get_pipeline(call_id, pipeline_name=None):
        return _StubResolution()

    monkeypatch.setattr(engine.pipeline_orchestrator, "get_pipeline", fake_get_pipeline)

    # Register a fake session
    from src.core.models import CallSession
    call_id = "call-abc"
    session = CallSession(call_id=call_id, caller_channel_id=call_id)
    session.pipeline_name = "local_only"
    await engine.session_store.upsert_call(session)

    # Start pipeline runner explicitly
    await engine._ensure_pipeline_runner(session, forced=True)

    assert call_id in engine._pipeline_tasks
    assert call_id in engine._pipeline_queues

    # Feed some audio and then cleanup
    q = engine._pipeline_queues[call_id]
    await q.put(b"\x00\x00" * 512)  # short chunk; runner will batch and continue

    await engine._cleanup_call(call_id)

    # Runner should be cancelled and queues/flags cleared
    assert call_id not in engine._pipeline_tasks
    assert call_id not in engine._pipeline_queues
    assert call_id not in engine._pipeline_forced
