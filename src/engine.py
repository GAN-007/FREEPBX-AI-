import asyncio
import logging
import math
import os
import random
import signal
import struct
import time
import uuid
import audioop
import base64
from collections import deque
from datetime import datetime
from typing import Dict, Any, Optional, List, Set, Tuple

# Simple audio capture system removed - not used in production

# WebRTC VAD for robust speech detection
try:
    import webrtcvad  # pyright: ignore[reportMissingImports]
    WEBRTC_VAD_AVAILABLE = True
except ImportError:
    WEBRTC_VAD_AVAILABLE = False
    webrtcvad = None

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, Histogram, Counter, Gauge

from .ari_client import ARIClient
from aiohttp import web
from pydantic import ValidationError

from .config import (
    AppConfig,
    load_config,
    LocalProviderConfig,
    DeepgramProviderConfig,
    GoogleProviderConfig,
    OpenAIRealtimeProviderConfig,
)
from .pipelines import PipelineOrchestrator, PipelineOrchestratorError, PipelineResolution
from .logging_config import get_logger, configure_logging
from .rtp_server import RTPServer
from .audio.audiosocket_server import AudioSocketServer
from .providers.base import AIProviderInterface
from .providers.deepgram import DeepgramProvider
from .providers.local import LocalProvider
from .providers.openai_realtime import OpenAIRealtimeProvider
from .providers.google_live import GoogleLiveProvider
from .core import SessionStore, PlaybackManager, ConversationCoordinator
from .core.vad_manager import EnhancedVADManager, VADResult
from .core.streaming_playback_manager import StreamingPlaybackManager
from .core.transport_orchestrator import TransportOrchestrator, TransportProfile
from .core.models import CallSession
from .utils.audio_capture import AudioCaptureManager
from src.pipelines.base import LLMResponse

logger = get_logger(__name__)

# -----------------------------------------------------------------------------
# Prometheus latency histograms (module scope, registered once)
# -----------------------------------------------------------------------------
_TURN_STT_TO_TTS = Histogram(
    "ai_agent_stt_to_tts_seconds",
    "Time from STT final transcript to first TTS bytes",
    buckets=(0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0),
    labelnames=("pipeline", "provider"),
)
_TURN_RESPONSE_SECONDS = Histogram(
    "ai_agent_turn_response_seconds",
    "Approx time from STT final transcript to ARI playback start",
    buckets=(0.2, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0),
    labelnames=("pipeline", "provider"),
)

# Config exposure gauges (per call at session start)
_CFG_BARGE_MS = Gauge(
    "ai_agent_config_barge_in_ms",
    "Configured barge-in timing values (ms)",
    labelnames=("call_id", "param"),
)
_CFG_BARGE_THRESHOLD = Gauge(
    "ai_agent_config_barge_in_threshold",
    "Configured barge-in energy threshold",
    labelnames=("call_id",),
)
_CFG_STREAM_MS = Gauge(
    "ai_agent_config_streaming_ms",
    "Configured streaming timing values (ms)",
    labelnames=("call_id", "param"),
)
_CFG_TD_MS = Gauge(
    "ai_agent_config_turn_detection_ms",
    "Configured provider turn detection timing values (ms)",
    labelnames=("call_id", "param"),
)
_CFG_TD_THRESHOLD = Gauge(
    "ai_agent_config_turn_detection_threshold",
    "Configured provider turn detection threshold",
    labelnames=("call_id",),
)

# Barge-in reaction latency (seconds) from first energy to trigger
_BARGE_REACTION_SECONDS = Histogram(
    "ai_agent_barge_in_reaction_seconds",
    "Time from first speech energy to barge-in trigger",
    buckets=(0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0),
    labelnames=("call_id",),
)

# Per-call audio byte counters (ingress)
_STREAM_RX_BYTES = Counter(
    "ai_agent_stream_rx_bytes_total",
    "Inbound audio bytes from caller (per call)",
    labelnames=("call_id",),
)
_CODEC_ALIGNMENT = Gauge(
    "ai_agent_codec_alignment",
    "Codec/sample-rate alignment status per call/provider (1=aligned,0=degraded)",
    labelnames=("call_id", "provider"),
)
_AUDIO_RMS_GAUGE = Gauge(
    "ai_agent_audio_rms",
    "Observed RMS levels for audio stages",
    labelnames=("call_id", "stage"),
)
_AUDIO_DC_OFFSET = Gauge(
    "ai_agent_audio_dc_offset",
    "Observed DC offset (mean sample value) for audio stages",
    labelnames=("call_id", "stage"),
)

# Call metadata and duration tracking for dashboard
_CALL_METADATA = Gauge(
    "ai_agent_call_metadata",
    "Call metadata including caller info, pipeline, provider, and context selection",
    labelnames=("call_id", "caller_name", "caller_number", "pipeline", "provider", "context"),
)
_CALL_DURATION = Histogram(
    "ai_agent_call_duration_seconds",
    "Total call duration from start to end",
    labelnames=("call_id", "pipeline", "provider"),
    buckets=(10, 30, 60, 120, 180, 300, 600, 900, 1800, 3600),
)
# Track call start times for duration calculation
_call_start_times = {}  # call_id -> timestamp


class Engine:
    """The main application engine."""

    def __init__(self, config: AppConfig):
        self.config = config
        base_url = f"http://{config.asterisk.host}:{config.asterisk.port}/ari"
        self.ari_client = ARIClient(
            username=config.asterisk.username,
            password=config.asterisk.password,
            base_url=base_url,
            app_name=config.asterisk.app_name
        )
        # Set engine reference for event propagation
        self.ari_client.engine = self
        
        # Initialize core components
        self.session_store = SessionStore()
        self.conversation_coordinator = ConversationCoordinator(self.session_store)
        self.playback_manager = PlaybackManager(
            self.session_store,
            self.ari_client,
            conversation_coordinator=self.conversation_coordinator,
        )
        self.conversation_coordinator.set_playback_manager(self.playback_manager)
        # Per-call transcript timing cache for latency histograms
        self._last_transcript_ts: Dict[str, float] = {}
        
        # Initialize streaming playback manager
        streaming_config = {}
        if hasattr(config, 'streaming') and config.streaming:
            audiosocket_fmt = "ulaw"
            try:
                if getattr(config, "audiosocket", None) and getattr(config.audiosocket, "format", None):
                    audiosocket_fmt = str(config.audiosocket.format).lower()
            except Exception:
                audiosocket_fmt = "ulaw"
            streaming_sample_rate = int(getattr(config.streaming, 'sample_rate', 8000) or 8000)
            # For PCM transport over AudioSocket, prefer 16 kHz by default unless explicitly set
            if self._canonicalize_encoding(audiosocket_fmt) in {"slin16", "linear16", "pcm16"}:
                try:
                    if not getattr(config.streaming, 'sample_rate', None):
                        streaming_sample_rate = 16000
                except Exception:
                    streaming_sample_rate = 16000
            streaming_config = {
                'sample_rate': streaming_sample_rate,
                'jitter_buffer_ms': config.streaming.jitter_buffer_ms,
                'keepalive_interval_ms': config.streaming.keepalive_interval_ms,
                'connection_timeout_ms': config.streaming.connection_timeout_ms,
                'fallback_timeout_ms': config.streaming.fallback_timeout_ms,
                'chunk_size_ms': config.streaming.chunk_size_ms,
                # Additional tuning knobs
                'min_start_ms': config.streaming.min_start_ms,
                'low_watermark_ms': config.streaming.low_watermark_ms,
                'provider_grace_ms': config.streaming.provider_grace_ms,
                'logging_level': config.streaming.logging_level,
                'egress_swap_mode': getattr(config.streaming, 'egress_swap_mode', 'auto'),
                'egress_force_mulaw': self._should_force_mulaw(
                    getattr(config.streaming, 'egress_force_mulaw', False),
                    audiosocket_fmt,
                ),
                # Continuous stream across provider segments (single pacer per call)
                'continuous_stream': bool(getattr(config.streaming, 'continuous_stream', True)),
                # Audio normalizer (RMS make-up gain prior to μ-law encode)
                'normalizer': {
                    'enabled': bool(getattr(getattr(config, 'streaming', {}), 'normalizer', {}).get('enabled', True)) if hasattr(config, 'streaming') else True,
                    'target_rms': int(getattr(getattr(config, 'streaming', {}), 'normalizer', {}).get('target_rms', 1400)) if hasattr(config, 'streaming') else 1400,
                    'max_gain_db': float(getattr(getattr(config, 'streaming', {}), 'normalizer', {}).get('max_gain_db', 9.0)) if hasattr(config, 'streaming') else 9.0,
                },
                # Diagnostics (optional): enable short PCM taps pre/post compand
                'diag_enable_taps': bool(getattr(config.streaming, 'diag_enable_taps', False)),
                'diag_pre_secs': int(getattr(config.streaming, 'diag_pre_secs', 0) or 0),
                'diag_post_secs': int(getattr(config.streaming, 'diag_post_secs', 0) or 0),
                'diag_out_dir': str(getattr(config.streaming, 'diag_out_dir', '') or ''),
            }
        # Debug/diagnostics: allow broadcasting outbound frames to all AudioSocket conns
        try:
            streaming_config['audiosocket_broadcast_debug'] = bool(int(os.getenv('AUDIOSOCKET_BROADCAST_DEBUG', '0')))
        except Exception:
            streaming_config['audiosocket_broadcast_debug'] = False

        # Initialize per-call audio capture used for diagnostics/RCA.
        # Captures are written under /tmp/ai-engine-captures/<call_id>/stream_name.wav,
        # which is what scripts/rca_collect.sh expects when building the "captures" bundle.
        capture_dir = "/tmp/ai-engine-captures"
        # Use DIAG_ENABLE_TAPS as a generic switch for keeping capture files after calls complete.
        keep_captures = os.getenv("DIAG_ENABLE_TAPS", "false").lower() in ("true", "1", "yes")
        self.audio_capture = AudioCaptureManager(base_dir=capture_dir, keep_files=keep_captures)
        logger.info(
            "Audio capture initialized",
            base_dir=capture_dir,
            keep_files=keep_captures,
        )
        self.streaming_playback_manager = StreamingPlaybackManager(
            self.session_store,
            self.ari_client,
            conversation_coordinator=self.conversation_coordinator,
            fallback_playback_manager=self.playback_manager,
            streaming_config=streaming_config,
            audio_transport=self.config.audio_transport,
            audio_diag_callback=self._update_audio_diagnostics_by_call,
            audio_capture_manager=self.audio_capture,
        )
        # Pre-seed audiosocket_format from YAML so provider audits use correct value
        try:
            initial_as_fmt = None
            if getattr(self.config, "audiosocket", None) and hasattr(self.config.audiosocket, "format"):
                initial_as_fmt = self.config.audiosocket.format
            if initial_as_fmt:
                self.streaming_playback_manager.set_transport(
                    audio_transport=self.config.audio_transport,
                    audiosocket_format=initial_as_fmt,
                )
        except Exception:
            logger.debug("Failed to pre-seed streaming manager format", exc_info=True)
        
        # Milestone7: Pipeline orchestrator coordinates per-call STT/LLM/TTS adapters.
        self.pipeline_orchestrator = PipelineOrchestrator(config)
        
        # DEBUG: Inspect loaded pipelines to verify tools
        try:
            lh = self.config.pipelines.get('local_hybrid')
            if lh:
                logger.info("DEBUG: local_hybrid pipeline config", tools=lh.tools, raw_entry=str(lh))
            else:
                logger.warning("DEBUG: local_hybrid pipeline not found in config")
        except Exception as e:
            logger.error("DEBUG: failed to inspect pipeline config", error=str(e))
        
        # P1: Transport orchestrator for multi-provider audio format negotiation
        self.transport_orchestrator = TransportOrchestrator(config.dict() if hasattr(config, 'dict') else config.__dict__)
        logger.info(
            "TransportOrchestrator initialized",
            profiles=list(self.transport_orchestrator.profiles.keys()),
            contexts=list(self.transport_orchestrator.contexts.keys()),
            default=self.transport_orchestrator.default_profile_name,
        )
        
        self.providers: Dict[str, AIProviderInterface] = {}
        # Track static codec/sample-rate validation issues per provider
        self.provider_alignment_issues: Dict[str, List[str]] = {}
        # Per-call provider streaming queues (AgentAudio -> streaming playback)
        self._provider_stream_queues: Dict[str, asyncio.Queue] = {}
        self._provider_stream_formats: Dict[str, Dict[str, Any]] = {}
        # Prevent duplicate runtime warnings per call when misalignment persists
        self._runtime_alignment_logged: Set[str] = set()
        # Per-call downstream audio preferences (format/sample-rate)
        self.call_audio_preferences: Dict[str, Dict[str, Any]] = {}
        self.conn_to_channel: Dict[str, str] = {}
        self.channel_to_conn: Dict[str, str] = {}
        self.conn_to_caller: Dict[str, str] = {}  # conn_id -> caller_channel_id
        self.audio_socket_server: Optional[AudioSocketServer] = None
        self.audiosocket_conn_to_ssrc: Dict[str, int] = {}
        self.audiosocket_resample_state: Dict[str, Optional[tuple]] = {}
        # Stateful resampling: maintain per-call/per-provider ratecv states to avoid drift
        # Provider input (caller -> provider) resample state
        self._resample_state_provider_in: Dict[str, Dict[str, Optional[tuple]]] = {}
        # Forced pipeline PCM16@16k path (per-call)
        self._resample_state_pipeline16k: Dict[str, Optional[tuple]] = {}
        # Enhanced VAD normalization to 8 kHz (per-call)
        self._resample_state_vad8k: Dict[str, Optional[tuple]] = {}
        self.pending_channel_for_bind: Optional[str] = None
        # Support duplicate Local ;1/;2 AudioSocket connections per call
        self.channel_to_conns: Dict[str, set] = {}
        self.audiosocket_primary_conn: Dict[str, str] = {}
        # Audio buffering for better playback quality
        self.audio_buffers: Dict[str, bytes] = {}
        self.buffer_size = 1600  # 200ms of audio at 8kHz (1600 bytes of ulaw)
        self.rtp_server: Optional[Any] = None
        self.headless_sessions: Dict[str, Dict[str, Any]] = {}
        # Bridge and Local channel tracking for Local Channel Bridge pattern
        self.bridges: Dict[str, str] = {}  # channel_id -> bridge_id
        self.local_channels: Dict[str, str] = {}  # channel_id -> legacy local_channel_id
        self.audiosocket_channels: Dict[str, str] = {}  # call_id -> audiosocket_channel_id
        # Streaming per-call persistent stream and gating state
        self._provider_stream_ids: Dict[str, str] = {}
        self._segment_tts_active: Set[str] = set()
        
        self.vad_manager: Optional[EnhancedVADManager] = None
        self.webrtc_vad = None
        try:
            vad_cfg = getattr(config, "vad", None)
            use_provider_vad = bool(getattr(vad_cfg, "use_provider_vad", False)) if vad_cfg else False
            if use_provider_vad:
                logger.info("Using provider-managed VAD; local VAD disabled")
            elif vad_cfg and getattr(vad_cfg, "enhanced_enabled", False):
                self.vad_manager = EnhancedVADManager(
                    energy_threshold=int(getattr(vad_cfg, "energy_threshold", 1500)),
                    confidence_threshold=float(getattr(vad_cfg, "confidence_threshold", 0.6)),
                    adaptive_threshold_enabled=bool(getattr(vad_cfg, "adaptive_threshold_enabled", False)),
                    noise_adaptation_rate=float(getattr(vad_cfg, "noise_adaptation_rate", 0.1)),
                    webrtc_aggressiveness=int(getattr(vad_cfg, "webrtc_aggressiveness", 1)),
                    min_speech_frames=int(getattr(vad_cfg, "webrtc_start_frames", 2)),
                    max_silence_frames=int(getattr(vad_cfg, "webrtc_end_silence_frames", 15)),
                )
                logger.info(
                    "Enhanced VAD enabled",
                    energy_threshold=self.vad_manager.energy_threshold,
                    confidence_threshold=self.vad_manager.confidence_threshold,
                )
                logger.info(
                    "🎯 WebRTC VAD settings",
                    aggressiveness=int(getattr(vad_cfg, "webrtc_aggressiveness", 1)),
                )
                if WEBRTC_VAD_AVAILABLE:
                    try:
                        aggressiveness = config.vad.webrtc_aggressiveness
                        self.webrtc_vad = webrtcvad.Vad(aggressiveness)
                        logger.info("🎤 WebRTC VAD initialized", aggressiveness=aggressiveness)
                    except Exception as e:
                        logger.warning("🎤 WebRTC VAD initialization failed", error=str(e))
                        self.webrtc_vad = None
                elif not use_provider_vad:
                    logger.warning("🎤 WebRTC VAD not available - install py-webrtcvad")
        except Exception:
            logger.error("Failed to initialize VAD components", exc_info=True)
        
        # Initialize Audio Gating Manager (for echo prevention in OpenAI Realtime)
        self.audio_gating_manager = None
        try:
            # Only initialize if VAD is available (needed for interrupt detection)
            if self.vad_manager:
                from src.core.audio_gating_manager import AudioGatingManager
                self.audio_gating_manager = AudioGatingManager(vad_manager=self.vad_manager)
                logger.info("🎛️ Audio gating manager initialized (OpenAI echo prevention)")
            else:
                logger.debug("Audio gating manager not initialized (VAD not available)")
        except Exception:
            logger.error("Failed to initialize audio gating manager", exc_info=True)
            self.audio_gating_manager = None
        
        # Map our synthesized UUID extension to the real ARI caller channel id
        self.uuidext_to_channel: Dict[str, str] = {}
        # NEW: Caller channel tracking for dual StasisStart handling
        self.pending_local_channels: Dict[str, str] = {}  # local_channel_id -> caller_channel_id
        self.pending_audiosocket_channels: Dict[str, str] = {}  # audiosocket_channel_id -> caller_channel_id
        self._audio_rx_debug: Dict[str, int] = {}
        self._keepalive_tasks: Dict[str, asyncio.Task] = {}
        # Track provider segment start timestamps per call for duration logging
        self._provider_segment_start_ts: Dict[str, float] = {}
        # Track provider AgentAudio chunk sequence per call for duration logging
        self._provider_chunk_seq: Dict[str, int] = {}
        # Track per-segment provider bytes vs. bytes enqueued to streaming
        self._provider_bytes: Dict[str, int] = {}
        self._enqueued_bytes: Dict[str, int] = {}
        # Transport observability
        self._transport_card_logged: Set[str] = set()
        # Audio Profile Resolution card one-shot tracker
        self._profile_card_logged: Set[str] = set()
        # Experimental coalescing: per-call buffer for provider TTS chunks
        self._provider_coalesce_buf: Dict[str, bytearray] = {}
        # Active playbacks are now managed by SessionStore
        # ExternalMedia to caller channel mapping is now managed by SessionStore
        # SSRC to caller channel mapping for RTP audio routing
        self.ssrc_to_caller: Dict[int, str] = {}  # ssrc -> caller_channel_id
        # Pipeline runtime structures (Milestone 7): per-call audio queues and runner tasks
        self._pipeline_queues: Dict[str, asyncio.Queue] = {}
        self._pipeline_tasks: Dict[str, asyncio.Task] = {}
        # Track calls where a pipeline was explicitly requested via AI_PROVIDER
        self._pipeline_forced: Dict[str, bool] = {}
        # Health server runner
        self._health_runner: Optional[web.AppRunner] = None

        # Event handlers
        self.ari_client.on_event("StasisStart", self._handle_stasis_start)
        self.ari_client.on_event("StasisEnd", self._handle_stasis_end)
        self.ari_client.on_event("ChannelDestroyed", self._handle_channel_destroyed)
        self.ari_client.on_event("ChannelDtmfReceived", self._handle_dtmf_received)
        self.ari_client.on_event("ChannelVarset", self._handle_channel_varset)

    async def on_rtp_packet(self, packet: bytes, addr: tuple):
        """Handle incoming RTP packets from the UDP server."""
        # ARCHITECT FIX: This legacy bypass fragments STT and bypasses VAD
        # Log warning and disable to ensure all audio goes through VAD
        logger.warning("🚨 LEGACY RTP BYPASS - This method bypasses VAD and fragments STT", 
                      packet_len=len(packet), 
                      addr=addr)
        
        # Disable this bypass to prevent STT fragmentation
        # All audio should go through RTPServer -> _on_rtp_audio -> _process_rtp_audio_with_vad
        return
        
        # LEGACY CODE (disabled):
        # if self.active_calls:
        #     channel_id = list(self.active_calls.keys())[0]
        #     call_data = self.active_calls[channel_id]
        #     provider = call_data.get("provider")
        #     if provider:
        #         # The first 12 bytes of an RTP packet are the header. The rest is payload.
        #         audio_payload = packet[12:]
        #         await provider.send_audio(audio_payload)

    async def _on_ari_event(self, event: Dict[str, Any]):
        """Default event handler for unhandled ARI events."""
        logger.debug("Received unhandled ARI event", event_type=event.get("type"), ari_event=event)

    async def _save_session(self, session: CallSession, *, new: bool = False) -> None:
        """Persist session updates and keep coordinator metrics in sync."""
        await self.session_store.upsert_call(session)
        if self.conversation_coordinator:
            if new:
                await self.conversation_coordinator.register_call(session)
            else:
                await self.conversation_coordinator.sync_from_session(session)

    async def start(self):
        """Connect to ARI and start the engine."""
        # 1) Load providers first (low risk)
        await self._load_providers()
        
        # Initialize tool calling system
        try:
            from src.tools.registry import tool_registry
            tool_registry.initialize_default_tools()
            logger.info("✅ Tool calling system initialized", tool_count=len(tool_registry.list_tools()))
        except Exception as e:
            logger.warning(f"Failed to initialize tool calling system: {e}", exc_info=True)

        # Milestone7: Start pipeline orchestrator to prepare per-call component lookups.
        try:
            await self.pipeline_orchestrator.start()
        except PipelineOrchestratorError as exc:
            logger.error(
                "Milestone7 pipeline orchestrator failed to start; legacy provider flow will be used",
                error=str(exc),
                exc_info=True,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error starting pipeline orchestrator",
                error=str(exc),
                exc_info=True,
            )

        # 2) Start health server EARLY so diagnostics are available even if transport/ARI fail
        try:
            asyncio.create_task(self._start_health_server())
        except Exception:
            logger.debug("Health server failed to start", exc_info=True)

        # 3) Log transport and downstream modes
        logger.info("Runtime modes", audio_transport=self.config.audio_transport, downstream_mode=self.config.downstream_mode)

        # 4) Prepare AudioSocket transport (guarded)
        if self.config.audio_transport == "audiosocket":
            try:
                if not self.config.audiosocket:
                    raise ValueError("AudioSocket configuration not found")

                host = self.config.audiosocket.host
                port = self.config.audiosocket.port
                self.audio_socket_server = AudioSocketServer(
                    host=host,
                    port=port,
                    on_uuid=self._audiosocket_handle_uuid,
                    on_audio=self._audiosocket_handle_audio,
                    on_disconnect=self._audiosocket_handle_disconnect,
                    on_dtmf=self._audiosocket_handle_dtmf,
                )
                await self.audio_socket_server.start()
                logger.info("AudioSocket server listening", host=host, port=port)
                # Configure streaming manager with AudioSocket format expected by dialplan
                as_format = None
                try:
                    if self.config.audiosocket and hasattr(self.config.audiosocket, 'format'):
                        as_format = self.config.audiosocket.format
                except Exception:
                    as_format = None
                self.streaming_playback_manager.set_transport(
                    audio_transport=self.config.audio_transport,
                    audiosocket_server=self.audio_socket_server,
                    audiosocket_format=as_format,
                )
                # Pre-call transport summary and alignment audit
                try:
                    self._audit_transport_alignment()
                except Exception:
                    logger.debug("Transport alignment audit failed", exc_info=True)
            except Exception as exc:
                logger.error("Failed to start AudioSocket transport", error=str(exc), exc_info=True)
                self.audio_socket_server = None

        # 5) Prepare RTP server for ExternalMedia transport (guarded)
        if self.config.audio_transport == "externalmedia":
            try:
                if not self.config.external_media:
                    raise ValueError("ExternalMedia configuration not found")
                
                rtp_host = self.config.external_media.rtp_host
                rtp_port = int(getattr(self.config.external_media, "rtp_port", 0) or 18080)
                codec = getattr(self.config.external_media, "codec", "ulaw")
                format = getattr(self.config.external_media, "format", "slin16")
                sample_rate = getattr(self.config.external_media, "sample_rate", None)
                
                # Infer sample_rate from format if not explicitly set
                if not sample_rate:
                    if format in ("slin16", "linear16", "pcm16"):
                        sample_rate = 16000
                    elif format in ("slin", "linear"):
                        sample_rate = 8000
                    else:  # ulaw, alaw
                        sample_rate = 8000
                
                port_range = self._parse_port_range(getattr(self.config.external_media, "port_range", None), rtp_port)
                
                # Create RTP server with callback to route audio to providers
                self.rtp_server = RTPServer(
                    host=rtp_host,
                    port=rtp_port,
                    engine_callback=self._on_rtp_audio,
                    codec=codec,
                    format=format,
                    sample_rate=sample_rate,
                    port_range=port_range,
                )
                
                # Start RTP server
                await self.rtp_server.start()
                logger.info("RTP server started for ExternalMedia transport", 
                           host=rtp_host, port=rtp_port, codec=codec, format=format, sample_rate=sample_rate)
                self.streaming_playback_manager.set_transport(
                    rtp_server=self.rtp_server,
                    audio_transport=self.config.audio_transport,
                )
                
                # Validate provider format alignment with ExternalMedia transport
                try:
                    for prov_name, provider in self.providers.items():
                        if hasattr(provider, 'config'):
                            cfg = provider.config
                            # Check provider input sample rate
                            provider_input_rate = getattr(cfg, 'provider_input_sample_rate_hz', None) or getattr(cfg, 'input_sample_rate_hz', None)
                            if provider_input_rate and provider_input_rate != sample_rate:
                                logger.warning(
                                    "⚠️  TRANSPORT/PROVIDER MISMATCH",
                                    provider=prov_name,
                                    transport="ExternalMedia",
                                    transport_rate=sample_rate,
                                    provider_rate=provider_input_rate,
                                    impact="Extra resampling step - slight quality loss",
                                    suggestion=f"Consider updating providers.{prov_name}.input_sample_rate_hz to {sample_rate} for optimal quality"
                                )
                except Exception:
                    logger.debug("Provider format validation failed", exc_info=True)
                
                # Pre-call transport summary and alignment audit
                try:
                    for prov_name, prov in self.providers.items():
                        issues = self._describe_provider_alignment(prov_name, prov)
                        if issues:
                            for issue in issues:
                                logger.info("Provider alignment info", provider=prov_name, issue=issue)
                except Exception:
                    logger.debug("Transport alignment audit failed", exc_info=True)
            except Exception as exc:
                logger.error("Failed to start ExternalMedia RTP transport", error=str(exc), exc_info=True)
                self.rtp_server = None

        # 6) Connect to ARI regardless to keep readiness visible and allow Stasis handling
        await self.ari_client.connect()
        # Add PlaybackFinished event handler for timing control
        self.ari_client.add_event_handler("PlaybackFinished", self._on_playback_finished)
        asyncio.create_task(self.ari_client.start_listening())
        logger.info("Engine started and listening for calls.")

    def _parse_port_range(self, value: Optional[Any], fallback_port: int) -> Tuple[int, int]:
        """Parse external_media.port_range into an inclusive (start, end) tuple."""
        try:
            if value is None:
                return (int(fallback_port), int(fallback_port))

            if isinstance(value, (list, tuple)) and len(value) == 2:
                start, end = int(value[0]), int(value[1])
            else:
                raw = str(value).strip()
                if not raw:
                    return (int(fallback_port), int(fallback_port))
                if ":" in raw:
                    start_s, end_s = raw.split(":", 1)
                elif "-" in raw:
                    start_s, end_s = raw.split("-", 1)
                else:
                    start_s = end_s = raw
                start, end = int(start_s), int(end_s)

            if start > end:
                start, end = end, start
            if start <= 0 or end <= 0:
                raise ValueError("Ports must be positive integers")
            return (start, end)
        except Exception:
            logger.warning(
                "Invalid external_media.port_range configuration; using fallback port",
                value=value,
                fallback=fallback_port,
            )
            return (int(fallback_port), int(fallback_port))

    async def stop(self):
        """Disconnect from ARI and stop the engine."""
        # Clean up all sessions from SessionStore
        sessions = await self.session_store.get_all_sessions()
        for session in sessions:
            await self._cleanup_call(session.call_id)
        await self.ari_client.disconnect()
        # Stop RTP server if running
        if hasattr(self, 'rtp_server') and self.rtp_server:
            await self.rtp_server.stop()
        # Stop health server
        if self.audio_socket_server:
            await self.audio_socket_server.stop()
            self.audio_socket_server = None
        try:
            if self._health_runner:
                await self._health_runner.cleanup()
        except Exception:
            logger.debug("Health server cleanup error", exc_info=True)
        # Milestone7: ensure orchestrator releases component assignments before shutdown.
        try:
            await self.pipeline_orchestrator.stop()
        except Exception:
            logger.debug("Pipeline orchestrator stop error", exc_info=True)
        logger.info("Engine stopped.")

    async def _load_providers(self):
        """Load and initialize AI providers from the configuration."""
        logger.info("Loading AI providers...", provider_names=list(self.config.providers.keys()))
        for name, provider_config_data in self.config.providers.items():
            if isinstance(provider_config_data, dict) and not provider_config_data.get("enabled", True):
                logger.info("Provider '%s' disabled in configuration; skipping initialization.", name)
                continue
            try:
                issues = self._audit_provider_config(name, provider_config_data)
                if issues:
                    self.provider_alignment_issues[name] = issues
                elif name in self.provider_alignment_issues:
                    self.provider_alignment_issues.pop(name, None)
                if name == "local":
                    config = LocalProviderConfig(**provider_config_data)
                    provider = LocalProvider(config, self.on_provider_event)
                    self.providers[name] = provider
                    logger.info(f"Provider '{name}' loaded successfully.")

                    # Provide initial greeting from global LLM config
                    try:
                        if hasattr(provider, 'set_initial_greeting'):
                            provider.set_initial_greeting(getattr(self.config.llm, 'initial_greeting', None))
                    except Exception:
                        logger.debug("Failed to set initial greeting on LocalProvider", exc_info=True)

                    # Initialize persistent connection for local provider
                    if hasattr(provider, 'initialize'):
                        await provider.initialize()
                        logger.info(f"Provider '{name}' connection initialized.")

                    runtime_issues = self._describe_provider_alignment(name, provider)
                    if runtime_issues:
                        self.provider_alignment_issues.setdefault(name, []).extend(runtime_issues)
                elif name == "deepgram":
                    deepgram_config = self._build_deepgram_config(provider_config_data)
                    if not deepgram_config:
                        continue

                    # Validate OpenAI dependency for Deepgram
                    if not self.config.llm.api_key:
                        logger.error("Deepgram provider requires OpenAI API key in LLM config")
                        continue

                    provider = DeepgramProvider(deepgram_config, self.config.llm, self.on_provider_event)
                    self.providers[name] = provider
                    logger.info("Provider 'deepgram' loaded successfully with OpenAI LLM dependency.")

                    runtime_issues = self._describe_provider_alignment(name, provider)
                    if runtime_issues:
                        self.provider_alignment_issues.setdefault(name, []).extend(runtime_issues)
                elif name == "openai_realtime":
                    openai_cfg = self._build_openai_realtime_config(provider_config_data)
                    if not openai_cfg:
                        continue

                    provider = OpenAIRealtimeProvider(
                        openai_cfg, 
                        self.on_provider_event,
                        gating_manager=self.audio_gating_manager
                    )
                    self.providers[name] = provider
                    logger.info(
                        "Provider 'openai_realtime' loaded successfully",
                        audio_gating_enabled=self.audio_gating_manager is not None
                    )

                    runtime_issues = self._describe_provider_alignment(name, provider)
                    if runtime_issues:
                        self.provider_alignment_issues.setdefault(name, []).extend(runtime_issues)
                elif name == "google_live":
                    # google_live uses GoogleProviderConfig like the pipeline adapters
                    try:
                        google_cfg = GoogleProviderConfig(**provider_config_data)
                    except Exception as e:
                        logger.error(f"Failed to build GoogleProviderConfig for google_live: {e}", exc_info=True)
                        continue

                    provider = GoogleLiveProvider(
                        google_cfg,
                        self.on_provider_event,
                        gating_manager=self.audio_gating_manager
                    )
                    self.providers[name] = provider
                    logger.info(
                        "Provider 'google_live' loaded successfully",
                        audio_gating_enabled=self.audio_gating_manager is not None
                    )

                    runtime_issues = self._describe_provider_alignment(name, provider)
                    if runtime_issues:
                        self.provider_alignment_issues.setdefault(name, []).extend(runtime_issues)
                else:
                    logger.warning(f"Unknown provider type: {name}")
                    continue
                    
            except Exception as e:
                logger.error(f"Failed to load provider '{name}': {e}", exc_info=True)
        
        # Validate that default provider is available
        if self.config.default_provider != "deepgram":
            available_providers = list(self.providers.keys())
            logger.error(f"Default provider '{self.config.default_provider}' not available. Available providers: {available_providers}")
        else:
            logger.info(f"Default provider '{self.config.default_provider}' is available and ready.")
            
            # Validate provider connectivity (full agent mode)
            for provider_name, provider in self.providers.items():
                # Check basic readiness
                try:
                    ready = provider.is_ready() if hasattr(provider, 'is_ready') else True
                    if not ready:
                        logger.error(
                            "Provider NOT ready",
                            provider=provider_name,
                            reason="is_ready() returned False"
                        )
                    else:
                        logger.info(
                            "Provider validated and ready",
                            provider=provider_name,
                            type=provider.__class__.__name__
                        )
                except Exception as exc:
                    logger.error(
                        "Provider readiness check failed",
                        provider=provider_name,
                        error=str(exc),
                        exc_info=True
                    )
            
            # Check codec/sample alignment
            for provider_name in self.providers:
                issues = self.provider_alignment_issues.get(provider_name, [])
                for detail in dict.fromkeys(issues):
                    logger.warning(
                        "Provider codec/sample alignment issue",
                        provider=provider_name,
                        detail=detail,
                    )
                if not issues:
                    logger.info(
                        "Provider codec/sample alignment verified",
                        provider=provider_name,
                    )

    def _is_caller_channel(self, channel: dict) -> bool:
        """Check if this is a caller channel (SIP, PJSIP, etc.)"""
        channel_name = channel.get('name', '')
        return any(channel_name.startswith(prefix) for prefix in ['SIP/', 'PJSIP/', 'DAHDI/', 'IAX2/'])

    def _is_local_channel(self, channel: dict) -> bool:
        """Check if this is a Local channel"""
        channel_name = channel.get('name', '')
        return channel_name.startswith('Local/')

    def _is_audiosocket_channel(self, channel: dict) -> bool:
        """Check if this is an AudioSocket channel (native channel interface)."""
        channel_name = channel.get('name', '')
        return channel_name.startswith('AudioSocket/')

    def _is_external_media_channel(self, channel: dict) -> bool:
        """Check if this is an ExternalMedia channel"""
        channel_name = channel.get('name', '')
        return channel_name.startswith('UnicastRTP/')

    async def _find_caller_for_local(self, local_channel_id: str) -> Optional[str]:
        """Find the caller channel that corresponds to this Local channel."""
        # Check if we have a pending Local channel mapping
        if local_channel_id in self.pending_local_channels:
            return self.pending_local_channels[local_channel_id]
        
        # Fallback: search through SessionStore
        sessions = await self.session_store.get_all_sessions()
        for session in sessions:
            if session.local_channel_id == local_channel_id:
                return session.caller_channel_id
        
        return None

    async def _handle_stasis_start(self, event: dict):
        """Handle StasisStart events - Hybrid ARI approach with single handler."""
        logger.info("🎯 HYBRID ARI - StasisStart event received", event_data=event)
        channel = event.get('channel', {})
        channel_id = channel.get('id')
        channel_name = channel.get('name', '')
        args = event.get('args', [])
        
        logger.info("🎯 HYBRID ARI - Channel analysis", 
                   channel_id=channel_id,
                   channel_name=channel_name,
                   args=args,
                   is_caller=self._is_caller_channel(channel),
                   is_local=self._is_local_channel(channel))
        
        # Check if this is an agent action (transfer, voicemail, queue, etc.)
        if args and len(args) > 0:
            action_type = args[0]
            logger.info(f"🔀 AGENT ACTION - Stasis entry with action: {action_type}",
                       channel_id=channel_id,
                       action_type=action_type,
                       args=args)
            await self._handle_agent_action_stasis(channel_id, channel, args)
            return
        
        if self._is_caller_channel(channel):
            # This is the caller channel entering Stasis - MAIN FLOW
            logger.info("🎯 HYBRID ARI - Processing caller channel", channel_id=channel_id)
            await self._handle_caller_stasis_start_hybrid(channel_id, channel)
        elif self._is_local_channel(channel):
            # This is the Local channel entering Stasis - legacy path
            logger.info("🎯 HYBRID ARI - Local channel entered Stasis",
                       channel_id=channel_id,
                       channel_name=channel_name)
            # Now add the Local channel to the bridge
            await self._handle_local_stasis_start_hybrid(channel_id, channel)
        elif self._is_audiosocket_channel(channel):
            logger.info(
                "🎯 HYBRID ARI - AudioSocket channel entered Stasis",
                channel_id=channel_id,
                channel_name=channel_name,
            )
            await self._handle_audiosocket_channel_stasis_start(channel_id, channel)
        elif self._is_external_media_channel(channel):
            # This is an ExternalMedia channel entering Stasis
            logger.info("🎯 EXTERNAL MEDIA - ExternalMedia channel entered Stasis", 
                       channel_id=channel_id,
                       channel_name=channel_name)
            await self._handle_external_media_stasis_start(channel_id, channel)
        else:
            logger.warning("🎯 HYBRID ARI - Unknown channel type in StasisStart", 
                          channel_id=channel_id, 
                          channel_name=channel_name)

    async def _start_external_media_channel(self, caller_channel_id: str) -> Optional[str]:
        """Allocate RTP resources and originate the ExternalMedia channel via ARI."""
        if not self.config.external_media:
            logger.error("🎯 EXTERNAL MEDIA - Configuration missing; cannot start ExternalMedia channel",
                         caller_channel_id=caller_channel_id)
            return None
        if not self.rtp_server:
            logger.error("🎯 EXTERNAL MEDIA - RTP server unavailable; cannot start ExternalMedia channel",
                         caller_channel_id=caller_channel_id)
            return None

        try:
            port = await self.rtp_server.allocate_session(caller_channel_id)
        except Exception as exc:
            logger.error("🎯 EXTERNAL MEDIA - RTP session allocation failed",
                         caller_channel_id=caller_channel_id,
                         error=str(exc),
                         exc_info=True)
            return None

        host = self.config.external_media.rtp_host
        codec = getattr(self.config.external_media, "codec", "ulaw")
        direction = getattr(self.config.external_media, "direction", "both")
        external_host = f"{host}:{port}"

        try:
            response = await self.ari_client.create_external_media_channel(
                app=self.config.asterisk.app_name,
                external_host=external_host,
                format=codec,
                direction=direction,
                encapsulation="rtp",
            )
        except Exception as exc:
            logger.error("🎯 EXTERNAL MEDIA - ARI create_external_media_channel failed",
                         caller_channel_id=caller_channel_id,
                         external_host=external_host,
                         error=str(exc),
                         exc_info=True)
            await self.rtp_server.cleanup_session(caller_channel_id)
            try:
                session = await self.session_store.get_by_call_id(caller_channel_id)
                if session:
                    session.external_media_port = None
                    session.pending_external_media_id = None
                    await self._save_session(session)
            except Exception:
                logger.debug("Failed to reset session after ARI external media failure",
                             caller_channel_id=caller_channel_id,
                             exc_info=True)
            return None

        channel_id = response.get("id") if isinstance(response, dict) else None
        if not channel_id:
            logger.error("🎯 EXTERNAL MEDIA - ARI create_external_media_channel returned no channel id",
                         caller_channel_id=caller_channel_id,
                         response=response)
            await self.rtp_server.cleanup_session(caller_channel_id)
            try:
                session = await self.session_store.get_by_call_id(caller_channel_id)
                if session:
                    session.external_media_port = None
                    session.pending_external_media_id = None
                    await self._save_session(session)
            except Exception:
                logger.debug("Failed to reset session after missing ExternalMedia channel id",
                             caller_channel_id=caller_channel_id,
                             exc_info=True)
            return None

        session = await self.session_store.get_by_call_id(caller_channel_id)
        if session:
            session.pending_external_media_id = channel_id
            session.external_media_port = port
            session.external_media_codec = codec  # Store codec for RTP byte-swap logic
            await self._save_session(session)

        logger.info("🎯 EXTERNAL MEDIA - ExternalMedia channel originated",
                    caller_channel_id=caller_channel_id,
                    external_media_id=channel_id,
                    rtp_host=host,
                    rtp_port=port,
                    codec=codec,
                    direction=direction)
        return channel_id

    async def _handle_external_media_stasis_start(self, external_media_id: str, channel: dict):
        """Handle ExternalMedia channel entering Stasis."""
        try:
            # Find session by external_media_id
            session = await self.session_store.get_by_channel_id(external_media_id)
            if not session:
                # Fallback: search all sessions for external_media_id
                sessions = await self.session_store.get_all_sessions()
                for s in sessions:
                    if s.external_media_id == external_media_id:
                        session = s
                        break
            
            if not session:
                logger.warning("ExternalMedia channel entered Stasis but no caller found", 
                             external_media_id=external_media_id)
                return
            
            caller_channel_id = session.caller_channel_id
            
            # Add ExternalMedia channel to the bridge
            bridge_id = session.bridge_id
            if bridge_id:
                success = await self.ari_client.add_channel_to_bridge(bridge_id, external_media_id)
                if success:
                    session.external_media_id = external_media_id
                    session.pending_external_media_id = None
                    await self._save_session(session)
                    logger.info("🎯 EXTERNAL MEDIA - ExternalMedia channel added to bridge", 
                               external_media_id=external_media_id,
                               bridge_id=bridge_id,
                               caller_channel_id=caller_channel_id)
                    
                    # Start the provider session now that media path is connected
                    await self._start_provider_session(caller_channel_id)
                else:
                    logger.error("🎯 EXTERNAL MEDIA - Failed to add ExternalMedia channel to bridge", 
                               external_media_id=external_media_id,
                               bridge_id=bridge_id)
            else:
                logger.error("ExternalMedia channel entered Stasis but no bridge found", 
                           external_media_id=external_media_id,
                           caller_channel_id=caller_channel_id)
                
        except Exception as e:
            logger.error("Error handling ExternalMedia StasisStart", 
                        external_media_id=external_media_id, 
                        error=str(e), 
                        exc_info=True)

    async def _handle_caller_stasis_start_hybrid(self, caller_channel_id: str, channel: dict):
        """Handle caller channel entering Stasis - Hybrid ARI approach."""
        caller_info = channel.get('caller', {})
        logger.info("🎯 HYBRID ARI - Caller channel entered Stasis", 
                    channel_id=caller_channel_id,
                    caller_name=caller_info.get('name'),
                    caller_number=caller_info.get('number'))
        
        # Check if call is already in progress
        existing_session = await self.session_store.get_by_call_id(caller_channel_id)
        if existing_session:
            logger.warning("🎯 HYBRID ARI - Caller already in progress", channel_id=caller_channel_id)
            return
        
        try:
            # Answer the caller
            logger.info("🎯 HYBRID ARI - Step 1: Answering caller channel", channel_id=caller_channel_id)
            await self.ari_client.answer_channel(caller_channel_id)
            logger.info("🎯 HYBRID ARI - Step 1: ✅ Caller channel answered", channel_id=caller_channel_id)
            
            # Create bridge immediately (use default bridge_type to prevent simple_bridge optimization)
            logger.info("🎯 HYBRID ARI - Step 2: Creating bridge immediately", channel_id=caller_channel_id)
            bridge_id = await self.ari_client.create_bridge()  # Uses default: mixing,dtmf_events,proxy_media
            if not bridge_id:
                raise RuntimeError("Failed to create mixing bridge")
            logger.info("🎯 HYBRID ARI - Step 2: ✅ Bridge created", 
                       channel_id=caller_channel_id, 
                       bridge_id=bridge_id)
            
            # Add caller to bridge
            logger.info("🎯 HYBRID ARI - Step 3: Adding caller to bridge", 
                       channel_id=caller_channel_id, 
                       bridge_id=bridge_id)
            caller_success = await self.ari_client.add_channel_to_bridge(bridge_id, caller_channel_id)
            if not caller_success:
                raise RuntimeError("Failed to add caller channel to bridge")
            logger.info("🎯 HYBRID ARI - Step 3: ✅ Caller added to bridge", 
                       channel_id=caller_channel_id, 
                       bridge_id=bridge_id)
            self.bridges[caller_channel_id] = bridge_id
            
            # Create CallSession and store in SessionStore
            session = CallSession(
                call_id=caller_channel_id,
                caller_channel_id=caller_channel_id,
                caller_name=caller_info.get('name'),
                caller_number=caller_info.get('number'),
                bridge_id=bridge_id,
                provider_name=self.config.default_provider,
                audio_capture_enabled=True,  # FIX #1: Start with capture enabled, only disable when TTS actually starts
                status="connected",
                start_time=datetime.now()  # Track call start time for email tools
            )
            session.enhanced_vad_enabled = bool(self.vad_manager)
            await self._save_session(session, new=True)
            
            # Record call start time for duration tracking
            import time
            _call_start_times[caller_channel_id] = time.time()
            logger.debug("Recorded call start time", call_id=caller_channel_id)
            
            # Export config metrics for this call
            try:
                await self._export_config_metrics(caller_channel_id)
            except Exception:
                logger.debug("Failed to export config metrics for call", call_id=caller_channel_id, exc_info=True)
            logger.info("🎯 HYBRID ARI - Step 4: ✅ Caller session created and stored",
                       channel_id=caller_channel_id,
                       bridge_id=bridge_id)

            # Resolve transport profile from dialplan hints/config defaults
            try:
                await self._hydrate_transport_from_dialplan(session, caller_channel_id)
            except Exception:
                logger.debug("Transport profile hydration failed", call_id=caller_channel_id, exc_info=True)

            # Detect caller codec/sample-rate so downstream playback matches the trunk.
            try:
                await self._detect_caller_codec(session, caller_channel_id)
            except Exception:
                logger.debug("Caller codec detection failed", call_id=caller_channel_id, exc_info=True)

            # P1: Resolve Audio Profile (profiles.* + contexts.* + channel var overrides)
            try:
                await self._resolve_audio_profile(session, caller_channel_id)
            except Exception:
                logger.debug("Audio profile resolution failed", call_id=caller_channel_id, exc_info=True)

            # Milestone7: Per-call override via Asterisk channel var AI_PROVIDER.
            # Values:
            #   - openai_realtime | deepgram → full agent override
            #   - customX (any other token) → pipeline name
            ai_provider_value = None
            try:
                resp = await self.ari_client.send_command(
                    "GET",
                    f"channels/{caller_channel_id}/variable",
                    params={"variable": "AI_PROVIDER"},
                )
                if isinstance(resp, dict):
                    ai_provider_value = (resp.get("value") or "").strip()
            except Exception:
                logger.debug(
                    "AI_PROVIDER read failed; continuing with defaults",
                    channel_id=caller_channel_id,
                    exc_info=True,
                )

            provider_aliases = {
                "openai": "openai_realtime",
                "deepgram_agent": "deepgram",
                "google": "google_live",
            }
            resolved_provider = (
                provider_aliases.get(ai_provider_value, ai_provider_value)
                if ai_provider_value
                else None
            )

            pipeline_resolution = None
            if resolved_provider and resolved_provider in self.providers:
                # Full agent override for this call
                previous = session.provider_name
                session.provider_name = resolved_provider
                await self._save_session(session)
                logger.info(
                    "AI provider override applied from channel variable",
                    channel_id=caller_channel_id,
                    variable="AI_PROVIDER",
                    value=ai_provider_value,
                    resolved_provider=resolved_provider,
                    previous_provider=previous,
                    resolved_mode="full_agent",
                )
            elif ai_provider_value:
                # Treat as a pipeline name for this call
                pipeline_resolution = await self._assign_pipeline_to_session(
                    session, pipeline_name=ai_provider_value
                )
                if pipeline_resolution:
                    logger.info(
                        "AI pipeline selection applied from channel variable",
                        channel_id=caller_channel_id,
                        variable="AI_PROVIDER",
                        value=ai_provider_value,
                        pipeline=pipeline_resolution.pipeline_name,
                        components=pipeline_resolution.component_summary(),
                        resolved_mode="pipeline",
                    )
                    # Opt-in to adapter-driven pipeline execution for this call
                    try:
                        await self._ensure_pipeline_runner(session, forced=True)
                    except Exception:
                        logger.debug("Failed to start pipeline runner", call_id=caller_channel_id, exc_info=True)
                elif getattr(self.pipeline_orchestrator, "started", False):
                    logger.warning(
                        "Requested pipeline via AI_PROVIDER not found; falling back",
                        channel_id=caller_channel_id,
                        requested_pipeline=ai_provider_value,
                    )
                    pipeline_resolution = await self._assign_pipeline_to_session(session)
            else:
                # Default behavior: only use active_pipeline if no valid provider already set
                # Skip pipeline resolution if context already set a monolithic provider
                if session.provider_name and session.provider_name in self.providers:
                    logger.info(
                        "Skipping pipeline resolution - context already set valid provider",
                        call_id=caller_channel_id,
                        provider=session.provider_name,
                        source="context",
                    )
                else:
                    pipeline_resolution = await self._assign_pipeline_to_session(session)
                    if not pipeline_resolution and getattr(self.pipeline_orchestrator, "started", False):
                        logger.info(
                            "Milestone7 pipeline orchestrator falling back to legacy provider flow",
                            call_id=caller_channel_id,
                            provider=session.provider_name,
                        )
            
            # Step 5: Create ExternalMedia channel or originate Local channel
            if self.config.audio_transport == "externalmedia":
                logger.info("🎯 EXTERNAL MEDIA - Step 5: Creating ExternalMedia channel", channel_id=caller_channel_id)
                external_media_id = await self._start_external_media_channel(caller_channel_id)
                if external_media_id:
                    # Update session with ExternalMedia ID
                    session.external_media_id = external_media_id
                    session.status = "external_media_created"
                    await self._save_session(session)
                    logger.info("🎯 EXTERNAL MEDIA - ExternalMedia channel created, session updated", 
                               channel_id=caller_channel_id, 
                               external_media_id=external_media_id)
                else:
                    logger.error("🎯 EXTERNAL MEDIA - Failed to create ExternalMedia channel", channel_id=caller_channel_id)
            else:
                logger.info("🎯 HYBRID ARI - Step 5: Originating AudioSocket channel", channel_id=caller_channel_id)
                await self._originate_audiosocket_channel_hybrid(caller_channel_id)
            
        except Exception as e:
            logger.error("🎯 HYBRID ARI - Failed to handle caller StasisStart", 
                        caller_channel_id=caller_channel_id, 
                        error=str(e), exc_info=True)
            await self._cleanup_call(caller_channel_id)

    async def _handle_local_stasis_start_hybrid(self, local_channel_id: str, channel: dict):
        """Handle Local channel entering Stasis - Hybrid ARI approach."""
        logger.info("🎯 HYBRID ARI - Processing Local channel StasisStart", 
                   local_channel_id=local_channel_id)
        
        # Find the caller channel that this Local channel belongs to
        caller_channel_id = await self._find_caller_for_local(local_channel_id)
        if not caller_channel_id:
            logger.error("🎯 HYBRID ARI - No caller found for Local channel", 
                        local_channel_id=local_channel_id)
            await self.ari_client.hangup_channel(local_channel_id)
            return
        
        # Check if caller channel exists and has a bridge
        session = await self.session_store.get_by_call_id(caller_channel_id)
        if not session:
            logger.error("🎯 HYBRID ARI - Caller channel not found for Local channel", 
                        local_channel_id=local_channel_id,
                        caller_channel_id=caller_channel_id)
            await self.ari_client.hangup_channel(local_channel_id)
            return
        
        bridge_id = session.bridge_id
        
        try:
            # Add Local channel to bridge
            logger.info("🎯 HYBRID ARI - Adding Local channel to bridge", 
                       local_channel_id=local_channel_id,
                       bridge_id=bridge_id)
            local_success = await self.ari_client.add_channel_to_bridge(bridge_id, local_channel_id)
            if local_success:
                logger.info("🎯 HYBRID ARI - ✅ Local channel added to bridge", 
                           local_channel_id=local_channel_id,
                           bridge_id=bridge_id)
                # Update session with Local channel info
                session.local_channel_id = local_channel_id
                session.status = "connected"
                await self._save_session(session)
                self.local_channels[caller_channel_id] = local_channel_id
                
                
                # Start provider session now that media path is connected
                await self._start_provider_session(caller_channel_id)
            else:
                logger.error("🎯 HYBRID ARI - Failed to add Local channel to bridge", 
                           local_channel_id=local_channel_id,
                           bridge_id=bridge_id)
                await self.ari_client.hangup_channel(local_channel_id)
        except Exception as e:
            logger.error("🎯 HYBRID ARI - Failed to handle Local channel StasisStart", 
                        local_channel_id=local_channel_id,
                        error=str(e), exc_info=True)
            await self.ari_client.hangup_channel(local_channel_id)

    async def _handle_audiosocket_channel_stasis_start(self, audiosocket_channel_id: str, channel: dict):
        """Handle AudioSocket channel entering Stasis when using channel interface."""
        logger.info(
            "🎯 HYBRID ARI - Processing AudioSocket channel StasisStart",
            audiosocket_channel_id=audiosocket_channel_id,
            channel_name=channel.get('name'),
        )

        caller_channel_id = self.pending_audiosocket_channels.pop(audiosocket_channel_id, None)
        if not caller_channel_id:
            # Fallback 1: try to parse the AudioSocket UUID from the channel name and map via uuidext_to_channel
            name = channel.get('name', '') or ''
            parsed_uuid = None
            try:
                # Expected form: "AudioSocket/host:port-<uuid>"; take substring after last '-'
                if name.startswith('AudioSocket/') and '-' in name:
                    candidate = name.rsplit('-', 1)[-1]
                    # Basic UUID sanity (contains 4 dashes)
                    if candidate.count('-') == 4:
                        parsed_uuid = candidate
            except Exception:
                parsed_uuid = None

            if parsed_uuid and parsed_uuid in self.uuidext_to_channel:
                caller_channel_id = self.uuidext_to_channel.get(parsed_uuid)
            
            # Fallback 2: brief retry loop to allow originate path to record mappings
            if not caller_channel_id:
                for attempt in range(5):
                    await asyncio.sleep(0.05)
                    # Recheck pending mapping
                    caller_channel_id = self.pending_audiosocket_channels.pop(audiosocket_channel_id, None)
                    if caller_channel_id:
                        break
                    # Recheck uuid mapping if we parsed one
                    if parsed_uuid and parsed_uuid in self.uuidext_to_channel:
                        caller_channel_id = self.uuidext_to_channel.get(parsed_uuid)
                        if caller_channel_id:
                            break
            
            # Fallback 3: scan sessions as a last resort
            if not caller_channel_id:
                sessions = await self.session_store.get_all_sessions()
                for s in sessions:
                    if getattr(s, 'audiosocket_channel_id', None) == audiosocket_channel_id:
                        caller_channel_id = s.caller_channel_id
                        break

        if not caller_channel_id:
            logger.error(
                "🎯 HYBRID ARI - No caller found for AudioSocket channel",
                audiosocket_channel_id=audiosocket_channel_id,
                channel_name=channel.get('name'),
            )
            await self.ari_client.hangup_channel(audiosocket_channel_id)
            return

        session = await self.session_store.get_by_call_id(caller_channel_id)
        if not session:
            logger.error(
                "🎯 HYBRID ARI - Session missing for AudioSocket channel",
                audiosocket_channel_id=audiosocket_channel_id,
                caller_channel_id=caller_channel_id,
            )
            await self.ari_client.hangup_channel(audiosocket_channel_id)
            return

        bridge_id = session.bridge_id
        if not bridge_id:
            logger.error(
                "🎯 HYBRID ARI - No bridge available for AudioSocket channel",
                audiosocket_channel_id=audiosocket_channel_id,
                caller_channel_id=caller_channel_id,
            )
            await self.ari_client.hangup_channel(audiosocket_channel_id)
            return

        try:
            added = await self.ari_client.add_channel_to_bridge(bridge_id, audiosocket_channel_id)
            if not added:
                raise RuntimeError("Failed to add AudioSocket channel to bridge")

            logger.info(
                "🎯 HYBRID ARI - ✅ AudioSocket channel added to bridge",
                audiosocket_channel_id=audiosocket_channel_id,
                bridge_id=bridge_id,
                caller_channel_id=caller_channel_id,
            )

            session.audiosocket_channel_id = audiosocket_channel_id
            session.status = "audiosocket_channel_connected"
            await self._save_session(session)

            self.audiosocket_channels[caller_channel_id] = audiosocket_channel_id
            self.bridges[audiosocket_channel_id] = bridge_id

            if not session.provider_session_active:
                await self._start_provider_session(caller_channel_id)

            # Start ARI channel recording on the AudioSocket channel (only when diagnostics enabled)
            # Check if diagnostic taps are enabled
            diag_enabled = False
            try:
                diag_enabled = bool(getattr(self.config.streaming, 'diag_enable_taps', False)) if hasattr(self.config, 'streaming') else False
            except Exception:
                pass
            
            if diag_enabled:
                try:
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    rec_name = f"out-{caller_channel_id}-{ts}"
                    ok = await self.ari_client.record_channel(
                        audiosocket_channel_id,
                        name=rec_name,
                        format="wav",
                        if_exists="overwrite",
                        max_duration_seconds=360,
                        max_silence_seconds=0,
                        beep=False,
                        terminate_on="none",
                    )
                    if ok:
                        logger.info(
                            "📼 ARI channel recording started on AudioSocket channel",
                            audiosocket_channel_id=audiosocket_channel_id,
                            name=rec_name,
                        )
                    else:
                        logger.debug(
                            "ARI channel recording failed to start (diagnostic recording)",
                            audiosocket_channel_id=audiosocket_channel_id,
                            name=rec_name,
                        )
                except Exception:
                    logger.debug("ARI channel recording start failed (diagnostic recording)", exc_info=True)
            else:
                logger.debug(
                    "ARI channel recording skipped (diag_enable_taps not enabled)",
                    audiosocket_channel_id=audiosocket_channel_id,
                )
        except Exception as exc:
            logger.error(
                "🎯 HYBRID ARI - Failed to process AudioSocket channel",
                audiosocket_channel_id=audiosocket_channel_id,
                caller_channel_id=caller_channel_id,
                error=str(exc),
                exc_info=True,
            )
            await self.ari_client.hangup_channel(audiosocket_channel_id)

    async def _handle_agent_action_stasis(self, channel_id: str, channel: dict, args: list):
        """
        Handle agent action channels entering Stasis (direct SIP origination via ARI).
        
        Channels enter Stasis directly when originated by tool execution (e.g., transfer_call).
        NO dialplan context is used - channels are originated with app="asterisk-ai-voice-agent".
        
        Args:
            channel_id: Channel that entered Stasis
            channel: Channel dict
            args: Stasis args [action_type, caller_id, target, ...]
        """
        if len(args) < 2:
            logger.error("🔀 AGENT ACTION - Insufficient args", 
                        channel_id=channel_id, args=args)
            await self.ari_client.hangup_channel(channel_id)
            return
        
        action_type = args[0]
        caller_id = args[1]
        
        logger.info("🔀 AGENT ACTION - Processing action",
                   action_type=action_type,
                   caller_id=caller_id,
                   channel_id=channel_id)
        
        # Route to specific handler based on action type
        handlers = {
            'transfer': self._handle_transfer_answered,
            'warm-transfer': self._handle_transfer_answered,  # Warm transfer uses same handler
            'transfer-failed': self._handle_transfer_failed,
            'voicemail-complete': self._handle_voicemail_complete,
            'queue-answered': self._handle_queue_answered,
            'queue-failed': self._handle_queue_failed,
        }
        
        handler = handlers.get(action_type)
        if handler:
            await handler(channel_id, args)
        else:
            logger.warning(f"🔀 AGENT ACTION - Unknown action type: {action_type}",
                          channel_id=channel_id, args=args)
            await self.ari_client.hangup_channel(channel_id)
    
    async def _handle_transfer_answered(self, channel_id: str, args: list):
        """
        Handle successful transfer (target answered).
        Args: ['warm-transfer', caller_id, target_extension]
        
        With direct SIP origination:
        - SIP channel (e.g., SIP/6000) enters Stasis directly on answer
        - We remove AI (UnicastRTP), stop provider, then bridge SIP to caller
        - Creates direct audio path: Caller ↔ SIP/Agent
        """
        action_type = args[0]
        caller_id = args[1]
        target = args[2] if len(args) > 2 else 'unknown'
        
        logger.info("🔀 TRANSFER ANSWERED - Direct SIP channel",
                   action_type=action_type,
                   channel_id=channel_id,
                   caller_id=caller_id,
                   target=target)
        
        # Find session
        session = await self.session_store.get_by_call_id(caller_id)
        if not session:
            logger.error("🔀 TRANSFER - Session not found",
                        caller_id=caller_id)
            await self.ari_client.hangup_channel(channel_id)
            return
        
        # Step 1: Remove UnicastRTP/ExternalMedia from bridge
        if session.external_media_id:
            try:
                await self.ari_client.remove_channel_from_bridge(
                    session.bridge_id,
                    session.external_media_id
                )
                logger.info("✅ UnicastRTP removed from bridge",
                           external_media_id=session.external_media_id)
            except Exception as e:
                logger.warning(f"Failed to remove UnicastRTP: {e}")
        
        # Step 2: Stop AI provider session
        provider = self.providers.get(session.provider_name)
        if provider:
            try:
                # Stop the provider's session for this call
                if hasattr(provider, 'stop_session'):
                    await provider.stop_session()
                    logger.info("✅ AI provider session stopped",
                               provider=session.provider_name)
            except Exception as e:
                logger.warning(f"Failed to stop provider: {e}")
        
        # Step 3: Bridge SIP channel directly to caller
        try:
            await self.ari_client.add_channel_to_bridge(
                session.bridge_id,
                channel_id  # This is SIP/6000 directly
            )
            logger.info("✅ TRANSFER COMPLETE - Direct SIP channel bridged",
                       channel_id=channel_id,
                       bridge_id=session.bridge_id,
                       target=target)
            
            # Step 4: Update session state
            if session.current_action:
                session.current_action['answered'] = True
                session.current_action['channel_id'] = channel_id
            await self.session_store.upsert_call(session)
            
        except Exception as e:
            logger.error(f"🔀 TRANSFER - Failed to bridge: {e}",
                        channel_id=channel_id)
            await self.ari_client.hangup_channel(channel_id)
    
    async def _handle_transfer_failed(self, channel_id: str, args: list):
        """
        Handle failed transfer (target didn't answer).
        Args: ['transfer-failed', caller_id, target, dial_status]
        """
        caller_id = args[1]
        target = args[2] if len(args) > 2 else 'unknown'
        status = args[3] if len(args) > 3 else 'UNKNOWN'
        
        logger.info("🔀 TRANSFER FAILED",
                   channel_id=channel_id,
                   caller_id=caller_id,
                   target=target,
                   status=status)
        
        # Find session and stop MOH
        session = await self.session_store.get_by_call_id(caller_id)
        if session:
            try:
                await self.ari_client.send_command(
                    method="DELETE",
                    resource=f"channels/{session.caller_channel_id}/moh"
                )
            except:
                pass
            
            # Clear current action
            session.current_action = None
            await self.session_store.upsert_call(session)
        
        # Hangup the Local channel
        await self.ari_client.hangup_channel(channel_id)
    
    async def _handle_voicemail_complete(self, channel_id: str, args: list):
        """Handle voicemail completion."""
        caller_id = args[1]
        vmbox = args[2] if len(args) > 2 else 'unknown'
        
        logger.info("📧 VOICEMAIL COMPLETE", vmbox=vmbox)
        await self.ari_client.hangup_channel(channel_id)
    
    async def _handle_queue_answered(self, channel_id: str, args: list):
        """Handle queue agent answered."""
        caller_id = args[1]
        queue_name = args[2] if len(args) > 2 else 'unknown'
        
        logger.info("📞 QUEUE ANSWERED", queue=queue_name)
        
        # Similar to transfer_answered - bridge the channel
        session = await self.session_store.get_by_call_id(caller_id)
        if session:
            try:
                await self.ari_client.add_channel_to_bridge(
                    session.bridge_id,
                    channel_id
                )
                logger.info("✅ QUEUE AGENT BRIDGED")
            except Exception as e:
                logger.error(f"Failed to bridge queue agent: {e}")
                await self.ari_client.hangup_channel(channel_id)
    
    async def _handle_queue_failed(self, channel_id: str, args: list):
        """Handle queue failure."""
        caller_id = args[1]
        logger.info("📞 QUEUE FAILED")
        await self.ari_client.hangup_channel(channel_id)

    async def _originate_audiosocket_channel_hybrid(self, caller_channel_id: str):
        """Originate an AudioSocket channel using the native channel interface."""
        if not self.config.audiosocket:
            logger.error(
                "🎯 HYBRID ARI - AudioSocket config missing, cannot originate channel",
                caller_channel_id=caller_channel_id,
            )
            raise RuntimeError("AudioSocket configuration missing")

        audio_uuid = str(uuid.uuid4())
        host = self.config.audiosocket.host or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        port = self.config.audiosocket.port
        # Match channel interface codec to YAML audiosocket.format
        codec = "slin"
        try:
            fmt = (getattr(self.config.audiosocket, 'format', '') or '').lower()
            if fmt in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                codec = "ulaw"
            elif fmt in ("slin16", "linear16", "pcm16"):
                codec = "slin16"
            else:
                # Treat any other/legacy value (e.g., 'slin') as 8 kHz PCM16
                codec = "slin"
        except Exception:
            codec = "slin"
        endpoint = f"AudioSocket/{host}:{port}/{audio_uuid}/c({codec})"

        orig_params = {
            "endpoint": endpoint,
            "app": self.config.asterisk.app_name,
            "timeout": "30",
            "channelVars": {
                "AUDIOSOCKET_UUID": audio_uuid,
            },
        }

        logger.info(
            "🎯 HYBRID ARI - Originating AudioSocket channel",
            caller_channel_id=caller_channel_id,
            endpoint=endpoint,
            audio_uuid=audio_uuid,
        )

        try:
            response = await self.ari_client.send_command("POST", "channels", params=orig_params)
            if response and response.get("id"):
                audiosocket_channel_id = response["id"]
                self.pending_audiosocket_channels[audiosocket_channel_id] = caller_channel_id
                self.uuidext_to_channel[audio_uuid] = caller_channel_id

                session = await self.session_store.get_by_call_id(caller_channel_id)
                if session:
                    session.audiosocket_uuid = audio_uuid
                    await self._save_session(session)
                    logger.info(
                        "🎯 HYBRID ARI - AudioSocket channel originated",
                        caller_channel_id=caller_channel_id,
                        audiosocket_channel_id=audiosocket_channel_id,
                    )
            else:
                raise RuntimeError("Failed to originate AudioSocket channel")
        except Exception as e:
            logger.error(
                "🎯 HYBRID ARI - AudioSocket channel originate failed",
                caller_channel_id=caller_channel_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def _handle_stasis_end(self, event: dict):
        """Handle StasisEnd event and clean up call resources."""
        try:
            channel = event.get("channel", {}) or {}
            channel_id = channel.get("id")
            if not channel_id:
                return
            logger.info("Stasis ended", channel_id=channel_id)
            await self._cleanup_call(channel_id)
        except Exception as exc:
            logger.error("Error handling StasisEnd", error=str(exc), exc_info=True)

    async def _handle_channel_destroyed(self, event: dict):
        """Clean up when a channel is destroyed."""
        try:
            channel = event.get("channel", {}) or {}
            channel_id = channel.get("id")
            if not channel_id:
                return
            logger.info("Channel destroyed", channel_id=channel_id)
            await self._cleanup_call(channel_id)
        except Exception as exc:
            logger.error("Error handling ChannelDestroyed", error=str(exc), exc_info=True)

    async def _handle_dtmf_received(self, event: dict):
        """Handle ChannelDtmfReceived events (informational logging for now)."""
        try:
            channel = event.get("channel", {}) or {}
            digit = event.get("digit")
            channel_id = channel.get("id")
            logger.info(
                "Channel DTMF received",
                channel_id=channel_id,
                digit=digit,
            )
        except Exception as exc:
            logger.error("Error handling ChannelDtmfReceived", error=str(exc), exc_info=True)

    async def _handle_channel_varset(self, event: dict):
        """Monitor ChannelVarset events for debugging configuration state."""
        try:
            channel = event.get("channel", {}) or {}
            variable = event.get("variable")
            value = event.get("value")
            channel_id = channel.get("id")
            logger.debug(
                "Channel variable set",
                channel_id=channel_id,
                variable=variable,
                value=value,
            )
        except Exception as exc:
            logger.error("Error handling ChannelVarset", error=str(exc), exc_info=True)

    async def _cleanup_call(self, channel_or_call_id: str) -> None:
        """Shared cleanup for StasisEnd/ChannelDestroyed paths."""
        try:
            # Resolve session by call_id first, then fallback to channel lookup.
            session = await self.session_store.get_by_call_id(channel_or_call_id)
            if not session:
                session = await self.session_store.get_by_channel_id(channel_or_call_id)
            if not session:
                logger.debug("No session found during cleanup", identifier=channel_or_call_id)
                return

            call_id = session.call_id
            logger.info("Cleaning up call", call_id=call_id)
            
            # Record call duration if we have start time
            try:
                import time
                if call_id in _call_start_times:
                    duration = time.time() - _call_start_times[call_id]
                    pipeline_name = getattr(session, 'pipeline_name', None) or "default"
                    provider_name = getattr(session, 'provider_name', None) or "unknown"
                    
                    _CALL_DURATION.labels(
                        call_id=call_id,
                        pipeline=pipeline_name,
                        provider=provider_name
                    ).observe(duration)
                    
                    # Clean up start time
                    del _call_start_times[call_id]
                    
                    logger.info("Recorded call duration", 
                               call_id=call_id,
                               duration_seconds=round(duration, 2),
                               pipeline=pipeline_name,
                               provider=provider_name)
            except Exception as e:
                logger.debug("Failed to record call duration", call_id=call_id, error=str(e))

            # Idempotent re-entrancy guard
            if getattr(session, "cleanup_completed", False):
                logger.debug("Cleanup already completed", call_id=call_id)
                return
            if getattr(session, "cleanup_in_progress", False):
                logger.debug("Cleanup already in progress", call_id=call_id)
                return
            try:
                session.cleanup_in_progress = True
                await self.session_store.upsert_call(session)
            except Exception:
                pass

            # Stop any active streaming playback.
            try:
                await self.streaming_playback_manager.stop_streaming_playback(call_id)
            except Exception:
                logger.debug("Streaming playback stop failed during cleanup", call_id=call_id, exc_info=True)

            # Stop the active provider session if one exists.
            try:
                provider_name = session.provider_name
                provider = self.providers.get(provider_name)
                if provider and hasattr(provider, "stop_session"):
                    await provider.stop_session()
            except Exception:
                logger.debug("Provider stop_session failed during cleanup", call_id=call_id, exc_info=True)

            # Check if call was transferred to dialplan (e.g., queue transfer)
            # If so, skip hanging up the caller channel
            transfer_active = getattr(session, 'transfer_active', False)
            
            # Tear down bridge.
            bridge_id = session.bridge_id
            if bridge_id:
                try:
                    await self.ari_client.destroy_bridge(bridge_id)
                    logger.info("Bridge destroyed", call_id=call_id, bridge_id=bridge_id)
                except Exception:
                    logger.debug("Bridge destroy failed", call_id=call_id, bridge_id=bridge_id, exc_info=True)

            # Hang up RTP and supporting channels (always)
            for channel_id in filter(None, [session.local_channel_id, session.external_media_id, session.audiosocket_channel_id]):
                try:
                    await self.ari_client.hangup_channel(channel_id)
                except Exception:
                    logger.debug("Hangup failed during cleanup", call_id=call_id, channel_id=channel_id, exc_info=True)
            
            # Hang up caller channel ONLY if not transferred
            if not transfer_active:
                try:
                    await self.ari_client.hangup_channel(session.caller_channel_id)
                except Exception:
                    logger.debug("Hangup failed during cleanup", call_id=call_id, channel_id=session.caller_channel_id, exc_info=True)
            else:
                logger.info("Skipping caller hangup - transferred to dialplan", call_id=call_id, transfer_target=getattr(session, 'transfer_target', 'unknown'))

            if getattr(self, 'rtp_server', None):
                try:
                    await self.rtp_server.cleanup_session(call_id)
                except Exception:
                    logger.debug("RTP session cleanup failed during call cleanup", call_id=call_id, exc_info=True)

            # Remove residual mappings so new calls don’t inherit.
            self.bridges.pop(session.caller_channel_id, None)
            if session.local_channel_id:
                self.pending_local_channels.pop(session.local_channel_id, None)
                self.local_channels.pop(session.caller_channel_id, None)
            if session.audiosocket_channel_id:
                self.pending_audiosocket_channels.pop(session.audiosocket_channel_id, None)
                self.audiosocket_channels.pop(session.caller_channel_id, None)
            if session.audiosocket_uuid:
                self.uuidext_to_channel.pop(session.audiosocket_uuid, None)

            # Cancel adapter pipeline runner, clear queue and forced flag
            try:
                task = self._pipeline_tasks.pop(call_id, None)
                if task:
                    task.cancel()
                q = self._pipeline_queues.pop(call_id, None)
                if q:
                    try:
                        q.put_nowait(None)
                    except Exception:
                        pass
                self._pipeline_forced.pop(call_id, None)
            except Exception:
                logger.debug("Pipeline cleanup failed", call_id=call_id, exc_info=True)

            # Clear detected codec preferences
            self.call_audio_preferences.pop(call_id, None)

            # Remove SSRC mapping for this call (if any)
            try:
                to_delete = [ssrc for ssrc, cid in self.ssrc_to_caller.items() if cid == call_id]
                for ssrc in to_delete:
                    self.ssrc_to_caller.pop(ssrc, None)
            except Exception:
                pass

            # Release pipeline components before dropping session.
            if getattr(self, "pipeline_orchestrator", None) and self.pipeline_orchestrator.enabled:
                try:
                    await self.pipeline_orchestrator.release_pipeline(call_id)
                except Exception:
                    logger.debug("Milestone7 pipeline release failed during cleanup", call_id=call_id, exc_info=True)

            # Auto-send email summary if enabled (before session is removed)
            try:
                # Auto-trigger email summary if configured and session has conversation history
                email_tool_config = self.config.tools.get('send_email_summary', {})
                if email_tool_config.get('enabled', False):
                    from src.tools.registry import tool_registry
                    email_tool = tool_registry.get('send_email_summary')
                    if email_tool:
                        # Verify session still exists (race condition with multiple cleanup calls)
                        check_session = await self.session_store.get_by_call_id(call_id)
                        if not check_session:
                            logger.debug(
                                "Skipping email summary - session already removed by concurrent cleanup",
                                call_id=call_id
                            )
                        else:
                            # Build execution context
                            from src.tools.context import ToolExecutionContext
                            context = ToolExecutionContext(
                                call_id=call_id,
                                caller_channel_id=session.caller_channel_id,
                                bridge_id=session.bridge_id,
                                session_store=self.session_store,
                                ari_client=self.ari_client,
                                config=self.config.dict()
                            )
                            # Execute synchronously to ensure session is available
                            # Email sending itself is still async (non-blocking)
                            await email_tool.execute({}, context)
                            logger.info("📧 Auto-triggered email summary", call_id=call_id)
            except RuntimeError as e:
                # Session not found is expected in concurrent cleanup scenarios
                if "Session not found" in str(e):
                    logger.debug(
                        "Email summary skipped - session already cleaned up",
                        call_id=call_id
                    )
                else:
                    logger.warning("Failed to auto-trigger email summary", call_id=call_id, error=str(e))
            except Exception as e:
                logger.warning("Failed to auto-trigger email summary", call_id=call_id, error=str(e), exc_info=True)

            # Send transcript emails if requested during call (complete conversation)
            try:
                if hasattr(session, 'transcript_emails') and session.transcript_emails:
                    transcript_tool_config = self.config.tools.get('request_transcript', {})
                    if transcript_tool_config.get('enabled', False):
                        from src.tools.registry import tool_registry
                        transcript_tool = tool_registry.get('request_transcript')
                        if transcript_tool:
                            # Send transcript to each requested email
                            for email_address in session.transcript_emails:
                                try:
                                    # Build execution context
                                    from src.tools.context import ToolExecutionContext
                                    context = ToolExecutionContext(
                                        call_id=call_id,
                                        caller_channel_id=session.caller_channel_id,
                                        bridge_id=session.bridge_id,
                                        session_store=self.session_store,
                                        ari_client=self.ari_client,
                                        config=self.config.dict()
                                    )
                                    
                                    # Get fresh session data with complete conversation
                                    current_session = await self.session_store.get_by_call_id(call_id)
                                    if current_session:
                                        # Prepare and send transcript email
                                        email_data = transcript_tool._prepare_email_data(
                                            email_address,
                                            current_session,
                                            transcript_tool_config,
                                            call_id
                                        )
                                        # Send asynchronously (don't block cleanup)
                                        asyncio.create_task(transcript_tool._send_transcript_async(email_data, call_id))
                                        logger.info(
                                            "📧 Sent end-of-call transcript",
                                            call_id=call_id,
                                            email=email_address
                                        )
                                except Exception as e:
                                    logger.warning(
                                        "Failed to send transcript to email",
                                        call_id=call_id,
                                        email=email_address,
                                        error=str(e)
                                    )
            except Exception as e:
                logger.warning("Failed to process transcript emails", call_id=call_id, error=str(e), exc_info=True)

            # Finally remove the session.
            await self.session_store.remove_call(call_id)

            try:
                self.audio_capture.close_call(call_id)
            except Exception:
                logger.debug("Audio capture cleanup failed", call_id=call_id, exc_info=True)

            if self.conversation_coordinator:
                await self.conversation_coordinator.unregister_call(call_id)
            
            # Clean up VAD manager state for this call
            if self.vad_manager:
                try:
                    await self.vad_manager.reset_call(call_id)
                    self.vad_manager.context_analyzer.cleanup_call(call_id)
                except Exception:
                    logger.debug("VAD cleanup failed during call cleanup", call_id=call_id, exc_info=True)
            
            # Clean up audio gating manager state for this call
            if self.audio_gating_manager:
                try:
                    await self.audio_gating_manager.cleanup_call(call_id)
                except Exception:
                    logger.debug("Audio gating cleanup failed during call cleanup", call_id=call_id, exc_info=True)

            try:
                # If the session still exists in store (rare race), mark completed; otherwise ignore
                sess2 = await self.session_store.get_by_call_id(call_id)
                if sess2:
                    sess2.cleanup_completed = True
                    sess2.cleanup_in_progress = False
                    await self.session_store.upsert_call(sess2)
            except Exception:
                pass

            # Reset per-call alignment warning state
            self._runtime_alignment_logged.discard(call_id)

            logger.info("Call cleanup completed", call_id=call_id)
        except Exception as exc:
            logger.error("Error cleaning up call", identifier=channel_or_call_id, error=str(exc), exc_info=True)
        finally:
            # Best-effort: if session still exists and we marked in-progress, clear it to unblock future attempts
            try:
                sess3 = await self.session_store.get_by_call_id(channel_or_call_id)
                if not sess3:
                    sess3 = await self.session_store.get_by_channel_id(channel_or_call_id)
                if sess3 and getattr(sess3, "cleanup_in_progress", False) and not getattr(sess3, "cleanup_completed", False):
                    sess3.cleanup_in_progress = False
                    await self.session_store.upsert_call(sess3)
            except Exception:
                pass

    async def _resolve_audio_profile(self, session: CallSession, channel_id: str) -> None:
        """Resolve TransportProfile and provider prefs from profiles/contexts.

        Precedence (provider): AI_PROVIDER (later) > contexts.*.provider > default_provider.
        """
        call_id = session.call_id
        # Read channel vars
        ai_profile = None
        ai_context = None
        try:
            resp = await self.ari_client.send_command(
                "GET",
                f"channels/{channel_id}/variable",
                params={"variable": "AI_AUDIO_PROFILE"},
            )
            if isinstance(resp, dict):
                ai_profile = (resp.get("value") or "").strip()
        except Exception:
            pass
        try:
            resp = await self.ari_client.send_command(
                "GET",
                f"channels/{channel_id}/variable",
                params={"variable": "AI_CONTEXT"},
            )
            if isinstance(resp, dict):
                ai_context = (resp.get("value") or "").strip()
        except Exception:
            pass

        cfg_profiles = getattr(self.config, "profiles", {}) or {}
        cfg_contexts = getattr(self.config, "contexts", {}) or {}
        # Extract default profile name
        default_profile_name = None
        try:
            dp = cfg_profiles.get("default")
            if isinstance(dp, str) and dp:
                default_profile_name = dp
        except Exception:
            default_profile_name = None
        # Build profile map excluding the 'default' selector key
        profile_map = {k: v for (k, v) in cfg_profiles.items() if isinstance(v, dict)}

        # Resolve profile name from channel var, then context mapping, else default
        context_block = cfg_contexts.get(ai_context) if ai_context else None
        ctx_profile = None
        try:
            if isinstance(context_block, dict):
                ctx_profile = context_block.get("profile")
        except Exception:
            ctx_profile = None
        selected_profile_name = ai_profile or ctx_profile or default_profile_name
        profile_obj = profile_map.get(selected_profile_name) if selected_profile_name else None
        if profile_obj is None and default_profile_name:
            profile_obj = profile_map.get(default_profile_name)

        # Extract transport_out and provider prefs
        transport_out = (profile_obj or {}).get("transport_out", {}) if isinstance(profile_obj, dict) else {}
        prov_pref = (profile_obj or {}).get("provider_pref", {}) if isinstance(profile_obj, dict) else {}
        chunk_ms = None
        idle_cutoff_ms = None
        try:
            v = prov_pref.get("preferred_chunk_ms")
            if v is not None:
                chunk_ms = int(v)
        except Exception:
            pass
        try:
            v = (profile_obj or {}).get("idle_cutoff_ms")
            if v is not None:
                idle_cutoff_ms = int(v)
        except Exception:
            pass

        # Determine transport encoding/rate from profile (fallback to existing)
        enc = self._canonicalize_encoding(transport_out.get("encoding")) or session.transport_profile.format
        try:
            rate = int(transport_out.get("sample_rate_hz") or 0)
        except Exception:
            rate = 0
        if rate <= 0:
            rate = session.transport_profile.sample_rate

        # Apply transport settings with 'config' source (won't override dialplan/detected)
        try:
            await self._update_transport_profile(session, fmt=enc, sample_rate=rate, source="config")
        except Exception:
            logger.debug("Transport profile update from profile failed", call_id=call_id, exc_info=True)

        # Apply context-level provider override (Option A), lower precedence than AI_PROVIDER.
        provider_origin = None
        try:
            ctx_provider = None
            if isinstance(context_block, dict):
                ctx_provider = (context_block.get("provider") or "").strip()
            if ctx_provider:
                aliases = {"openai": "openai_realtime", "deepgram_agent": "deepgram"}
                resolved = aliases.get(ctx_provider, ctx_provider)
                if resolved in self.providers and session.provider_name != resolved:
                    prev = session.provider_name
                    session.provider_name = resolved
                    await self._save_session(session)
                    provider_origin = "context"
                    logger.info("Context provider override applied", call_id=call_id, context=ai_context, previous_provider=prev, provider=resolved)
        except Exception:
            logger.debug("Context provider override failed", call_id=call_id, exc_info=True)

        # Wire streaming manager parameters (global fields; per-call override is a future improvement)
        spm = getattr(self, "streaming_playback_manager", None)
        if spm is not None:
            # CRITICAL: Do NOT override audiosocket_format from transport profile.
            # AudioSocket wire format must always match config.audiosocket.format (set at engine init),
            # NOT the caller's SIP codec. Caller codec applies only to provider transcoding.
            # Bug fix: removed lines that set spm.audiosocket_format = enc
            try:
                if rate and rate > 0:
                    spm.sample_rate = int(rate)
            except Exception:
                pass
            try:
                if chunk_ms and int(chunk_ms) > 0:
                    spm.chunk_size_ms = int(chunk_ms)
            except Exception:
                pass
            try:
                if idle_cutoff_ms and int(idle_cutoff_ms) > 0:
                    spm.idle_cutoff_ms = int(idle_cutoff_ms)
            except Exception:
                pass

        # Emit one-shot profile resolution card
        try:
            self._emit_profile_resolution_card(
                session.call_id,
                session,
                profile_name=selected_profile_name,
                context_name=ai_context,
                transport_encoding=enc,
                transport_sample_rate=rate,
                chunk_ms=chunk_ms,
                idle_cutoff_ms=idle_cutoff_ms,
                provider_origin=provider_origin or ("profile" if ai_profile else ("context" if ai_context else None)),
            )
        except Exception:
            logger.debug("Audio Profile Resolution card logging failed", call_id=call_id, exc_info=True)

    def _emit_profile_resolution_card(
        self,
        call_id: Optional[str],
        session: Optional[CallSession],
        *,
        profile_name: Optional[str],
        context_name: Optional[str],
        transport_encoding: Optional[Any],
        transport_sample_rate: Optional[Any],
        chunk_ms: Optional[Any],
        idle_cutoff_ms: Optional[Any],
        provider_origin: Optional[str],
    ) -> None:
        if not call_id or call_id in self._profile_card_logged:
            return
        def _ir(v):
            try:
                return int(v) if v is not None else None
            except Exception:
                return None
        payload = {
            "call_id": call_id,
            "log_event": "Audio Profile Resolution",
            "profile": profile_name,
            "context": context_name,
            "provider": getattr(session, "provider_name", None) if session else None,
            "provider_origin": provider_origin,
            "transport_encoding": self._canonicalize_encoding(transport_encoding) or None,
            "transport_sample_rate_hz": _ir(transport_sample_rate),
            "chunk_size_ms": _ir(chunk_ms),
            "idle_cutoff_ms": _ir(idle_cutoff_ms),
        }
        try:
            logger.info("AudioProfileResolution", **{k: v for k, v in payload.items() if v is not None})
            self._profile_card_logged.add(call_id)
        except Exception:
            logger.debug("Profile resolution card logging failed", call_id=call_id, exc_info=True)

    async def _audiosocket_handle_uuid(self, conn_id: str, uuid_str: str) -> bool:
        """Bind inbound AudioSocket connection to the caller channel via UUID."""
        try:
            caller_channel_id = self.uuidext_to_channel.get(uuid_str)

            # Handle race where the TCP client connects before we finish recording
            # the UUID mapping. Give the originate path a brief window to catch up.
            if not caller_channel_id:
                for attempt in range(3):
                    await asyncio.sleep(0.05)
                    caller_channel_id = self.uuidext_to_channel.get(uuid_str)
                    if caller_channel_id:
                        logger.debug(
                            "AudioSocket UUID resolved after retry",
                            conn_id=conn_id,
                            uuid=uuid_str,
                            attempt=attempt + 1,
                        )
                        break

            if not caller_channel_id:
                logger.warning(
                    "AudioSocket UUID not recognized",
                    conn_id=conn_id,
                    uuid=uuid_str,
                )
                return False

            # Track mappings
            self.conn_to_channel[conn_id] = caller_channel_id
            self.channel_to_conn[caller_channel_id] = conn_id
            self.channel_to_conns.setdefault(caller_channel_id, set()).add(conn_id)
            if caller_channel_id not in self.audiosocket_primary_conn:
                self.audiosocket_primary_conn[caller_channel_id] = conn_id

            # Update session
            session = await self.session_store.get_by_call_id(caller_channel_id)
            if session:
                session.audiosocket_uuid = uuid_str
                # Record current AudioSocket connection for streaming playback
                try:
                    session.audiosocket_conn_id = conn_id
                except Exception:
                    pass
                session.status = "audiosocket_bound"
                await self._save_session(session)

            logger.info(
                "AudioSocket connection bound to caller",
                conn_id=conn_id,
                uuid=uuid_str,
                caller_channel_id=caller_channel_id,
            )
            return True
        except Exception as exc:
            logger.error("Error binding AudioSocket UUID", conn_id=conn_id, uuid=uuid_str, error=str(exc), exc_info=True)
            return False

    async def _audiosocket_handle_audio(self, conn_id: str, audio_bytes: bytes) -> None:
        """Forward inbound AudioSocket audio to the active provider for the bound call."""
        # Track every frame for diagnostics
        if not hasattr(self, '_audiosocket_frame_count'):
            self._audiosocket_frame_count = {}
        
        try:
            caller_channel_id = self.conn_to_channel.get(conn_id)
            if not caller_channel_id and self.audio_socket_server:
                # Fallback: resolve via server's UUID registry
                try:
                    uuid_str = self.audio_socket_server.get_uuid_for_conn(conn_id)
                    if uuid_str:
                        caller_channel_id = self.uuidext_to_channel.get(uuid_str)
                        if caller_channel_id:
                            self.conn_to_channel[conn_id] = caller_channel_id
                except Exception:
                    pass

            if not caller_channel_id:
                logger.debug("AudioSocket audio received for unknown connection", conn_id=conn_id, bytes=len(audio_bytes))
                return

            # Track frame count per call
            self._audiosocket_frame_count[caller_channel_id] = self._audiosocket_frame_count.get(caller_channel_id, 0) + 1
            frame_num = self._audiosocket_frame_count[caller_channel_id]
            
            # Log every 10th frame + first 5 frames
            if frame_num <= 5 or frame_num % 10 == 0:
                logger.info(
                    "🎤 AUDIOSOCKET RX - Frame received",
                    call_id=caller_channel_id,
                    frame_num=frame_num,
                    frame_bytes=len(audio_bytes),
                    conn_id=conn_id,
                )

            session = await self.session_store.get_by_call_id(caller_channel_id)
            if not session:
                logger.debug("No session for caller; dropping AudioSocket audio", conn_id=conn_id, caller_channel_id=caller_channel_id)
                return

            diagnostics_flags = session.audio_diagnostics
            if "inbound_first_frame" not in diagnostics_flags:
                fmt, rate = self._infer_transport_from_frame(len(audio_bytes))
                await self._update_transport_profile(session, fmt=fmt, sample_rate=rate, source="audiosocket")
                diagnostics_flags["inbound_first_frame"] = True

            # Per-call RX bytes
            try:
                _STREAM_RX_BYTES.labels(caller_channel_id).inc(len(audio_bytes))
            except Exception:
                pass

            # First-frame diagnostics probe (no mutation): log RMS for format verification
            try:
                vad_state = session.vad_state
            except Exception:
                vad_state = session.vad_state = {}
            if not vad_state.get('format_probe_done'):
                try:
                    try:
                        as_fmt = (getattr(self.config, 'audiosocket', None).format or 'ulaw').lower()
                    except Exception:
                        as_fmt = 'ulaw'
                    if as_fmt in ('slin16', 'linear16', 'pcm16'):
                        rms_native = audioop.rms(audio_bytes, 2)
                        try:
                            swapped = audioop.byteswap(audio_bytes, 2)
                            rms_swapped = audioop.rms(swapped, 2)
                        except Exception:
                            rms_swapped = 0
                        logger.info(
                            "AudioSocket frame probe",
                            call_id=caller_channel_id,
                            audiosocket_format=as_fmt,
                            frame_bytes=len(audio_bytes),
                            rms_native=rms_native,
                            rms_swapped=rms_swapped,
                        )
                        # Determine if inbound PCM16 appears byte-swapped (big-endian on wire)
                        try:
                            frame_bytes = len(audio_bytes)
                            # Conservative rule: only flag swap when swapped energy is clearly higher
                            swap_flag = (
                                frame_bytes >= 640 and  # 20ms @ 16k PCM
                                rms_swapped >= 2048 and
                                rms_swapped >= 16 * max(1, rms_native)
                            )
                            vad_state['pcm16_inbound_swap'] = bool(swap_flag)
                            if swap_flag:
                                logger.warning(
                                    "Inbound slin16 appears byte-swapped; will normalize to PCM16-LE for processing",
                                    call_id=caller_channel_id,
                                    rms_native=rms_native,
                                    rms_swapped=rms_swapped,
                                )
                        except Exception:
                            pass
                    else:
                        try:
                            pcm = audioop.ulaw2lin(audio_bytes, 2)
                            rms_pcm = audioop.rms(pcm, 2)
                        except Exception:
                            rms_pcm = 0
                        logger.info(
                            "AudioSocket frame probe",
                            call_id=caller_channel_id,
                            audiosocket_format=as_fmt,
                            frame_bytes=len(audio_bytes),
                            rms_pcm8k=rms_pcm,
                        )
                        # μ-law path: no PCM16 swap needed
                        vad_state['pcm16_inbound_swap'] = False
                    vad_state['format_probe_done'] = True
                except Exception:
                    pass

            try:
                swap_needed_flag = bool(session.vad_state.get('pcm16_inbound_swap', False))
            except Exception:
                swap_needed_flag = False
            try:
                # CRITICAL: AudioSocket format is authoritative for AudioSocket transport
                # For RTP, use transport profile (negotiated codec)
                if self.config.audio_transport == "audiosocket":
                    # Use AudioSocket's actual format (from YAML)
                    profile_fmt = getattr(self.config.audiosocket, "format", "slin16")
                    # Get sample rate from AudioSocket config or infer from format
                    profile_rate = getattr(self.config.audiosocket, "sample_rate", None)
                    if not profile_rate:
                        # Infer rate from format: slin=8kHz, slin16=16kHz
                        canonical_fmt = self._canonicalize_encoding(profile_fmt)
                        if canonical_fmt == "slin":
                            profile_rate = 8000
                        elif canonical_fmt == "slin16":
                            profile_rate = 16000
                        else:
                            profile_rate = getattr(self.config.streaming, "sample_rate", 8000)
                else:
                    # For RTP: use transport profile (negotiated codec)
                    profile_fmt = session.transport_profile.format or "ulaw"
                    profile_rate = session.transport_profile.sample_rate or 8000
            except Exception:
                # Safe fallback based on transport type
                if self.config.audio_transport == "audiosocket":
                    profile_fmt = "slin16"
                    profile_rate = 16000
                else:
                    profile_fmt = "ulaw"
                    profile_rate = 8000
            pcm_bytes, pcm_rate = self._wire_to_pcm16(audio_bytes, profile_fmt, swap_needed_flag, profile_rate)
            # Remove DC bias ONLY (disable IIR DC-block filter - causes audio degradation)
            try:
                if pcm_bytes:
                    try:
                        mean = int(audioop.avg(pcm_bytes, 2))
                    except Exception:
                        mean = 0
                    if mean:
                        try:
                            pcm_bytes = audioop.bias(pcm_bytes, 2, -mean)
                        except Exception:
                            pass
                    # DC-block IIR filter DISABLED - was causing progressive audio level collapse
                    # Symptoms: Audio started strong (RMS 4000) but degraded to near-silence (RMS 16)
                    # Root cause: Stateful filter accumulated error, over-attenuated speech
                    # Keep simple DC offset removal (audioop.bias) above, skip IIR filter
            except Exception:
                logger.debug("Inbound DC conditioning failed", call_id=caller_channel_id, exc_info=True)
            try:
                if pcm_bytes:
                    self._update_audio_diagnostics(session, "transport_in", pcm_bytes, "slin16", pcm_rate)
                    self.audio_capture.append_pcm16(session.call_id, "caller_inbound", pcm_bytes, pcm_rate)
            except Exception:
                logger.debug("Inbound diagnostics update failed", call_id=caller_channel_id, exc_info=True)

            # CRITICAL FIX: Check for pipeline mode FIRST before routing to monolithic providers
            if self._pipeline_forced.get(caller_channel_id):
                # AAVA-28: Check gating to prevent agent from hearing its own TTS output
                if not session.audio_capture_enabled:
                    # Drop audio during TTS playback (gating active)
                    return
                
                q = self._pipeline_queues.get(caller_channel_id)
                if q:
                    try:
                        pcm16 = pcm_bytes
                        if pcm16 and pcm_rate != 16000:
                            try:
                                state = self._resample_state_pipeline16k.get(caller_channel_id)
                                pcm16, state = audioop.ratecv(pcm16, 2, 1, pcm_rate, 16000, state)
                                self._resample_state_pipeline16k[caller_channel_id] = state
                            except Exception:
                                pcm16 = pcm_bytes
                        if pcm16:
                            q.put_nowait(pcm16)
                        return
                    except asyncio.QueueFull:
                        logger.debug("Pipeline queue full; dropping AudioSocket frame", call_id=caller_channel_id)
                        return

            # Unconditional continuous-input forward: Deepgram/OpenAI Realtime expect raw audio flow
            # NOTE: Only applies to monolithic providers, not pipelines (handled above)
            try:
                provider_name = getattr(session, 'provider_name', None) or self.config.default_provider
                provider = self.providers.get(provider_name)
            except Exception:
                provider = None
            continuous_input = False
            try:
                # Use provider capabilities instead of hardcoded names
                capabilities = None
                if provider and hasattr(provider, 'get_capabilities'):
                    try:
                        capabilities = provider.get_capabilities()
                    except Exception:
                        pass
                
                if capabilities and capabilities.requires_continuous_audio:
                    continuous_input = True
                else:
                    # Fallback for legacy providers without capabilities
                    pcfg = getattr(provider, 'config', None)
                    if isinstance(pcfg, dict):
                        continuous_input = bool(pcfg.get('continuous_input', False))
                    else:
                        continuous_input = bool(getattr(pcfg, 'continuous_input', False))
            except Exception:
                continuous_input = False
            
            if continuous_input and provider and hasattr(provider, 'send_audio'):
                # CRITICAL FIX: Google Live needs gating, but OpenAI/Deepgram don't
                # - Google Live: Bidirectional audio, NO server-side echo cancellation → NEEDS gating
                # - OpenAI Realtime: Server-side AEC → gating harmful
                # - Deepgram: Text-based output → no echo risk
                needs_gating = provider_name == "google_live"
                
                if needs_gating and not session.audio_capture_enabled:
                    # CRITICAL: Google Live requires continuous audio stream (like WebRTC)
                    # Send SILENCE frames instead of blocking to maintain stream continuity
                    # This prevents echo while keeping VAD healthy
                    logger.debug(
                        "🔇 GATING ACTIVE - Sending silence frame for Google Live (TTS playing)",
                        call_id=caller_channel_id,
                        audio_capture_enabled=session.audio_capture_enabled,
                    )
                    # Replace audio with silence (zero-filled PCM16)
                    pcm_bytes = b'\x00' * len(pcm_bytes)
                
                # Forward to provider
                logger.info(
                    "📤 CONTINUOUS INPUT - Forwarding frame to provider",
                    call_id=caller_channel_id,
                    provider=provider_name,
                    frame_bytes=len(audio_bytes),
                    pcm_bytes=len(pcm_bytes),
                    gating_active=needs_gating and not session.audio_capture_enabled,
                    is_silence=needs_gating and not session.audio_capture_enabled,
                )
                try:
                    self._update_audio_diagnostics(session, "provider_in", pcm_bytes, "slin16", pcm_rate)
                except Exception:
                    logger.debug("Provider input diagnostics update failed (unconditional)", call_id=caller_channel_id, exc_info=True)
                try:
                    prov_payload, prov_enc, prov_rate = self._encode_for_provider(
                        session.call_id,
                        provider_name,
                        provider,
                        pcm_bytes,
                        pcm_rate,
                    )
                    logger.info(
                        "📤 CONTINUOUS INPUT - Encoded for provider",
                        call_id=caller_channel_id,
                        provider=provider_name,
                        prov_payload_bytes=len(prov_payload),
                        prov_enc=prov_enc,
                        prov_rate=prov_rate,
                    )
                    try:
                        self.audio_capture.append_encoded(
                            session.call_id,
                            "caller_to_provider",
                            prov_payload,
                            prov_enc,
                            prov_rate,
                        )
                    except Exception:
                        logger.debug("Provider input capture failed (unconditional)", call_id=session.call_id, exc_info=True)
                    
                    # CRITICAL: Pass sample_rate and encoding to prevent double resampling
                    # Google Live needs to know audio is already at provider_rate to skip resampling
                    try:
                        await provider.send_audio(prov_payload, prov_rate, prov_enc)
                        logger.info(
                            "✅ CONTINUOUS INPUT - Frame sent to provider successfully",
                            call_id=caller_channel_id,
                            provider=provider_name,
                        )
                    except TypeError:
                        # Fallback for providers with old signature (audio_chunk only)
                        await provider.send_audio(prov_payload)
                        logger.info(
                            "✅ CONTINUOUS INPUT - Frame sent to provider (legacy signature)",
                            call_id=caller_channel_id,
                            provider=provider_name,
                        )
                except Exception as e:
                    logger.error(
                        "❌ CONTINUOUS INPUT - Provider forward error",
                        call_id=caller_channel_id,
                        provider=provider_name,
                        error=str(e),
                        exc_info=True,
                    )
                return
            else:
                logger.info(
                    "⚠️ CONTINUOUS INPUT - Block skipped",
                    call_id=caller_channel_id,
                    continuous_input=continuous_input,
                    provider_found=provider is not None,
                    has_send_audio=hasattr(provider, 'send_audio') if provider else False,
                    provider_name=provider_name,
                )

            # Post-TTS end protection: drop inbound briefly after gating clears to avoid agent echo re-capture
            try:
                cfg = getattr(self.config, 'barge_in', None)
                post_guard_ms = int(getattr(cfg, 'post_tts_end_protection_ms', 0)) if cfg else 0
            except Exception:
                post_guard_ms = 0
            if post_guard_ms and getattr(session, 'tts_ended_ts', 0.0) and session.audio_capture_enabled:
                try:
                    elapsed_ms = int((time.time() - float(session.tts_ended_ts)) * 1000)
                except Exception:
                    elapsed_ms = post_guard_ms
                if elapsed_ms < post_guard_ms:
                    logger.debug(
                        "Dropping inbound during post-TTS protection window",
                        call_id=caller_channel_id,
                        elapsed_ms=elapsed_ms,
                        protect_ms=post_guard_ms,
                    )
                    return

            vad_result: Optional[VADResult] = None
            if self.vad_manager:
                try:
                    vad_result = await self._run_enhanced_vad(session, audio_bytes)
                except Exception:
                    logger.debug(
                        "Enhanced VAD processing error",
                        call_id=caller_channel_id,
                        exc_info=True,
                    )

            # Self-echo mitigation and barge-in/continuous-input handling during TTS playback
            if hasattr(session, 'audio_capture_enabled') and not session.audio_capture_enabled:
                cfg = getattr(self.config, 'barge_in', None)
                
                # AAVA-28: Pipelines now respect gating - no special bypass during TTS
                # Drop audio for pipelines during TTS playback (handled by earlier gating check)
                if self._pipeline_forced.get(caller_channel_id):
                    # Audio already dropped by gating check above (line 2059)
                    return
                
                # Determine provider and continuous-input capability FIRST to allow forwarding during greeting guard
                try:
                    provider_name = getattr(session, 'provider_name', None) or self.config.default_provider
                    provider = self.providers.get(provider_name)
                except Exception:
                    provider = None
                continuous_input = False
                try:
                    # CRITICAL: Use provider capabilities to determine continuous audio requirement
                    # Providers with native VAD (full agents) need continuous audio stream
                    # Pipeline providers use engine-side VAD (gated audio)
                    capabilities = None
                    if provider and hasattr(provider, 'get_capabilities'):
                        try:
                            capabilities = provider.get_capabilities()
                        except Exception:
                            pass
                    
                    if capabilities and capabilities.requires_continuous_audio:
                        # Provider declares it needs continuous audio (e.g., for native VAD)
                        continuous_input = True
                    else:
                        # Fallback: check config for legacy providers
                        pcfg = getattr(provider, 'config', None)
                        if isinstance(pcfg, dict):
                            continuous_input = bool(pcfg.get('continuous_input', False))
                        else:
                            continuous_input = bool(getattr(pcfg, 'continuous_input', False))
                except Exception:
                    continuous_input = False
                # If provider supports continuous input, forward provider-encoded PCM immediately (during TTS guard)
                if continuous_input and provider and hasattr(provider, 'send_audio'):
                    try:
                        # Diagnostics on the PCM payload we are about to send
                        self._update_audio_diagnostics(session, "provider_in", pcm_bytes, "slin16", pcm_rate)
                    except Exception:
                        logger.debug("Provider input diagnostics update failed (continuous-input)", call_id=caller_channel_id, exc_info=True)
                    try:
                        prov_payload, prov_enc, prov_rate = self._encode_for_provider(
                            session.call_id,
                            provider_name,
                            provider,
                            pcm_bytes,
                            pcm_rate,
                        )
                        try:
                            self.audio_capture.append_encoded(
                                session.call_id,
                                "caller_to_provider",
                                prov_payload,
                                prov_enc,
                                prov_rate,
                            )
                        except Exception:
                            logger.debug("Provider input capture failed (continuous-input)", call_id=session.call_id, exc_info=True)
                        # CRITICAL: Pass encoding and sample_rate to provider
                        # Google Live needs these to correctly interpret audio format
                        # Other providers with single-param signature will ignore extras
                        logger.debug(
                            "Sending audio to provider",
                            call_id=session.call_id,
                            provider=provider_name,
                            encoding=prov_enc,
                            sample_rate=prov_rate,
                            payload_bytes=len(prov_payload),
                        )
                        try:
                            await provider.send_audio(prov_payload, prov_rate, prov_enc)
                        except TypeError as e:
                            logger.warning(
                                "Provider send_audio TypeError - falling back to old signature",
                                call_id=session.call_id,
                                provider=provider_name,
                                error=str(e),
                            )
                            # Fallback for providers with old signature (audio_chunk only)
                            await provider.send_audio(prov_payload)
                    except Exception:
                        logger.debug("Provider continuous-input forward error", call_id=caller_channel_id, exc_info=True)
                    return
                # Protection window from TTS start to avoid initial self-echo (applies when not using continuous-input)
                now = time.time()
                tts_elapsed_ms = 0
                try:
                    if getattr(session, 'tts_started_ts', 0.0) > 0:
                        tts_elapsed_ms = int((now - session.tts_started_ts) * 1000)
                except Exception:
                    tts_elapsed_ms = 0
                initial_protect = int(getattr(cfg, 'initial_protection_ms', 200)) if cfg else 200
                
                # CRITICAL FIX #3: Extended protection for OpenAI Realtime (echo prevention)
                # OpenAI's VAD is highly sensitive and detects agent's own audio as "user speech"
                # This causes 20+ false speech_started events, creating response cancellation loop
                # 5 seconds ensures complete greeting plays before accepting any input
                # Other providers unaffected: Deepgram uses continuous_input path (line 2204 early return)
                # CRITICAL: Only apply if TTS has actually started (not during pre-TTS initialization)
                try:
                    if provider_name == "openai_realtime" and getattr(session, 'tts_started_ts', 0.0) > 0.0:
                        initial_protect = 5000  # 5 seconds to prevent echo feedback loop
                        logger.debug(
                            "Extended TTS protection for OpenAI Realtime (echo prevention)",
                            call_id=caller_channel_id,
                            protect_ms=initial_protect,
                            tts_started_ts=session.tts_started_ts
                        )
                except Exception:
                    pass
                
                # Greeting-specific extra protection
                try:
                    if getattr(session, 'conversation_state', None) == 'greeting' and cfg:
                        greet_ms = int(getattr(cfg, 'greeting_protection_ms', 0))
                        if greet_ms > initial_protect:
                            initial_protect = greet_ms
                except Exception:
                    pass
                if tts_elapsed_ms < initial_protect:
                    logger.debug("Dropping inbound during initial TTS protection window",
                                 conn_id=conn_id, caller_channel_id=caller_channel_id,
                                 tts_elapsed_ms=tts_elapsed_ms, protect_ms=initial_protect)
                    return
                # If barge-in disabled and no continuous-input path, drop
                if not cfg or not getattr(cfg, 'enabled', True):
                    logger.debug("Dropping inbound AudioSocket audio during TTS playback (barge-in disabled)",
                                 conn_id=conn_id, caller_channel_id=caller_channel_id, bytes=len(audio_bytes))
                    return
                # Barge-in detection: accumulate candidate window based on multi-criteria (VAD + energy)
                threshold = int(getattr(cfg, 'energy_threshold', 1000))
                frame_ms = 20
                energy = 0
                confidence = 0.0
                vad_speech = False
                webrtc_positive = False

                if vad_result:
                    frame_ms = max(vad_result.frame_duration_ms, 1)
                    energy = vad_result.energy_level
                    confidence = vad_result.confidence
                    vad_speech = vad_result.is_speech
                    webrtc_positive = vad_result.webrtc_result
                    try:
                        session.vad_state['last_vad_result'] = {
                            'is_speech': vad_speech,
                            'confidence': confidence,
                            'energy': energy,
                            'webrtc': webrtc_positive,
                        }
                    except Exception:
                        pass
                else:
                    try:
                        pcm16_frame = pcm_bytes
                        energy = audioop.rms(pcm16_frame, 2) if pcm16_frame else 0
                    except Exception:
                        energy = 0

                criteria_met = 0
                if vad_speech:
                    criteria_met += 1
                if energy >= threshold:
                    criteria_met += 1
                if vad_result and confidence >= getattr(self.vad_manager, 'confidence_threshold', 0.6):
                    criteria_met += 1
                if webrtc_positive:
                    criteria_met += 1

                if vad_result:
                    if criteria_met >= 2:
                        if int(getattr(session, 'barge_in_candidate_ms', 0)) == 0:
                            try:
                                session.barge_start_ts = now
                            except Exception:
                                session.barge_start_ts = 0.0
                        session.barge_in_candidate_ms = int(getattr(session, 'barge_in_candidate_ms', 0)) + frame_ms
                    else:
                        session.barge_in_candidate_ms = 0
                else:
                    if energy >= threshold:
                        if int(getattr(session, 'barge_in_candidate_ms', 0)) == 0:
                            try:
                                session.barge_start_ts = now
                            except Exception:
                                session.barge_start_ts = 0.0
                        session.barge_in_candidate_ms = int(getattr(session, 'barge_in_candidate_ms', 0)) + frame_ms
                    else:
                        session.barge_in_candidate_ms = 0

                # Cooldown check to avoid flapping
                cooldown_ms = int(getattr(cfg, 'cooldown_ms', 500))
                last_barge_in_ts = float(getattr(session, 'last_barge_in_ts', 0.0) or 0.0)
                in_cooldown = (now - last_barge_in_ts) * 1000 < cooldown_ms if last_barge_in_ts else False

                min_ms = int(getattr(cfg, 'min_ms', 250))
                should_trigger = not in_cooldown and session.barge_in_candidate_ms >= min_ms
                
                # CRITICAL FIX #2: Skip engine-level barge-in for OpenAI Realtime
                # OpenAI Realtime handles turn-taking/interruption internally via its own VAD
                # Engine-level barge-in causes double-cancellation (both systems fighting)
                provider_name = getattr(session, 'provider_name', None)
                if should_trigger and provider_name == 'openai_realtime':
                    logger.debug(
                        "Local barge-in detected for OpenAI Realtime - sending cancellation to server",
                        call_id=caller_channel_id,
                        energy=energy,
                        criteria_met=criteria_met,
                    )
                    # Notify OpenAI to cancel any in-progress response generation
                    try:
                        provider = self.providers.get('openai_realtime')
                        if provider and hasattr(provider, 'cancel_response'):
                            await provider.cancel_response()
                    except Exception:
                        logger.debug("Failed to cancel OpenAI response", call_id=caller_channel_id, exc_info=True)
                    
                    # Reset candidate counter but don't trigger local playback stops
                    session.barge_in_candidate_ms = 0
                    # Continue forwarding audio to provider (OpenAI will handle the rest)
                    should_trigger = False

                if should_trigger:
                    # Trigger barge-in: stop active playback(s), clear gating, and continue forwarding audio
                    try:
                        playback_ids = await self.session_store.list_playbacks_for_call(caller_channel_id)
                        for pid in playback_ids:
                            try:
                                await self.ari_client.stop_playback(pid)
                            except Exception:
                                logger.debug("Playback stop error during barge-in", playback_id=pid, exc_info=True)

                        # Clear all active gating tokens
                        tokens = list(getattr(session, 'tts_tokens', set()) or [])
                        for token in tokens:
                            try:
                                if self.conversation_coordinator:
                                    await self.conversation_coordinator.on_tts_end(caller_channel_id, token, reason="barge-in")
                            except Exception:
                                logger.debug("Failed to clear gating token during barge-in", token=token, exc_info=True)

                        session.barge_in_candidate_ms = 0
                        session.last_barge_in_ts = now
                        # Observe reaction latency if we captured onset
                        try:
                            if float(getattr(session, 'barge_start_ts', 0.0) or 0.0) > 0.0:
                                reaction_s = max(0.0, now - float(session.barge_start_ts))
                                _BARGE_REACTION_SECONDS.labels(caller_channel_id).observe(reaction_s)
                                session.barge_start_ts = 0.0
                        except Exception:
                            pass
                        await self._save_session(session)
                        
                        # Notify VAD manager of barge-in event for adaptive learning
                        if self.vad_manager and vad_result:
                            self.vad_manager.notify_call_event(
                                caller_channel_id, 
                                "barge_in", 
                                {"confidence": confidence, "energy": energy, "criteria_met": criteria_met}
                            )
                        
                        logger.info(
                            "🎧 BARGE-IN triggered",
                            call_id=caller_channel_id,
                            energy=energy,
                            criteria_met=criteria_met,
                            confidence=confidence,
                            vad_speech=vad_speech,
                            webrtc=webrtc_positive,
                        )
                    except Exception:
                        logger.error("Error triggering barge-in", call_id=caller_channel_id, exc_info=True)
                    # After barge-in, fall through to forward this frame to provider
                else:
                    # Not yet triggered; drop inbound frame while TTS is active
                    if energy > 0 and self.conversation_coordinator:
                        try:
                            self.conversation_coordinator.note_audio_during_tts(caller_channel_id)
                        except Exception:
                            pass
                    logger.debug(
                        "Dropping inbound during TTS",
                        call_id=caller_channel_id,
                        candidate_ms=session.barge_in_candidate_ms,
                        energy=energy,
                        criteria_met=criteria_met,
                        confidence=confidence,
                    )
                    return

            # If pipeline execution is forced, route to pipeline queue after converting to PCM16 @ 16 kHz
            if self._pipeline_forced.get(caller_channel_id):
                q = self._pipeline_queues.get(caller_channel_id)
                if q:
                    try:
                        pcm16 = pcm_bytes
                        if pcm16 and pcm_rate != 16000:
                            try:
                                state = self._resample_state_pipeline16k.get(caller_channel_id)
                                pcm16, state = audioop.ratecv(pcm16, 2, 1, pcm_rate, 16000, state)
                                self._resample_state_pipeline16k[caller_channel_id] = state
                            except Exception:
                                pcm16 = pcm_bytes
                        if pcm16:
                            q.put_nowait(pcm16)
                        return
                    except asyncio.QueueFull:
                        logger.debug("Pipeline queue full; dropping AudioSocket frame", call_id=caller_channel_id)
                        return

            # Enhanced VAD Audio Filtering with continuous delivery
            forward_original_audio = True
            pcm_payload = pcm_bytes
            payload_rate = pcm_rate

            # Pre-guard RMS for instrumentation
            try:
                pre_guard_rms = audioop.rms(pcm_bytes, 2) if pcm_bytes else 0
            except Exception:
                pre_guard_rms = 0

            if vad_result:
                now = time.time()
                state = session.vad_state

                # Initialize VAD state if needed
                if 'vad_start_time' not in state:
                    state['vad_start_time'] = now
                    state['last_speech_time'] = now
                    state['frames_since_speech'] = 0

                frames_since_speech = int(state.get('frames_since_speech', 0))
                call_duration = now - float(state.get('vad_start_time', now))

                if call_duration >= 2.0:
                    forward_original_audio = (
                        vad_result.is_speech
                        or vad_result.confidence > 0.3
                        or frames_since_speech < 25
                        or self._should_use_vad_fallback(session)
                    )

                if vad_result.is_speech:
                    state['last_speech_time'] = now
                    state['frames_since_speech'] = 0
                else:
                    state['frames_since_speech'] = frames_since_speech + 1

                if not forward_original_audio:
                    # During greeting, avoid zeroing frames; allow audio to pass to provider
                    if getattr(session, 'conversation_state', None) == 'greeting':
                        pcm_payload = pcm_bytes
                        forward_original_audio = True
                    else:
                        silence_len = len(pcm_bytes) if pcm_bytes else len(audio_bytes) * 2
                        pcm_payload = b"\x00" * silence_len
                        logger.debug(
                            "🎤 VAD - Replacing frame with silence",
                            call_id=caller_channel_id,
                            confidence=f"{vad_result.confidence:.2f}",
                            energy=vad_result.energy_level,
                            is_speech=vad_result.is_speech,
                            frames_since_speech=state.get('frames_since_speech', 0),
                        )

            # Post-guard RMS instrumentation
            try:
                post_guard_rms = audioop.rms(pcm_payload, 2) if pcm_payload else 0
            except Exception:
                post_guard_rms = 0
            try:
                logger.info(
                    "Inbound PCM guard RMS",
                    call_id=caller_channel_id,
                    pre_guard_pcm_rms=pre_guard_rms,
                    post_guard_pcm_rms=post_guard_rms,
                )
            except Exception:
                pass

            # DEBUG: Audio routing state (OpenAI troubleshooting)
            provider_name = session.provider_name or self.config.default_provider
            if provider_name == "openai_realtime":
                logger.debug(
                    "🎤 AUDIO ROUTING - Ready to forward",
                    call_id=caller_channel_id,
                    audio_capture_enabled=getattr(session, 'audio_capture_enabled', None),
                    audio_bytes=len(audio_bytes),
                    pcm_payload_bytes=len(pcm_payload) if pcm_payload else 0,
                )
            
            provider = self.providers.get(provider_name)
            if not provider:
                logger.warning(
                    "Provider object is None!",
                    provider_name=provider_name,
                    call_id=caller_channel_id,
                )
                return
            if not hasattr(provider, 'send_audio'):
                logger.warning(
                    "Provider missing send_audio method!",
                    provider_name=provider_name,
                    call_id=caller_channel_id,
                )
                return
            
            # DEBUG: Provider ready check (OpenAI troubleshooting)
            if provider_name == "openai_realtime":
                logger.debug(
                    "🎤 AUDIO ROUTING - Provider ready",
                    call_id=caller_channel_id,
                    provider_name=provider_name,
                )
            try:
                self._update_audio_diagnostics(session, "provider_in", pcm_payload, "slin16", payload_rate)
            except Exception:
                logger.debug("Provider input diagnostics update failed", call_id=caller_channel_id, exc_info=True)

            provider_payload, provider_encoding, provider_rate = self._encode_for_provider(
                session.call_id,
                provider_name,
                provider,
                pcm_payload,
                payload_rate,
            )

            # Preserve original μ-law frames for Deepgram when the payload was replaced with silence
            if (
                provider_name == "deepgram"
                and provider_encoding in ("ulaw", "mulaw", "g711_ulaw", "mu-law")
                and provider_payload
                and not any(provider_payload)
            ):
                provider_payload = audio_bytes
                provider_rate = 8000
            try:
                self.audio_capture.append_encoded(
                    session.call_id,
                    "caller_to_provider",
                    provider_payload,
                    provider_encoding,
                    provider_rate,
                )
            except Exception:
                logger.debug("Provider input capture failed", call_id=session.call_id, exc_info=True)
            await provider.send_audio(provider_payload)
            
            # DEBUG: Confirm audio sent (OpenAI troubleshooting)
            if provider_name == "openai_realtime":
                logger.debug(
                    "🎤 AUDIO ROUTING - Sent to provider",
                    call_id=caller_channel_id,
                    provider_name=provider_name,
                    bytes_sent=len(provider_payload) if provider_payload else 0,
                )
        except Exception as exc:
            logger.error("Error handling AudioSocket audio", conn_id=conn_id, error=str(exc), exc_info=True)

    async def _run_enhanced_vad(self, session: CallSession, audio_bytes: bytes) -> Optional[VADResult]:
        """Normalize inbound AudioSocket audio to PCM16 @ 8 kHz 20 ms frames and run enhanced VAD."""
        if not self.vad_manager or not audio_bytes:
            return None

        try:
            # Detect AudioSocket wire format from session first (actual negotiated),
            # then fall back to YAML. Map 'slin' (Asterisk) to PCM16 @ 8 kHz.
            try:
                fmt_token = (session.transport_profile.format or '').lower()
            except Exception:
                fmt_token = ''
            if not fmt_token:
                try:
                    fmt_token = (getattr(self.config, 'audiosocket', None).format or 'ulaw').lower()
                except Exception:
                    fmt_token = 'ulaw'

            # Determine source rate preference from session profile when available
            try:
                prof_rate = int(session.transport_profile.sample_rate or 0)
            except Exception:
                prof_rate = 0

            if fmt_token in ('ulaw', 'mulaw', 'g711_ulaw', 'mu-law'):
                pcm_src = EnhancedVADManager.mu_law_to_pcm16(audio_bytes)
                src_rate = 8000
            elif fmt_token in ('slin', 'slin8', 'linear16_8k', 'pcm16_8k'):
                # Asterisk 'slin' is 8 kHz PCM16
                pcm_src = audio_bytes
                src_rate = 8000
            else:
                # Generic PCM16: prefer session sample rate, default to 16000 only if unknown
                pcm_src = audio_bytes
                src_rate = prof_rate if prof_rate > 0 else 16000
                # Normalize endian if probe indicated swap
                try:
                    if bool(session.vad_state.get('pcm16_inbound_swap', False)):
                        pcm_src = audioop.byteswap(pcm_src, 2)
                except Exception:
                    pass
            if src_rate != 8000:
                try:
                    state = self._resample_state_vad8k.get(session.call_id)
                    pcm16, state = audioop.ratecv(pcm_src, 2, 1, src_rate, 8000, state)
                    self._resample_state_vad8k[session.call_id] = state
                except Exception:
                    pcm16 = pcm_src
            else:
                pcm16 = pcm_src
        except Exception:
            logger.debug(
                "Enhanced VAD conversion failed",
                call_id=session.call_id,
                exc_info=True,
            )
            return None

        if not pcm16:
            return None

        vad_state = session.vad_state.setdefault("enhanced_vad", {})
        frame_buffer: bytearray = vad_state.setdefault("frame_buffer", bytearray())
        frame_buffer.extend(pcm16)

        result: Optional[VADResult] = None
        stats = vad_state.setdefault("stats", {"frames": 0, "speech_frames": 0})

        while len(frame_buffer) >= 320:
            frame = bytes(frame_buffer[:320])
            del frame_buffer[:320]
            result = await self.vad_manager.process_frame(session.call_id, frame)
            stats["frames"] = stats.get("frames", 0) + 1
            if result.is_speech:
                stats["speech_frames"] = stats.get("speech_frames", 0) + 1

        if result:
            try:
                total = max(stats.get("frames", 0), 1)
                speech_ratio = stats.get("speech_frames", 0) / total
                session.vad_state["enhanced_summary"] = {
                    "frames": stats.get("frames", 0),
                    "speech_frames": stats.get("speech_frames", 0),
                    "speech_ratio": speech_ratio,
                    "last_confidence": result.confidence,
                    "last_energy": result.energy_level,
                }
            except Exception:
                pass

        return result

    def _should_use_vad_fallback(self, session: CallSession) -> bool:
        """Determine if we should use fallback audio forwarding when VAD doesn't detect speech."""
        try:
            vad_config = getattr(self.config, 'vad', None)
            if not vad_config or not getattr(vad_config, 'fallback_enabled', True):
                return False
            
            now = time.time()
            last_speech_time = session.vad_state.get('last_speech_time')
            if not last_speech_time:
                session.vad_state['last_speech_time'] = now
                return False

            silence_duration = (now - float(last_speech_time)) * 1000
            fallback_interval = getattr(vad_config, 'fallback_interval_ms', 1500)
            if silence_duration < fallback_interval:
                return False

            fallback_state = session.vad_state.setdefault('fallback_state', {
                'last_fallback_ts': 0.0,
            })

            last_fallback_ts = float(fallback_state.get('last_fallback_ts', 0.0) or 0.0)
            fallback_period_ms = 200  # Forward real audio every 200 ms during extended silence

            if (now - last_fallback_ts) * 1000 >= fallback_period_ms:
                fallback_state['last_fallback_ts'] = now
                logger.debug(
                    "🎤 VAD - Periodic fallback forwarding original audio",
                    call_id=session.call_id,
                    silence_duration_ms=int(silence_duration),
                    fallback_interval_ms=fallback_interval,
                )
                return True

            return False
            
        except Exception as e:
            logger.debug("VAD fallback logic error", call_id=session.call_id, error=str(e))
            return True  # Default to allowing audio through on error

    @staticmethod
    def _ulaw_silence(length: int) -> bytes:
        if length <= 0:
            return b""
        return bytes([0xFF]) * length

    def _silence_for_format(self, length: int) -> bytes:
        """Generate silence matching the negotiated AudioSocket format (μ-law or PCM16)."""
        if length <= 0:
            return b""
        try:
            as_fmt = (getattr(self.config, 'audiosocket', None).format or 'ulaw').lower()
        except Exception:
            as_fmt = 'ulaw'
        if as_fmt in ('ulaw', 'mulaw', 'g711_ulaw', 'mu-law'):
            return bytes([0xFF]) * length  # μ-law silence
        return b"\x00" * length  # PCM16 silence (zeroed samples)

    async def _export_config_metrics(self, call_id: str) -> None:
        """Expose configured knobs as Prometheus gauges for this call."""
        try:
            b = getattr(self.config, 'barge_in', None)
            if b:
                _CFG_BARGE_MS.labels(call_id, "initial_protection_ms").set(int(getattr(b, 'initial_protection_ms', 0)))
                _CFG_BARGE_MS.labels(call_id, "min_ms").set(int(getattr(b, 'min_ms', 0)))
                _CFG_BARGE_MS.labels(call_id, "post_tts_end_protection_ms").set(int(getattr(b, 'post_tts_end_protection_ms', 0)))
                _CFG_BARGE_MS.labels(call_id, "greeting_protection_ms").set(int(getattr(b, 'greeting_protection_ms', 0)))
                _CFG_BARGE_THRESHOLD.labels(call_id).set(int(getattr(b, 'energy_threshold', 0)))
        except Exception:
            pass
        try:
            s = getattr(self.config, 'streaming', None)
            if s:
                _CFG_STREAM_MS.labels(call_id, "min_start_ms").set(int(getattr(s, 'min_start_ms', 0)))
                _CFG_STREAM_MS.labels(call_id, "greeting_min_start_ms").set(int(getattr(s, 'greeting_min_start_ms', 0)))
                _CFG_STREAM_MS.labels(call_id, "low_watermark_ms").set(int(getattr(s, 'low_watermark_ms', 0)))
                _CFG_STREAM_MS.labels(call_id, "jitter_buffer_ms").set(int(getattr(s, 'jitter_buffer_ms', 0)))
                _CFG_STREAM_MS.labels(call_id, "fallback_timeout_ms").set(int(getattr(s, 'fallback_timeout_ms', 0)))
        except Exception:
            pass
        try:
            pblock = (getattr(self.config, 'providers', {}) or {}).get('openai_realtime', {})
            td = (pblock or {}).get('turn_detection') or {}
            if td:
                _CFG_TD_MS.labels(call_id, "silence_duration_ms").set(int(td.get('silence_duration_ms', 0)))
                _CFG_TD_MS.labels(call_id, "prefix_padding_ms").set(int(td.get('prefix_padding_ms', 0)))
                try:
                    _CFG_TD_THRESHOLD.labels(call_id).set(float(td.get('threshold', 0.0)))
                except Exception:
                    pass
        except Exception:
            pass

    async def _audiosocket_handle_disconnect(self, conn_id: str) -> None:
        """Cleanup mappings when an AudioSocket connection disconnects."""
        try:
            caller_channel_id = self.conn_to_channel.pop(conn_id, None)
            if caller_channel_id:
                conns = self.channel_to_conns.get(caller_channel_id, set())
                conns.discard(conn_id)
                if not conns:
                    self.channel_to_conns.pop(caller_channel_id, None)
                # Reset primary if needed
                if self.audiosocket_primary_conn.get(caller_channel_id) == conn_id:
                    self.audiosocket_primary_conn.pop(caller_channel_id, None)
                    if conns:
                        self.audiosocket_primary_conn[caller_channel_id] = next(iter(conns))
                # Clear audiosocket_conn_id on session if it matched
                try:
                    sess = await self.session_store.get_by_call_id(caller_channel_id)
                    if sess and getattr(sess, 'audiosocket_conn_id', None) == conn_id:
                        sess.audiosocket_conn_id = None
                        await self._save_session(sess)
                except Exception:
                    pass
            logger.info("AudioSocket connection disconnected", conn_id=conn_id, caller_channel_id=caller_channel_id)
        except Exception as exc:
            logger.error("Error during AudioSocket disconnect cleanup", conn_id=conn_id, error=str(exc), exc_info=True)

    async def _audiosocket_handle_dtmf(self, conn_id: str, digit: str) -> None:
        """Handle DTMF received over AudioSocket (informational)."""
        try:
            caller_channel_id = self.conn_to_channel.get(conn_id)
            logger.info("AudioSocket DTMF received", conn_id=conn_id, caller_channel_id=caller_channel_id, digit=digit)
        except Exception as exc:
            logger.error("Error handling AudioSocket DTMF", conn_id=conn_id, error=str(exc), exc_info=True)

    async def _on_rtp_audio(self, ssrc: int, pcm_16k: bytes) -> None:
        """Route inbound ExternalMedia RTP audio (PCM16 @ 16 kHz) to the active provider.

        This mirrors the gating/barge-in logic of `_audiosocket_handle_audio` and
        establishes an SSRC→call_id mapping the first time we see a new SSRC.
        """
        try:
            # Resolve call_id from SSRC mapping or infer from sessions awaiting SSRC
            caller_channel_id = self.ssrc_to_caller.get(ssrc)
            if not caller_channel_id:
                # Choose the most recent session that has an ExternalMedia channel and no SSRC yet
                sessions = await self.session_store.get_all_sessions()
                candidate = None
                for s in sessions:
                    try:
                        if getattr(s, 'external_media_id', None) and not getattr(s, 'ssrc', None):
                            if candidate is None or float(getattr(s, 'created_at', 0.0)) > float(getattr(candidate, 'created_at', 0.0)):
                                candidate = s
                    except Exception:
                        continue
                if candidate:
                    caller_channel_id = candidate.caller_channel_id
                    self.ssrc_to_caller[ssrc] = caller_channel_id
                    try:
                        candidate.ssrc = ssrc
                        await self._save_session(candidate)
                    except Exception:
                        pass
                    try:
                        if getattr(self, 'rtp_server', None) and hasattr(self.rtp_server, 'map_ssrc_to_call_id'):
                            self.rtp_server.map_ssrc_to_call_id(ssrc, caller_channel_id)
                        
                    except Exception:
                        pass

            if not caller_channel_id:
                logger.debug("RTP audio received for unknown SSRC", ssrc=ssrc, bytes=len(pcm_16k))
                return

            session = await self.session_store.get_by_call_id(caller_channel_id)
            if not session:
                logger.debug("No session for caller; dropping RTP audio", ssrc=ssrc, caller_channel_id=caller_channel_id)
                return

            # Check for pipeline mode FIRST (before continuous_input provider routing)
            # Pipeline adapters need audio in their queue, not sent to monolithic providers
            if self._pipeline_forced.get(caller_channel_id):
                # AAVA-28: Check gating to prevent agent from hearing its own TTS output
                if not session.audio_capture_enabled:
                    # Drop audio during TTS playback (gating active)
                    return
                
                q = self._pipeline_queues.get(caller_channel_id)
                if q:
                    try:
                        q.put_nowait(pcm_16k)  # Pipeline expects PCM16@16kHz
                        logger.debug("RTP audio routed to pipeline queue", call_id=caller_channel_id, bytes=len(pcm_16k))
                    except Exception as exc:
                        logger.warning("Pipeline queue full or unavailable (RTP)", call_id=caller_channel_id, error=str(exc))
                    return  # Done - don't route to monolithic provider
                else:
                    logger.warning("Pipeline mode active but no queue found (RTP)", call_id=caller_channel_id)

            # Check if provider requires continuous audio input using capabilities
            # Full agents with native VAD need uninterrupted audio flow for turn-taking
            provider_name = getattr(session, 'provider_name', None) or self.config.default_provider
            provider = self.providers.get(provider_name)
            continuous_input = False
            try:
                capabilities = None
                if provider and hasattr(provider, 'get_capabilities'):
                    try:
                        capabilities = provider.get_capabilities()
                    except Exception:
                        pass
                
                if capabilities and capabilities.requires_continuous_audio:
                    continuous_input = True
                else:
                    pcfg = getattr(provider, 'config', None)
                    if isinstance(pcfg, dict):
                        continuous_input = bool(pcfg.get('continuous_input', False))
                    else:
                        continuous_input = bool(getattr(pcfg, 'continuous_input', False))
            except Exception:
                continuous_input = False

            # For continuous-input providers, forward audio (but respect gating during TTS playback)
            # OpenAI Realtime has server-side echo cancellation, but we still need to gate during TTS
            # to prevent the provider from hearing its own audio as "user speech"
            if continuous_input:
                if not provider or not hasattr(provider, 'send_audio'):
                    logger.debug("Provider unavailable for continuous RTP audio", provider=provider_name)
                    return
                
                # CRITICAL: Check if audio capture is disabled (TTS playing)
                # For Google Live: Send silence frames to maintain stream continuity (like AudioSocket)
                # For OpenAI/Deepgram: Can drop audio (they handle gaps gracefully)
                needs_gating = provider_name == "google_live"
                
                if needs_gating and not session.audio_capture_enabled:
                    # Send SILENCE instead of dropping to maintain Google Live's stream
                    logger.debug(
                        "🔇 GATING ACTIVE - Sending silence frame for Google Live (TTS playing)",
                        call_id=caller_channel_id,
                        provider=provider_name,
                    )
                    # Replace audio with silence (zero-filled PCM16)
                    pcm_16k = b'\x00' * len(pcm_16k)
                elif not needs_gating and not session.audio_capture_enabled:
                    # For other providers, can safely drop audio during TTS
                    logger.debug(
                        "Dropping RTP audio for continuous provider during TTS playback",
                        call_id=caller_channel_id,
                        provider=provider_name,
                    )
                    return
                # Encode audio for provider (same as AudioSocket path)
                try:
                    # Get RTP server's configured sample rate (no longer hardcoded)
                    rtp_rate = getattr(self.rtp_server, 'sample_rate', 16000) if self.rtp_server else 16000
                    
                    prov_payload, prov_enc, prov_rate = self._encode_for_provider(
                        session.call_id,
                        provider_name,
                        provider,
                        pcm_16k,
                        rtp_rate,  # Use configured rate from RTP server
                    )
                    try:
                        self.audio_capture.append_encoded(
                            session.call_id,
                            "caller_to_provider",
                            prov_payload,
                            prov_enc,
                            prov_rate,
                        )
                    except Exception:
                        logger.debug("Provider input capture failed (continuous-input RTP)", call_id=session.call_id, exc_info=True)
                    # CRITICAL: Pass sample_rate and encoding to provider
                    # Google Live needs these to avoid double resampling
                    await provider.send_audio(prov_payload, sample_rate=prov_rate, encoding=prov_enc)
                except Exception as exc:
                    logger.debug("Continuous-input RTP forward error", call_id=caller_channel_id, error=str(exc))
                return

            # Below: standard gating/barge-in logic for hybrid (P2) providers only
            
            # Post-TTS end guard to avoid self-echo re-capture
            try:
                cfg = getattr(self.config, 'barge_in', None)
                post_guard_ms = int(getattr(cfg, 'post_tts_end_protection_ms', 0)) if cfg else 0
            except Exception:
                post_guard_ms = 0
            if post_guard_ms and getattr(session, 'tts_ended_ts', 0.0) and session.audio_capture_enabled:
                try:
                    elapsed_ms = int((time.time() - float(session.tts_ended_ts)) * 1000)
                except Exception:
                    elapsed_ms = post_guard_ms
                if elapsed_ms < post_guard_ms:
                    logger.debug(
                        "Dropping inbound RTP during post-TTS protection window",
                        call_id=caller_channel_id,
                        elapsed_ms=elapsed_ms,
                        protect_ms=post_guard_ms,
                    )
                    return

            # If TTS is playing (capture disabled), decide whether to drop or barge-in
            if hasattr(session, 'audio_capture_enabled') and not session.audio_capture_enabled:
                cfg = getattr(self.config, 'barge_in', None)
                if not cfg or not getattr(cfg, 'enabled', True):
                    logger.debug("Dropping inbound RTP during TTS playback (barge-in disabled)",
                                 ssrc=ssrc, caller_channel_id=caller_channel_id, bytes=len(pcm_16k))
                    return

                now = time.time()
                tts_elapsed_ms = 0
                try:
                    if getattr(session, 'tts_started_ts', 0.0) > 0:
                        tts_elapsed_ms = int((now - session.tts_started_ts) * 1000)
                except Exception:
                    tts_elapsed_ms = 0

                initial_protect = int(getattr(cfg, 'initial_protection_ms', 200))
                try:
                    if getattr(session, 'conversation_state', None) == 'greeting':
                        greet_ms = int(getattr(cfg, 'greeting_protection_ms', 0))
                        if greet_ms > initial_protect:
                            initial_protect = greet_ms
                except Exception:
                    pass
                if tts_elapsed_ms < initial_protect:
                    logger.debug("Dropping inbound RTP during initial TTS protection window",
                                 ssrc=ssrc, caller_channel_id=caller_channel_id,
                                 tts_elapsed_ms=tts_elapsed_ms, protect_ms=initial_protect)
                    return

                # Barge-in detection on PCM16 energy
                try:
                    energy = audioop.rms(pcm_16k, 2)
                except Exception:
                    energy = 0
                threshold = int(getattr(cfg, 'energy_threshold', 1000))
                frame_ms = 20
                if energy >= threshold:
                    if int(getattr(session, 'barge_in_candidate_ms', 0)) == 0:
                        try:
                            session.barge_start_ts = now
                        except Exception:
                            session.barge_start_ts = 0.0
                    session.barge_in_candidate_ms = int(getattr(session, 'barge_in_candidate_ms', 0)) + frame_ms
                else:
                    session.barge_in_candidate_ms = 0

                cooldown_ms = int(getattr(cfg, 'cooldown_ms', 500))
                last_barge_in_ts = float(getattr(session, 'last_barge_in_ts', 0.0) or 0.0)
                in_cooldown = (now - last_barge_in_ts) * 1000 < cooldown_ms if last_barge_in_ts else False

                min_ms = int(getattr(cfg, 'min_ms', 250))
                if not in_cooldown and session.barge_in_candidate_ms >= min_ms:
                    try:
                        playback_ids = await self.session_store.list_playbacks_for_call(caller_channel_id)
                        for pid in playback_ids:
                            try:
                                await self.ari_client.stop_playback(pid)
                            except Exception:
                                logger.debug("Playback stop error during RTP barge-in", playback_id=pid, exc_info=True)

                        tokens = list(getattr(session, 'tts_tokens', set()) or [])
                        for token in tokens:
                            try:
                                if self.conversation_coordinator:
                                    await self.conversation_coordinator.on_tts_end(caller_channel_id, token, reason="barge-in")
                            except Exception:
                                logger.debug("Failed to clear gating token during RTP barge-in", token=token, exc_info=True)

                        session.barge_in_candidate_ms = 0
                        session.last_barge_in_ts = now
                        try:
                            if float(getattr(session, 'barge_start_ts', 0.0) or 0.0) > 0.0:
                                reaction_s = max(0.0, now - float(session.barge_start_ts))
                                _BARGE_REACTION_SECONDS.labels(caller_channel_id).observe(reaction_s)
                                session.barge_start_ts = 0.0
                        except Exception:
                            pass
                        await self._save_session(session)
                        logger.info("🎧 BARGE-IN (RTP) triggered", call_id=caller_channel_id)
                    except Exception:
                        logger.error("Error triggering RTP barge-in", call_id=caller_channel_id, exc_info=True)
                else:
                    # Not yet triggered; drop inbound frame while TTS is active
                    if energy > 0 and self.conversation_coordinator:
                        try:
                            self.conversation_coordinator.note_audio_during_tts(caller_channel_id)
                        except Exception:
                            pass
                    logger.debug("Dropping inbound RTP during TTS (candidate_ms=%d, energy=%d)",
                                 session.barge_in_candidate_ms, energy)
                    return

            # If a pipeline was explicitly requested for this call, route to pipeline queue
            if self._pipeline_forced.get(caller_channel_id):
                # AAVA-28: Check gating to prevent agent from hearing its own TTS output
                if not session.audio_capture_enabled:
                    # Drop audio during TTS playback (gating active)
                    return
                
                q = self._pipeline_queues.get(caller_channel_id)
                if q:
                    try:
                        q.put_nowait(pcm_16k)
                        return
                    except asyncio.QueueFull:
                        logger.debug("Pipeline queue full; dropping RTP frame", call_id=caller_channel_id)
                        return

            provider_name = session.provider_name or self.config.default_provider
            provider = self.providers.get(provider_name)
            if not provider or not hasattr(provider, 'send_audio'):
                logger.debug("Provider unavailable for RTP audio", provider=provider_name)
                return

            # Forward PCM16 16k frames to provider
            await provider.send_audio(pcm_16k)
        except Exception as exc:
            logger.error("Error handling RTP audio", ssrc=ssrc, error=str(exc), exc_info=True)

    def _build_deepgram_config(self, provider_cfg: Dict[str, Any]) -> Optional[DeepgramProviderConfig]:
        """Construct a DeepgramProviderConfig from raw provider settings with validation."""
        try:
            # SECURITY: API keys ONLY from environment variables, never from YAML
            merged = dict(provider_cfg)
            merged['api_key'] = os.getenv('DEEPGRAM_API_KEY')  # Force from .env only
            
            cfg = DeepgramProviderConfig(**merged)
            if not cfg.api_key:
                logger.error("Deepgram provider API key missing (DEEPGRAM_API_KEY)")
                return None
            return cfg
        except Exception as exc:
            logger.error("Failed to build DeepgramProviderConfig", error=str(exc), exc_info=True)
            return None

    def _build_openai_realtime_config(self, provider_cfg: Dict[str, Any]) -> Optional[OpenAIRealtimeProviderConfig]:
        """Construct an OpenAIRealtimeProviderConfig from raw provider settings."""
        try:
            # Respect provider overrides; only fill when missing/empty
            merged = dict(provider_cfg)
            
            # SECURITY: API key ONLY from environment variables, never from YAML
            merged['api_key'] = os.getenv('OPENAI_API_KEY')  # Force from .env only
            
            try:
                instr = (merged.get("instructions") or "").strip()
            except Exception:
                instr = ""
            if not instr:
                merged["instructions"] = getattr(self.config.llm, "prompt", None)
            try:
                greet = (merged.get("greeting") or "").strip()
            except Exception:
                greet = ""
            if not greet:
                merged["greeting"] = getattr(self.config.llm, "initial_greeting", None)

            cfg = OpenAIRealtimeProviderConfig(**merged)
            if not cfg.enabled:
                logger.info("OpenAI Realtime provider disabled in configuration; skipping initialization.")
                return None
            if not cfg.api_key:
                logger.error("OpenAI Realtime provider API key missing (OPENAI_API_KEY)")
                return None
            return cfg
        except Exception as exc:
            logger.error("Failed to build OpenAIRealtimeProviderConfig", error=str(exc), exc_info=True)
            return None

    def _audit_provider_config(self, name: str, provider_cfg: Dict[str, Any]) -> List[str]:
        """Static sanity checks for provider/audio format alignment.

        Returns a list of descriptive issue strings when mismatches are detected."""
        issues: List[str] = []
        try:
            audiosocket_format = "ulaw"
            try:
                if getattr(self.config, "audiosocket", None):
                    audiosocket_format = (self.config.audiosocket.format or "ulaw").lower()
            except Exception:
                audiosocket_format = "ulaw"
            audiosocket_canon = self._canonicalize_encoding(audiosocket_format)

            if name == "deepgram":
                enc = (provider_cfg.get("input_encoding") or "linear16").lower()
                enc_canon = self._canonicalize_encoding(enc)
                if enc_canon in {"slin16", "linear16", "pcm16"} and audiosocket_canon not in {"slin", "slin16"}:
                    issues.append(
                        f"Deepgram expects PCM input but audiosocket.format={audiosocket_format}; "
                        "set audiosocket.format=slin16 or change deepgram.input_encoding to ulaw."
                    )
                if enc_canon in {"ulaw", "mulaw", "g711_ulaw", "mu-law"} and audiosocket_canon not in {"ulaw", "mulaw"}:
                    # Allow intentional bridge: audiosocket carries PCM16 while provider works in μ-law
                    if audiosocket_canon not in {"slin", "slin16"}:
                        issues.append(
                            f"Deepgram expects μ-law input but audiosocket.format={audiosocket_format}; "
                            "set audiosocket.format=ulaw or change deepgram.input_encoding to linear16."
                        )

            if name == "openai_realtime":
                provider_rate = int(provider_cfg.get("provider_input_sample_rate_hz") or 0)
                output_rate = int(provider_cfg.get("output_sample_rate_hz") or 0)
                if provider_rate and provider_rate < 24000:
                    issues.append(
                        f"OpenAI Realtime provider_input_sample_rate_hz={provider_rate}; "
                        "set to 24000 for correct streaming."
                    )
                if output_rate and output_rate < 24000:
                    issues.append(
                        f"OpenAI Realtime output_sample_rate_hz={output_rate}; "
                        "set to 24000 so downstream audio plays at the correct speed."
                    )

                # Check target encoding vs audiosocket format
                # NOTE: Intentional transcoding is supported - system handles conversion
                target_encoding = (provider_cfg.get("target_encoding") or "ulaw").lower()
                if target_encoding in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                    if audiosocket_format in ("ulaw", "mulaw"):
                        # Perfect alignment
                        pass
                    elif audiosocket_format in ("slin", "slin16", "linear16", "pcm16"):
                        # Intentional transcoding: AudioSocket PCM → Provider μ-law (system handles this)
                        pass
                    else:
                        issues.append(
                            f"OpenAI Realtime target_encoding={target_encoding} but audiosocket.format={audiosocket_format}; "
                            "set audiosocket.format=ulaw or adjust provider target encoding."
                        )
                if target_encoding in ("slin16", "linear16", "pcm16") and audiosocket_format not in ("slin", "slin16", "linear16", "pcm16"):
                    issues.append(
                        f"OpenAI Realtime target_encoding={target_encoding} but audiosocket.format={audiosocket_format}; "
                        "set audiosocket.format=slin16 or change provider target encoding."
                    )
        except Exception:
            logger.debug("Provider configuration audit failed", provider=name, exc_info=True)
        return issues

    def _describe_provider_alignment(self, name: str, provider: AIProviderInterface) -> List[str]:
        issues: List[str] = []
        try:
            audiosocket_format = "ulaw"
            try:
                if getattr(self.config, "audiosocket", None):
                    audiosocket_format = (self.config.audiosocket.format or "ulaw").lower()
            except Exception:
                audiosocket_format = "ulaw"
            audiosocket_canon = self._canonicalize_encoding(audiosocket_format)

            streaming_encoding = getattr(self.streaming_playback_manager, "audiosocket_format", None)
            if streaming_encoding:
                streaming_encoding = streaming_encoding.lower()
            else:
                streaming_encoding = audiosocket_format
            streaming_canon = self._canonicalize_encoding(streaming_encoding) or audiosocket_canon

            try:
                streaming_rate = int(getattr(self.streaming_playback_manager, "sample_rate", 8000) or 8000)
            except Exception:
                streaming_rate = 8000

            describe_method = getattr(provider, "describe_alignment", None)
            if callable(describe_method):
                issues.extend(
                    describe_method(
                        audiosocket_format=audiosocket_canon,
                        streaming_encoding=streaming_canon,
                        streaming_sample_rate=streaming_rate,
                    )
                )
        except Exception:
            logger.debug("Provider alignment description failed", provider=name, exc_info=True)
        return issues

    def _audit_transport_alignment(self) -> None:
        """Log a pre-call summary of transport settings and warn on misalignment.

        YAML is the source of truth. We check:
        - audiosocket.format vs streaming.sample_rate
        - provider target vs audiosocket.format
        - OpenAI Realtime provider input/output sample rates
        """
        try:
            # Gather core transport settings
            as_fmt = "ulaw"
            if getattr(self.config, "audiosocket", None):
                try:
                    as_fmt = (self.config.audiosocket.format or "ulaw").lower()
                except Exception:
                    as_fmt = "ulaw"
            try:
                streaming_rate = int(getattr(self.streaming_playback_manager, "sample_rate", 8000) or 8000)
            except Exception:
                streaming_rate = 8000

            # Provider configs (raw YAML dicts)
            providers_cfg = getattr(self.config, "providers", {}) or {}
            oair_cfg = providers_cfg.get("openai_realtime", {}) or {}
            dg_cfg = providers_cfg.get("deepgram", {}) or {}

            # Normalize key fields
            def _lower_str(d: dict, key: str, default: str = "") -> str:
                val = d.get(key, default)
                if isinstance(val, str):
                    return val.lower()
                return str(val).lower()

            oair_target_enc = _lower_str(oair_cfg, "target_encoding", "ulaw")
            oair_target_rate = int(oair_cfg.get("target_sample_rate_hz") or 8000)
            oair_in_rate = int(oair_cfg.get("provider_input_sample_rate_hz") or 24000)
            oair_out_rate = int(oair_cfg.get("output_sample_rate_hz") or 24000)

            dg_in_enc = _lower_str(dg_cfg, "input_encoding", "linear16")
            try:
                dg_in_rate = int(dg_cfg.get("input_sample_rate_hz") or 8000)
            except Exception:
                dg_in_rate = 8000

            # Info summary
            streaming_target_fmt = (getattr(self.streaming_playback_manager, "audiosocket_format", None) or as_fmt).lower()
            streaming_swap_mode = getattr(self.streaming_playback_manager, "egress_swap_mode", "auto")
            streaming_force_mulaw = bool(getattr(self.streaming_playback_manager, "egress_force_mulaw", False))

            dg_out_enc = _lower_str(dg_cfg, "output_encoding", "")
            try:
                dg_out_rate = int(dg_cfg.get("output_sample_rate_hz") or 0)
            except Exception:
                dg_out_rate = 0

            summary = {
                "audiosocket_format": as_fmt,
                "streaming_target_encoding": streaming_target_fmt,
                "streaming_sample_rate_hz": streaming_rate,
                "streaming_egress_swap_mode": streaming_swap_mode,
                "streaming_egress_force_mulaw": streaming_force_mulaw,
                "openai_realtime_input_encoding": _lower_str(oair_cfg, "input_encoding", ""),
                "openai_realtime_input_sample_rate_hz": int(oair_cfg.get("input_sample_rate_hz") or 0),
                "openai_realtime_provider_input_sample_rate_hz": oair_in_rate,
                "openai_realtime_output_sample_rate_hz": oair_out_rate,
                "openai_realtime_target_encoding": oair_target_enc,
                "openai_realtime_target_sample_rate_hz": oair_target_rate,
                "deepgram_input_encoding": dg_in_enc,
                "deepgram_input_sample_rate_hz": dg_in_rate,
                "deepgram_output_encoding": dg_out_enc,
                "deepgram_output_sample_rate_hz": dg_out_rate,
            }

            logger.info("Transport alignment summary", **summary)

            # Expected streaming rate from audiosocket format
            expected_rate = None
            if as_fmt in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                expected_rate = 8000
            elif as_fmt in ("slin16", "linear16", "pcm16"):
                expected_rate = 16000

            # Warn on streaming rate mismatch
            if expected_rate and streaming_rate != expected_rate:
                logger.warning(
                    "Streaming sample rate misaligned with audiosocket.format",
                    audiosocket_format=as_fmt,
                    streaming_sample_rate=streaming_rate,
                    expected_sample_rate=expected_rate,
                    suggestion=(
                        "Set streaming.sample_rate to %d or change audiosocket.format to match"
                        % expected_rate
                    ),
                )

            # Provider target vs audiosocket.format
            if as_fmt in ("ulaw", "mulaw", "g711_ulaw", "mu-law") and oair_target_enc not in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                logger.warning(
                    "OpenAI target encoding misaligned with audiosocket.format",
                    audiosocket_format=as_fmt,
                    openai_target_encoding=oair_target_enc,
                    suggestion="Set providers.openai_realtime.target_encoding to 'ulaw' or change audiosocket.format",
                )
            if as_fmt in ("slin16", "linear16", "pcm16") and oair_target_enc not in ("slin16", "linear16", "pcm16"):
                logger.warning(
                    "OpenAI target encoding misaligned with audiosocket.format",
                    audiosocket_format=as_fmt,
                    openai_target_encoding=oair_target_enc,
                    suggestion="Set providers.openai_realtime.target_encoding to 'slin16' or change audiosocket.format",
                )

            # OpenAI provider IO rates
            if oair_in_rate and oair_in_rate < 24000:
                logger.warning(
                    "OpenAI provider_input_sample_rate_hz suboptimal",
                    value=oair_in_rate,
                    suggestion="Set providers.openai_realtime.provider_input_sample_rate_hz to 24000",
                )
            if oair_out_rate and oair_out_rate < 24000:
                logger.warning(
                    "OpenAI output_sample_rate_hz suboptimal",
                    value=oair_out_rate,
                    suggestion="Set providers.openai_realtime.output_sample_rate_hz to 24000",
                )

            # Deepgram input encoding vs audiosocket (suppress intentional PCM↔μ-law bridge)
            try:
                as_canon = self._canonicalize_encoding(as_fmt)
            except Exception:
                as_canon = as_fmt
            try:
                dg_in_canon = self._canonicalize_encoding(dg_in_enc)
            except Exception:
                dg_in_canon = dg_in_enc

            if dg_in_canon in ("ulaw",) and as_canon in ("slin", "slin16"):
                # Intentional bridge: audiosocket carries PCM16 (slin/slin16) while Deepgram ingests μ-law
                # System transcodes between them - this is the golden baseline configuration
                pass
            elif dg_in_enc in ("ulaw", "mulaw", "g711_ulaw", "mu-law") and as_fmt not in ("ulaw", "mulaw", "g711_ulaw", "mu-law", "slin", "slin16"):
                logger.warning(
                    "Deepgram input encoding expects μ-law but audiosocket is PCM",
                    audiosocket_format=as_fmt,
                    deepgram_input_encoding=dg_in_enc,
                    suggestion="Set audiosocket.format to 'ulaw' or change deepgram.input_encoding to 'linear16'",
                )
            if dg_in_enc in ("slin16", "linear16", "pcm16") and as_fmt not in ("slin16", "linear16", "pcm16"):
                logger.warning(
                    "Deepgram input encoding expects PCM16 but audiosocket is μ-law",
                    audiosocket_format=as_fmt,
                    deepgram_input_encoding=dg_in_enc,
                    suggestion="Set audiosocket.format to 'slin16' or change deepgram.input_encoding to 'ulaw'",
                )
        except Exception:
            logger.debug("Transport audit encountered an error", exc_info=True)

    async def on_provider_event(self, event: Dict[str, Any]):
        """Handle async events from the active provider (Deepgram/OpenAI/local).

        For file-based downstream (current default), buffer AgentAudio bytes until
        AgentAudioDone, then play the accumulated audio via PlaybackManager.
        """
        try:
            etype = event.get("type")
            call_id = event.get("call_id")
            if not call_id:
                return

            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                logger.warning("Provider event for unknown call", event_type=etype, call_id=call_id)
                return

            # Provider requests early TTS gating clear (e.g., OpenAI greeting complete)
            if etype == "ClearTtsGating":
                try:
                    tokens = list(getattr(session, "tts_tokens", set()) or [])
                except Exception:
                    tokens = []
                if not tokens:
                    logger.info(
                        "ClearTtsGating received but no active TTS tokens",
                        call_id=call_id,
                        reason=event.get("reason"),
                    )
                    return

                logger.info(
                    "Processing ClearTtsGating event",
                    call_id=call_id,
                    reason=event.get("reason"),
                    token_count=len(tokens),
                )
                for token in tokens:
                    try:
                        if self.conversation_coordinator:
                            await self.conversation_coordinator.on_tts_end(call_id, token, reason=event.get("reason") or "provider-request")
                    except Exception:
                        logger.debug(
                            "Failed to clear gating token from ClearTtsGating",
                            call_id=call_id,
                            token=token,
                            exc_info=True,
                        )
                return

            # Provider announced its audio format before first audio chunk
            if etype == "ProviderAudioFormat":
                encoding = event.get("encoding")
                if isinstance(encoding, bytes):
                    try:
                        encoding = encoding.decode("utf-8", "ignore")
                    except Exception:
                        encoding = None
                if isinstance(encoding, str):
                    encoding = encoding.lower().strip() or None
                sr_val = event.get("sample_rate")
                try:
                    sample_rate = int(sr_val) if sr_val is not None else None
                except (TypeError, ValueError):
                    sample_rate = None

                # Persist as the stream's expected source format so streaming manager can align
                fmt_entry = self._provider_stream_formats.get(call_id, {}).copy()
                if encoding is not None:
                    fmt_entry["encoding"] = encoding
                if sample_rate is not None:
                    fmt_entry["sample_rate"] = sample_rate
                if fmt_entry:
                    self._provider_stream_formats[call_id] = fmt_entry

                # Update transport profile early (source="provider") for downstream alignment
                try:
                    await self._update_transport_profile(session, fmt=encoding, sample_rate=sample_rate, source="provider")
                except Exception:
                    logger.debug("ProviderAudioFormat profile update failed", call_id=call_id, exc_info=True)

                logger.info("Provider audio format announced", call_id=call_id, encoding=encoding, sample_rate=sample_rate)
                return

            # Downstream strategy: stream provider audio in near-real time via StreamingPlaybackManager
            if etype == "AgentAudio":
                chunk: bytes = event.get("data") or b""
                if not chunk:
                    return
                encoding = event.get("encoding")
                if isinstance(encoding, bytes):
                    try:
                        encoding = encoding.decode("utf-8", "ignore")
                    except Exception:
                        encoding = None
                if isinstance(encoding, str):
                    encoding = encoding.lower().strip()
                    if not encoding:
                        encoding = None
                sample_rate_val = event.get("sample_rate")
                sample_rate_int: Optional[int]
                try:
                    sample_rate_int = int(sample_rate_val) if sample_rate_val is not None else None
                except (TypeError, ValueError):
                    sample_rate_int = None
                # Persist latest provider format hints per call
                fmt_entry = self._provider_stream_formats.get(call_id, {}).copy()
                if encoding is not None:
                    fmt_entry["encoding"] = encoding
                if sample_rate_int is not None:
                    fmt_entry["sample_rate"] = sample_rate_int
                if fmt_entry:
                    self._provider_stream_formats[call_id] = fmt_entry
                try:
                    diag_encoding = fmt_entry.get("encoding") or encoding or session.transport_profile.format
                    diag_rate = int(fmt_entry.get("sample_rate") or sample_rate_int or session.transport_profile.sample_rate)
                    self._update_audio_diagnostics(session, "provider_out", chunk, diag_encoding, diag_rate)
                except Exception:
                    logger.debug("Provider audio diagnostics update failed", call_id=call_id, exc_info=True)
                try:
                    self.audio_capture.append_encoded(
                        call_id,
                        "agent_from_provider",
                        chunk,
                        diag_encoding,
                        diag_rate,
                    )
                except Exception:
                    logger.debug("Provider audio capture failed", call_id=call_id, exc_info=True)
                # Log provider AgentAudio chunk metrics for RCA
                try:
                    rate = int(sample_rate_int or diag_rate or 0) if (locals().get('diag_rate') is not None) else int(sample_rate_int or 0)
                except Exception:
                    rate = 0
                try:
                    enc = (encoding or diag_encoding or "").lower() if (locals().get('diag_encoding') is not None) else (encoding or "")
                except Exception:
                    enc = encoding or ""
                bps = 2 if enc in ("linear16", "pcm16", "slin", "slin16") else 1
                duration_ms = 0.0
                try:
                    if rate and bps:
                        duration_ms = round((len(chunk) / float(bps * rate)) * 1000.0, 3)
                except Exception:
                    duration_ms = 0.0
                seq = self._provider_chunk_seq.get(call_id, 0) + 1
                self._provider_chunk_seq[call_id] = seq
                try:
                    logger.info(
                        "PROVIDER CHUNK",
                        call_id=call_id,
                        seq=seq,
                        size_bytes=len(chunk),
                        encoding=enc,
                        sample_rate_hz=rate,
                        approx_duration_ms=duration_ms,
                    )
                except Exception:
                    pass
                # Use streaming config rate for provider audio, not transport_profile which can be
                # corrupted by inbound audio detection (user's 8kHz vs provider's 16kHz)
                wire_rate = getattr(self.config.streaming, "sample_rate", 16000) or rate or 16000
                try:
                    transport_encoding = self._canonicalize_encoding(session.transport_profile.format)
                except Exception:
                    transport_encoding = ""
                out_chunk = chunk
                if enc in ("linear16", "pcm16", "slin", "slin16") and rate and wire_rate and rate != wire_rate:
                    try:
                        out_chunk, _ = audioop.ratecv(chunk, 2, 1, rate, wire_rate, None)
                        seq = self._provider_chunk_seq.get(call_id, 0) + 1
                        self._provider_chunk_seq[call_id] = seq
                        logger.info(
                            "PROVIDER CHUNK",
                            call_id=call_id,
                            seq=seq,
                            size_bytes=len(chunk),
                            encoding=enc,
                            sample_rate_hz=rate,
                            approx_duration_ms=duration_ms,
                        )
                    except Exception:
                        logger.debug("Provider chunk resample failed; passing original", call_id=call_id, exc_info=True)
                # Do not slice μ-law in engine; StreamingPlaybackManager handles segmentation/pacing

                # Coalescing settings
                coalesce_enabled = bool(getattr(getattr(self.config, 'streaming', {}), 'coalesce_enabled', False))
                try:
                    coalesce_min_ms = int(getattr(self.config.streaming, 'coalesce_min_ms', 600))
                except Exception:
                    coalesce_min_ms = 600
                try:
                    micro_fallback_ms = int(getattr(self.config.streaming, 'micro_fallback_ms', 300))
                except Exception:
                    micro_fallback_ms = 300

                q = self._provider_stream_queues.get(call_id)
                # In continuous-stream mode, ensure per-segment gating is active
                try:
                    if getattr(self.streaming_playback_manager, 'continuous_stream', False):
                        if call_id not in self._segment_tts_active:
                            await self.streaming_playback_manager.start_segment_gating(call_id)
                            self._segment_tts_active.add(call_id)
                except Exception:
                    logger.debug("Failed to start segment gating", call_id=call_id, exc_info=True)
                if coalesce_enabled and q is None and not isinstance(out_chunk, list):
                    buf = self._provider_coalesce_buf.setdefault(call_id, bytearray())
                    buf.extend(out_chunk)
                    try:
                        # Respect μ-law 1 byte/sample vs PCM16 2 bytes/sample
                        fmt = self._canonicalize_encoding(session.transport_profile.format)
                        bps = 1 if fmt == "mulaw" or fmt == "ulaw" else 2
                        buf_ms = round((len(buf) / float(max(1, bps * max(1, wire_rate)))) * 1000.0, 3)
                    except Exception:
                        buf_ms = 0.0
                    logger.info("PROVIDER COALESCE BUFFER", call_id=call_id, buf_ms=buf_ms, bytes=len(buf))
                    if buf_ms < coalesce_min_ms:
                        # Count provider bytes even while buffering prior to stream start
                        try:
                            self._provider_bytes[call_id] = int(self._provider_bytes.get(call_id, 0)) + (len(chunk) if isinstance(chunk, (bytes, bytearray)) else len(out_chunk))
                        except Exception:
                            pass
                        # Keep buffering until threshold
                        return
                    # Start streaming now with coalesced buffer
                    try:
                        q = asyncio.Queue(maxsize=256)
                        self._provider_stream_queues[call_id] = q
                        playback_type = "greeting" if getattr(session, "conversation_state", "") == "greeting" else "streaming-response"
                        fmt_info = self._provider_stream_formats.get(call_id, {})
                        provider_name = getattr(session, "provider_name", None) or self.config.default_provider
                        alignment_issues = self.provider_alignment_issues.get(provider_name, [])
                        if alignment_issues and call_id not in self._runtime_alignment_logged:
                            for detail in alignment_issues:
                                logger.warning("Provider codec/sample alignment issue persists during streaming", call_id=call_id, provider=provider_name, detail=detail)
                            self._runtime_alignment_logged.add(call_id)
                        target_encoding, target_sample_rate, remediation = self._resolve_stream_targets(session, session.provider_name)
                        if target_sample_rate <= 0:
                            target_sample_rate = session.transport_profile.wire_sample_rate
                        if remediation:
                            session.audio_diagnostics["codec_remediation"] = remediation
                        
                        # Get source sample rate with fallback to provider's configured output rate
                        source_sample_rate = fmt_info.get("sample_rate")
                        if not source_sample_rate:
                            # Fallback: use provider's configured output rate (prevents 8kHz default)
                            try:
                                provider = session.provider
                                if hasattr(provider, '_dg_output_rate'):
                                    source_sample_rate = provider._dg_output_rate
                                    logger.debug(
                                        "Using provider configured output rate as source_sample_rate fallback",
                                        call_id=call_id,
                                        rate=source_sample_rate,
                                        reason="fmt_info empty"
                                    )
                            except Exception:
                                pass
                        # Final fallback to streaming config
                        if not source_sample_rate:
                            source_sample_rate = self.config.streaming.sample_rate
                        
                        # DOWNSTREAM_MODE GATING: Check if streaming playback is allowed
                        # downstream_mode="file" forces file-based playback (useful for debugging/testing)
                        # downstream_mode="stream" allows streaming playback (default for full agents)
                        use_streaming = self.config.downstream_mode != "file"
                        
                        if use_streaming:
                            await self.streaming_playback_manager.start_streaming_playback(
                                call_id,
                                q,
                                playback_type=playback_type,
                                source_encoding=fmt_info.get("encoding"),
                                source_sample_rate=source_sample_rate,
                                target_encoding=target_encoding,
                                target_sample_rate=target_sample_rate,
                            )
                        else:
                            # downstream_mode="file" - use file playback instead of streaming
                            logger.info("Using file playback (downstream_mode=file)", call_id=call_id)
                            try:
                                playback_id = await self.playback_manager.play_audio(call_id, bytes(buf), "streaming-response")
                                logger.info("File playback started (forced by downstream_mode)", 
                                           call_id=call_id, playback_id=playback_id, buf_ms=buf_ms)
                            except Exception:
                                logger.error("File playback failed (downstream_mode=file)", call_id=call_id, exc_info=True)
                            self._provider_coalesce_buf.pop(call_id, None)
                            return
                        self._emit_transport_card(
                            call_id,
                            session,
                            source_encoding=fmt_info.get("encoding") or encoding,
                            source_sample_rate=source_sample_rate,
                            target_encoding=target_encoding,
                            target_sample_rate=target_sample_rate,
                        )
                        logger.info("COALESCE START", call_id=call_id, coalesced_ms=buf_ms, coalesced_bytes=len(buf))
                        try:
                            q.put_nowait(bytes(buf))
                            # Account for the initial coalesced enqueue
                            try:
                                self._enqueued_bytes[call_id] = int(self._enqueued_bytes.get(call_id, 0)) + len(buf)
                            except Exception:
                                pass
                        except asyncio.QueueFull:
                            logger.debug("Coalesced enqueue dropped (queue full)", call_id=call_id)
                        self._provider_coalesce_buf.pop(call_id, None)
                        return
                    except Exception:
                        logger.error("File fallback failed after coalesce start error", call_id=call_id, exc_info=True)
                        self._provider_coalesce_buf.pop(call_id, None)
                        return
                else:
                    # Normal path: ensure stream and enqueue
                    if q is None:
                        # No existing queue - create new one
                        q = asyncio.Queue(maxsize=256)
                        self._provider_stream_queues[call_id] = q
                        try:
                            playback_type = "greeting" if getattr(session, "conversation_state", "") == "greeting" else "streaming-response"
                            fmt_info = self._provider_stream_formats.get(call_id, {})
                            provider_name = getattr(session, "provider_name", None) or self.config.default_provider
                            alignment_issues = self.provider_alignment_issues.get(provider_name, [])
                            if alignment_issues and call_id not in self._runtime_alignment_logged:
                                for detail in alignment_issues:
                                    logger.warning("Provider codec/sample alignment issue persists during streaming", call_id=call_id, provider=provider_name, detail=detail)
                                self._runtime_alignment_logged.add(call_id)
                            target_encoding, target_sample_rate, remediation = self._resolve_stream_targets(session, session.provider_name)
                            if target_sample_rate <= 0:
                                target_sample_rate = session.transport_profile.wire_sample_rate
                            if remediation:
                                session.audio_diagnostics["codec_remediation"] = remediation
                            src_encoding = fmt_info.get("encoding") or encoding
                            src_rate = fmt_info.get("sample_rate") or sample_rate_int or getattr(session.provider, "_dg_output_rate", None)
                            
                            # DOWNSTREAM_MODE GATING: Check if streaming playback is allowed
                            use_streaming = self.config.downstream_mode != "file"
                            
                            if use_streaming:
                                await self.streaming_playback_manager.start_streaming_playback(
                                    call_id,
                                    q,
                                    playback_type=playback_type,
                                    source_encoding=src_encoding,
                                    source_sample_rate=src_rate,
                                    target_encoding=target_encoding,
                                    target_sample_rate=target_sample_rate,
                                )
                            else:
                                # downstream_mode="file" - use file playback instead of streaming
                                logger.info("Using file playback (downstream_mode=file)", call_id=call_id)
                                try:
                                    playback_id = await self.playback_manager.play_audio(call_id, out_chunk, "streaming-response")
                                    if playback_id:
                                        logger.info("File playback started (forced by downstream_mode)", 
                                                   call_id=call_id, playback_id=playback_id)
                                    else:
                                        logger.error("File playback failed (downstream_mode=file)", call_id=call_id)
                                except Exception:
                                    logger.error("File playback exception (downstream_mode=file)", call_id=call_id, exc_info=True)
                                return
                            self._emit_transport_card(
                                call_id,
                                session,
                                source_encoding=src_encoding,
                                source_sample_rate=src_rate,
                                target_encoding=target_encoding,
                                target_sample_rate=target_sample_rate,
                            )
                            logger.info("Streaming playback started", call_id=call_id)
                        except Exception:
                            logger.error("Failed to start streaming playback", call_id=call_id, exc_info=True)
                            try:
                                playback_id = await self.playback_manager.play_audio(call_id, out_chunk, "streaming-response")
                                if not playback_id:
                                    logger.error("Fallback file playback failed", call_id=call_id, size=len(out_chunk))
                            except Exception:
                                logger.error("Fallback file playback exception", call_id=call_id, exc_info=True)
                            return
                    try:
                        # Track provider bytes
                        self._provider_bytes[call_id] = int(self._provider_bytes.get(call_id, 0)) + (len(chunk) if isinstance(chunk, (bytes, bytearray)) else sum(len(f) for f in (out_chunk if isinstance(out_chunk, list) else [out_chunk])))
                        if isinstance(out_chunk, list):
                            for frame in out_chunk:
                                q.put_nowait(frame)
                                self._enqueued_bytes[call_id] = int(self._enqueued_bytes.get(call_id, 0)) + len(frame)
                        else:
                            q.put_nowait(out_chunk)
                            self._enqueued_bytes[call_id] = int(self._enqueued_bytes.get(call_id, 0)) + len(out_chunk)
                    except asyncio.QueueFull:
                        logger.debug("Provider streaming queue full; dropping chunk", call_id=call_id)
            elif etype == "AgentAudioDone":
                continuous = bool(getattr(self.streaming_playback_manager, 'continuous_stream', False))
                q = self._provider_stream_queues.get(call_id)
                if continuous:
                    # Do NOT end the stream; mark boundary and end per-segment gating
                    try:
                        await self.streaming_playback_manager.mark_segment_boundary(call_id)
                    except Exception:
                        logger.debug("Failed to mark segment boundary", call_id=call_id, exc_info=True)
                    try:
                        await self.streaming_playback_manager.end_segment_gating(call_id)
                    except Exception:
                        logger.debug("Failed to end segment gating", call_id=call_id, exc_info=True)
                    # CRITICAL FIX #1: Do NOT discard call_id for continuous streams
                    # Discarding causes subsequent chunks to re-trigger gating, interrupting playback
                    # For OpenAI greeting: 20+ interruptions in 86s call (gating every 3-5s)
                    # Keep call_id in set so subsequent chunks don't re-gate
                    # try:
                    #     self._segment_tts_active.discard(call_id)
                    # except Exception:
                    #     pass
                else:
                    if q is not None:
                        # Signal end of stream (per-segment mode)
                        try:
                            q.put_nowait(None)  # sentinel for StreamingPlaybackManager
                        except asyncio.QueueFull:
                            asyncio.create_task(q.put(None))
                        # Clear queue reference so next chunk creates new queue/stream
                        self._provider_stream_queues.pop(call_id, None)
                    else:
                        logger.debug("AgentAudioDone with no active stream queue", call_id=call_id)
                    self._provider_stream_formats.pop(call_id, None)
                # Log provider segment wall duration
                try:
                    start_ts = self._provider_segment_start_ts.pop(call_id, None)
                    if start_ts is not None:
                        wall = max(0.0, time.time() - float(start_ts))
                        logger.info(
                            "PROVIDER SEGMENT END",
                            call_id=call_id,
                            segment_wall_seconds=round(wall, 3),
                        )
                    # Segment byte accounting summary
                    prov = int(self._provider_bytes.pop(call_id, 0))
                    enq = int(self._enqueued_bytes.pop(call_id, 0))
                    try:
                        ratio = 0.0 if prov <= 0 else (enq / float(prov))
                    except Exception:
                        ratio = 0.0
                    logger.info("PROVIDER SEGMENT BYTES",
                                call_id=call_id,
                                provider_bytes=prov,
                                enqueued_bytes=enq,
                                enqueued_ratio=round(ratio, 3))
                    try:
                        if hasattr(self, 'streaming_playback_manager') and self.streaming_playback_manager:
                            self.streaming_playback_manager.record_provider_bytes(call_id, int(prov))
                    except Exception:
                        logger.debug("Failed to propagate provider_bytes to streaming manager",
                                     call_id=call_id, exc_info=True)
                    # Reset chunk sequence at segment end
                    self._provider_chunk_seq.pop(call_id, None)
                except Exception:
                    pass
                # Experimental: if coalescing buffer exists but stream never started, play or stream it now
                try:
                    coalesce_enabled = bool(getattr(getattr(self.config, 'streaming', {}), 'coalesce_enabled', False))
                except Exception:
                    coalesce_enabled = False
                if coalesce_enabled and call_id in self._provider_coalesce_buf:
                    buf = self._provider_coalesce_buf.pop(call_id, bytearray())
                    try:
                        wire_rate = int(getattr(self.config.streaming, 'sample_rate', 16000))
                    except Exception:
                        wire_rate = 16000
                    try:
                        buf_ms = round((len(buf) / float(2 * max(1, wire_rate))) * 1000.0, 3)
                    except Exception:
                        buf_ms = 0.0
                    micro_fallback_ms = int(getattr(self.config.streaming, 'micro_fallback_ms', 300)) if hasattr(self.config, 'streaming') else 300
                    if buf and buf_ms < micro_fallback_ms:
                        try:
                            playback_id = await self.playback_manager.play_audio(call_id, bytes(buf), "streaming-response")
                            logger.info("MICRO SEGMENT FILE FALLBACK (end)", call_id=call_id, buf_ms=buf_ms, playback_id=playback_id)
                        except Exception:
                            logger.error("File fallback failed at segment end", call_id=call_id, exc_info=True)
                    elif buf:
                        # Stream coalesced buffer now as a short segment
                        try:
                            q2 = asyncio.Queue(maxsize=256)
                            self._provider_stream_queues[call_id] = q2
                            playback_type = "streaming-response"
                            fmt_info = self._provider_stream_formats.get(call_id, {})
                            target_encoding, target_sample_rate, remediation = self._resolve_stream_targets(session, session.provider_name)
                            if target_sample_rate <= 0:
                                target_sample_rate = session.transport_profile.wire_sample_rate
                            await self.streaming_playback_manager.start_streaming_playback(
                                call_id,
                                q2,
                                playback_type=playback_type,
                                source_encoding=fmt_info.get("encoding"),
                                source_sample_rate=fmt_info.get("sample_rate"),
                                target_encoding=target_encoding,
                                target_sample_rate=target_sample_rate,
                            )
                            self._emit_transport_card(
                                call_id,
                                session,
                                source_encoding=fmt_info.get("encoding"),
                                source_sample_rate=fmt_info.get("sample_rate"),
                                target_encoding=target_encoding,
                                target_sample_rate=target_sample_rate,
                            )
                            logger.info("COALESCE START (end)", call_id=call_id, coalesced_ms=buf_ms, coalesced_bytes=len(buf))
                            try:
                                q2.put_nowait(bytes(buf))
                                # Account for the coalesced enqueue at segment end
                                try:
                                    self._enqueued_bytes[call_id] = int(self._enqueued_bytes.get(call_id, 0)) + len(buf)
                                except Exception:
                                    pass
                                q2.put_nowait(None)
                            except asyncio.QueueFull:
                                logger.debug("Coalesced enqueue dropped at end (queue full)", call_id=call_id)
                        except Exception:
                            logger.error("Coalesced streaming failed at segment end", call_id=call_id, exc_info=True)
                
                # Check if hangup was requested after TTS completion
                # Only check when streaming_done is True (complete response ended, not just segment boundary)
                streaming_done = event.get("streaming_done", False)
                if streaming_done:
                    try:
                        session = await self.session_store.get_by_call_id(call_id)
                        if session and getattr(session, 'cleanup_after_tts', False):
                            logger.info("🔚 Cleanup after TTS requested - hanging up call", call_id=call_id)
                            # Give a small delay for audio to finish playing
                            await asyncio.sleep(0.5)
                            try:
                                await self.ari_client.hangup_channel(session.caller_channel_id)
                                logger.info("✅ Call hung up successfully", call_id=call_id, channel_id=session.caller_channel_id)
                            except Exception as e:
                                logger.error("Failed to hang up call", call_id=call_id, error=str(e), exc_info=True)
                    except Exception as e:
                        logger.debug("Error checking cleanup_after_tts flag", call_id=call_id, error=str(e))
            
            elif etype == "HangupReady":
                # Hangup triggered by farewell response completion (Option C implementation)
                # This ensures hangup happens even if farewell response produces no audio
                call_id = event.get("call_id")
                reason = event.get("reason", "unknown")
                had_audio = event.get("had_audio", False)
                
                logger.info(
                    "🔚 HangupReady event received - executing hangup",
                    call_id=call_id,
                    reason=reason,
                    had_audio=had_audio
                )
                
                # Delay to ensure audio completes through RTP pipeline
                # Accounts for: RTP transmission, jitter buffer, and playback
                await asyncio.sleep(1.0)
                
                try:
                    session = await self.session_store.get_by_call_id(call_id)
                    if session:
                        await self.ari_client.hangup_channel(session.caller_channel_id)
                        logger.info(
                            "✅ Call hung up successfully (farewell completed)",
                            call_id=call_id,
                            channel_id=session.caller_channel_id
                        )
                    else:
                        logger.warning("No session found for HangupReady", call_id=call_id)
                except Exception as e:
                    logger.error(
                        "Failed to hangup after farewell",
                        call_id=call_id,
                        error=str(e),
                        exc_info=True
                    )
            
            else:
                # Log control/JSON events at debug for now
                logger.debug("Provider control event", provider_event=event)

        except Exception as exc:
            logger.error("Error handling provider event", error=str(exc), exc_info=True)

    def _as_to_pcm16_16k(self, audio_bytes: bytes) -> bytes:
        """Convert AudioSocket inbound bytes to PCM16 @ 16 kHz for pipeline STT.

        Assumes AudioSocket format is 8 kHz μ-law (default) or PCM16.
        """
        try:
            fmt = None
            try:
                if self.config and getattr(self.config, 'audiosocket', None):
                    fmt = (self.config.audiosocket.format or 'ulaw').lower()
            except Exception:
                fmt = 'ulaw'
            if fmt in ('ulaw', 'mulaw', 'g711_ulaw'):
                pcm8k = audioop.ulaw2lin(audio_bytes, 2)
            else:
                # Treat as PCM16 8 kHz
                pcm8k = audio_bytes
            try:
                # Use pipeline16k resample state under synthetic key 'pipeline'
                state = self._resample_state_pipeline16k.get('pipeline')
                pcm16, state = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, state)
                self._resample_state_pipeline16k['pipeline'] = state
            except Exception:
                pcm16 = pcm8k
            return pcm16
        except Exception:
            logger.debug("AudioSocket -> PCM16 16k conversion failed", exc_info=True)
            return audio_bytes

    async def _ensure_pipeline_runner(self, session: CallSession, *, forced: bool = False) -> None:
        """Create per-call queue and start pipeline runner if not already started."""
        call_id = session.call_id
        if call_id in self._pipeline_tasks:
            if forced:
                self._pipeline_forced[call_id] = True
            return
        # Require orchestrator enabled and a selected pipeline
        if not getattr(self, 'pipeline_orchestrator', None) or not self.pipeline_orchestrator.enabled:
            return
        if not getattr(session, 'pipeline_name', None):
            return
        # Create queue and start task
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._pipeline_queues[call_id] = q
        self._pipeline_forced[call_id] = bool(forced)
        task = asyncio.create_task(self._pipeline_runner(call_id))
        self._pipeline_tasks[call_id] = task
        logger.info("Pipeline runner started", call_id=call_id, pipeline=session.pipeline_name)

    async def _pipeline_runner(self, call_id: str) -> None:
        """Minimal adapter-driven loop: STT -> LLM -> TTS -> file playback.

        Designed to be opt-in (forced via AI_PROVIDER=pipeline_name) to avoid
        impacting the tested ExternalMedia + Local full-agent path.
        """
        try:
            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                return
            pipeline = self.pipeline_orchestrator.get_pipeline(call_id, getattr(session, 'pipeline_name', None))
            if not pipeline:
                logger.debug("Pipeline runner: no pipeline resolved", call_id=call_id)
                return
            # Inject context prompt into LLM options with fallback chain
            # Fallback chain: AI_CONTEXT → pipeline default → global llm_config
            llm_options = pipeline.llm_options or {}
            prompt_source = "pipeline_default"
            try:
                # Priority 1: Check if context has a custom prompt
                # Use session.context_name (persisted string) instead of transport_profile.context (object may not persist)
                context_prompt_injected = False
                context_name = getattr(session, 'context_name', None)
                if context_name:
                    context_config = self.transport_orchestrator.get_context_config(context_name)
                    if context_config and context_config.prompt:
                            # Create a copy to avoid mutating the pipeline's original options
                            llm_options = dict(llm_options)
                            llm_options['system_prompt'] = context_config.prompt
                            prompt_source = "context_injection"
                            context_prompt_injected = True
                            logger.info(
                                "Pipeline LLM prompt resolved from context",
                                call_id=call_id,
                                context=context_name,
                                prompt_length=len(context_config.prompt),
                                prompt_preview=context_config.prompt[:80] + "..." if len(context_config.prompt) > 80 else context_config.prompt,
                            )
                
                # Priority 2: If no context prompt, check if pipeline has default or use global
                if not context_prompt_injected:
                    # Check if system_prompt already in llm_options (pipeline default)
                    if llm_options.get('system_prompt'):
                        prompt_source = "pipeline_default"
                        logger.info(
                            "Pipeline LLM prompt using pipeline default",
                            call_id=call_id,
                            prompt_length=len(llm_options['system_prompt']),
                        )
                    else:
                        # Priority 3: Fall back to global llm_config
                        global_prompt = getattr(self.config.llm, 'prompt', None)
                        if global_prompt:
                            llm_options = dict(llm_options)
                            llm_options['system_prompt'] = global_prompt
                            prompt_source = "global_llm_config"
                            logger.info(
                                "Pipeline LLM prompt resolved from global config",
                                call_id=call_id,
                                prompt_length=len(global_prompt),
                            )
            except Exception as exc:
                logger.error(
                    "Failed to inject context prompt into pipeline LLM options",
                    call_id=call_id,
                    error=str(exc),
                    exc_info=True,
                )
                prompt_source = "error"
            
            # Open per-call state for adapters (best-effort)
            try:
                await pipeline.stt_adapter.open_call(call_id, pipeline.stt_options)
            except Exception:
                logger.debug("STT open_call failed", call_id=call_id, exc_info=True)
            else:
                logger.info("Pipeline STT adapter session opened", call_id=call_id)
            try:
                await pipeline.llm_adapter.open_call(call_id, llm_options)
            except Exception:
                logger.debug("LLM open_call failed", call_id=call_id, exc_info=True)
            else:
                logger.info("Pipeline LLM adapter session opened", call_id=call_id)
            try:
                await pipeline.tts_adapter.open_call(call_id, pipeline.tts_options)
            except Exception:
                logger.debug("TTS open_call failed", call_id=call_id, exc_info=True)
            else:
                logger.info("Pipeline TTS adapter session opened", call_id=call_id)

            # Pipeline-managed initial greeting (optional)
            # Fallback chain: AI_CONTEXT → global llm_config → empty
            greeting = ""
            greeting_source = "none"
            try:
                # Priority 1: Check if context has a custom greeting
                # Use session.context_name (persisted string) instead of transport_profile.context
                context_name = getattr(session, 'context_name', None)
                if context_name:
                    context_config = self.transport_orchestrator.get_context_config(context_name)
                    if context_config and context_config.greeting:
                        greeting = context_config.greeting.strip()
                        greeting_source = "context_injection"
                        logger.info(
                            "Pipeline greeting resolved from context",
                            call_id=call_id,
                            context=context_name,
                            greeting_length=len(greeting),
                        )
                
                # Priority 2: Fall back to global config greeting
                if not greeting:
                    global_greeting = (getattr(self.config.llm, "initial_greeting", None) or "").strip()
                    if global_greeting:
                        greeting = global_greeting
                        greeting_source = "global_llm_config"
                        logger.info(
                            "Pipeline greeting resolved from global config",
                            call_id=call_id,
                            greeting_length=len(greeting),
                        )
                
                # Log if no greeting found
                if not greeting:
                    logger.info(
                        "Pipeline greeting not configured (no greeting will be played)",
                        call_id=call_id,
                    )
            except Exception as exc:
                logger.error(
                    "Pipeline greeting resolution failed",
                    call_id=call_id,
                    error=str(exc),
                    exc_info=True,
                )
                greeting = ""
                greeting_source = "error"
            
            # Apply template substitution for personalized greetings
            if greeting:
                try:
                    caller_name = getattr(session, 'caller_name', None) or "there"
                    caller_number = getattr(session, 'caller_number', None) or "unknown"
                    greeting = greeting.format(
                        caller_name=caller_name,
                        caller_number=caller_number
                    )
                    logger.debug(
                        "Applied greeting template substitution",
                        call_id=call_id,
                        caller_name=caller_name,
                        greeting_length=len(greeting)
                    )
                except KeyError as e:
                    logger.warning(
                        "Greeting template has invalid placeholder",
                        call_id=call_id,
                        error=str(e)
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to apply greeting template substitution",
                        call_id=call_id,
                        error=str(e)
                    )
            
            if greeting:
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    try:
                        tts_bytes = bytearray()
                        async for chunk in pipeline.tts_adapter.synthesize(call_id, greeting, pipeline.tts_options):
                            if chunk:
                                tts_bytes.extend(chunk)
                        if not tts_bytes:
                            logger.warning(
                                "Pipeline greeting produced no audio",
                                call_id=call_id,
                                attempt=attempt,
                            )
                        else:
                            logger.info("DEBUG: About to play audio", call_id=call_id)
                            await self.playback_manager.play_audio(call_id, bytes(tts_bytes), "pipeline-tts-greeting")
                            
                            # AAVA-85: Persist greeting to session history so it appears in email summary
                            try:
                                logger.info("DEBUG: PERSISTING GREETING START", call_id=call_id, greeting_len=len(greeting))
                                session.conversation_history.append({"role": "assistant", "content": greeting})
                                await self.session_store.upsert_call(session)
                                logger.info("DEBUG: PERSISTING GREETING DONE", call_id=call_id)
                                logger.info("Persisted initial greeting to session history", call_id=call_id)
                            except Exception as e:
                                logger.error("DEBUG: PERSISTING GREETING FAILED", call_id=call_id, error=str(e))
                                logger.warning("Failed to persist greeting history", call_id=call_id, error=str(e))
                                
                        break
                    except RuntimeError as exc:
                        error_text = str(exc).lower()
                        if attempt < max_attempts and "session" in error_text:
                            logger.debug(
                                "Pipeline greeting retry after session error",
                                call_id=call_id,
                                attempt=attempt,
                                exc_info=True,
                            )
                            try:
                                await pipeline.tts_adapter.open_call(call_id, pipeline.tts_options)
                                continue
                            except Exception:
                                logger.debug(
                                    "Pipeline greeting re-open_call failed",
                                    call_id=call_id,
                                    attempt=attempt,
                                    exc_info=True,
                                )
                        logger.error(
                            "Pipeline greeting synthesis failed",
                            call_id=call_id,
                            attempt=attempt,
                            error=str(exc),
                            exc_info=True,
                        )
                        break
                    except Exception:
                        logger.error(
                            "Pipeline greeting unexpected failure",
                            call_id=call_id,
                            attempt=attempt,
                            exc_info=True,
                        )
                        break

            # Accumulate into ~160ms chunks for STT while keeping ingestion responsive
            bytes_per_ms = 32  # 16k Hz * 2 bytes / 1000 ms
            base_commit_ms = 160
            stt_chunk_ms = int(pipeline.stt_options.get("chunk_ms", base_commit_ms)) if pipeline.stt_options else base_commit_ms
            commit_ms = max(stt_chunk_ms, 80)
            commit_bytes = bytes_per_ms * commit_ms

            inbound_queue = self._pipeline_queues.get(call_id)
            if not inbound_queue:
                return

            buffer_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=200)
            transcript_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=8)

            use_streaming = bool((pipeline.stt_options or {}).get("streaming", False))
            if use_streaming:
                streaming_supported = all(
                    hasattr(pipeline.stt_adapter, attr)
                    for attr in ("start_stream", "send_audio", "iter_results", "stop_stream")
                )
                if not streaming_supported:
                    logger.warning(
                        "Streaming STT requested but adapter does not support streaming APIs; falling back to chunked mode",
                        call_id=call_id,
                        component=getattr(pipeline.stt_adapter, "component_key", "unknown"),
                    )
                    use_streaming = False
            stream_format = (pipeline.stt_options or {}).get("stream_format", "pcm16_16k")
            if use_streaming:
                try:
                    logger.info(
                        "Streaming STT enabled",
                        call_id=call_id,
                        commit_ms=commit_ms,
                        stream_format=stream_format,
                        buffer_max=getattr(buffer_queue, "_maxsize", 200) if hasattr(buffer_queue, "_maxsize") else 200,
                    )
                except Exception:
                    logger.debug("Streaming STT info log failed", exc_info=True)

            async def enqueue_buffer(item: Optional[bytes]) -> None:
                if item is None:
                    await buffer_queue.put(None)
                    return
                while True:
                    if buffer_queue.full():
                        dropped = await buffer_queue.get()
                        if dropped is not None:
                            logger.debug(
                                "Pipeline audio buffer overflow; dropping oldest frame",
                                call_id=call_id,
                            )
                        continue
                    await buffer_queue.put(item)
                    return

            async def ingest_audio() -> None:
                try:
                    while True:
                        chunk = await inbound_queue.get()
                        if chunk is None:
                            await enqueue_buffer(None)
                            break
                        await enqueue_buffer(chunk)
                except asyncio.CancelledError:
                    pass

            if not use_streaming:

                async def process_audio(audio_chunk: bytes) -> None:
                    transcript = ""
                    try:
                        transcript = await pipeline.stt_adapter.transcribe(
                            call_id,
                            audio_chunk,
                            16000,
                            pipeline.stt_options,
                        )
                    except Exception:
                        logger.debug("STT transcribe failed", call_id=call_id, exc_info=True)
                        return
                    transcript = (transcript or "").strip()
                    if not transcript:
                        return
                    # Record time when a final transcript is obtained
                    try:
                        self._last_transcript_ts[call_id] = time.time()
                    except Exception:
                        pass
                    try:
                        transcript_queue.put_nowait(transcript)
                    except asyncio.QueueFull:
                        try:
                            dropped = transcript_queue.get_nowait()
                            logger.warning(
                                "Pipeline transcript backlog full; dropping oldest transcript",
                                call_id=call_id,
                                dropped_preview=(dropped or "")[:80] if dropped else "",
                            )
                        except asyncio.QueueEmpty:
                            pass
                        await transcript_queue.put(transcript)

                async def stt_worker() -> None:
                    local_buf = bytearray()
                    try:
                        while True:
                            frame = await buffer_queue.get()
                            if frame is None:
                                if local_buf:
                                    await process_audio(bytes(local_buf))
                                await transcript_queue.put(None)
                                break
                            local_buf.extend(frame)
                            if len(local_buf) < commit_bytes:
                                continue
                            await process_audio(bytes(local_buf))
                            local_buf.clear()
                    except asyncio.CancelledError:
                        pass

            else:

                async def stt_sender() -> None:
                    local_buf = bytearray()
                    try:
                        while True:
                            frame = await buffer_queue.get()
                            if frame is None:
                                if local_buf:
                                    try:
                                        await pipeline.stt_adapter.send_audio(
                                            call_id,
                                            bytes(local_buf),
                                            fmt=stream_format,
                                        )
                                    except Exception:
                                        logger.debug(
                                            "Streaming STT final send failed",
                                            call_id=call_id,
                                            exc_info=True,
                                        )
                                    local_buf.clear()
                                break
                            local_buf.extend(frame)
                            if len(local_buf) < commit_bytes:
                                continue
                            chunk = bytes(local_buf)
                            local_buf.clear()
                            try:
                                await pipeline.stt_adapter.send_audio(
                                    call_id,
                                    chunk,
                                    fmt=stream_format,
                                )
                            except Exception:
                                logger.debug(
                                    "Streaming STT send failed",
                                    call_id=call_id,
                                    exc_info=True,
                                )
                    except asyncio.CancelledError:
                        pass

                async def stt_receiver() -> None:
                    try:
                        async for final in pipeline.stt_adapter.iter_results(call_id):
                            try:
                                # Record time when a final transcript arrives
                                self._last_transcript_ts[call_id] = time.time()
                                transcript_queue.put_nowait(final)
                            except asyncio.QueueFull:
                                try:
                                    transcript_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                await transcript_queue.put(final)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.debug(
                            "Streaming STT receive loop error",
                            call_id=call_id,
                            exc_info=True,
                        )
                    finally:
                        try:
                            transcript_queue.put_nowait(None)
                        except asyncio.QueueFull:
                            pass

            async def dialog_worker() -> None:
                pending_segments: List[str] = []
                flush_task: Optional[asyncio.Task] = None
                accumulation_timeout = float(
                    (pipeline.llm_options or {}).get("aggregation_timeout_sec", 2.0)
                )
                # Track conversation history to include prior messages
                # AAVA-85 FIX: Initialize from session to preserve greeting
                conversation_history: List[Dict[str, str]] = list(session.conversation_history or [])

                async def cancel_flush() -> None:
                    nonlocal flush_task
                    if flush_task and not flush_task.done():
                        current = asyncio.current_task()
                        if flush_task is not current:
                            flush_task.cancel()
                    flush_task = None

                async def run_turn(transcript_text: str) -> None:
                    nonlocal conversation_history
                    response_text = ""
                    tool_calls = []
                    
                    pipeline_label = getattr(session, 'pipeline_name', None) or 'none'
                    provider_label = getattr(session, 'provider_name', None) or 'unknown'
                    t_start = self._last_transcript_ts.get(call_id)
                    
                    # Build context with conversation history
                    # System prompt only in first turn (when history is empty)
                    context_for_llm = {"prior_messages": list(conversation_history)}
                    
                    try:
                        llm_result = await pipeline.llm_adapter.generate(
                            call_id,
                            transcript_text,
                            context_for_llm,  # Include conversation history
                            llm_options,  # Use context-injected options (includes system_prompt)
                        )
                    except Exception:
                        logger.debug("LLM generate failed", call_id=call_id, exc_info=True)
                        return

                    # Milestone7: Handle structured LLM response with tool calls
                    # from src.pipelines.base import LLMResponse  # Moved to top-level
                    
                    logger.info("DEBUG: LLM Result Type", type=str(type(llm_result)), tool_calls_len=len(getattr(llm_result, 'tool_calls', [])), is_llm_response=isinstance(llm_result, LLMResponse), call_id=call_id)
                    
                    if isinstance(llm_result, LLMResponse):
                        response_text = (llm_result.text or "").strip()
                        tool_calls = llm_result.tool_calls
                    else:
                        response_text = (str(llm_result) or "").strip()
                        tool_calls = []

                    if not response_text and not tool_calls:
                        return
                    
                    # Update conversation history
                    conversation_history.append({"role": "user", "content": transcript_text})
                    if response_text:
                        conversation_history.append({"role": "assistant", "content": response_text})
                    elif tool_calls:
                        conversation_history.append({"role": "assistant", "content": "(tool execution)"})
                    
                    # AAVA-85: Persist session history so tools (email) can access it
                    session.conversation_history = list(conversation_history)
                    await self.session_store.upsert_call(session)
                    
                    logger.info("DEBUG: Post-session-upsert", has_response_text=bool(response_text), has_tool_calls=bool(tool_calls), tool_calls_count=len(tool_calls), call_id=call_id)

                    playback_id = None
                    
                    # 1. Synthesize and Play Text (if any)
                    logger.info("DEBUG: Before TTS block", response_text_len=len(response_text), will_skip_tts=not bool(response_text), call_id=call_id)
                    if response_text:
                        tts_bytes = bytearray()
                        first_tts_ts: Optional[float] = None
                        try:
                            async for tts_chunk in pipeline.tts_adapter.synthesize(
                                call_id,
                                response_text,
                                pipeline.tts_options,
                            ):
                                if tts_chunk:
                                    if first_tts_ts is None:
                                        first_tts_ts = time.time()
                                        try:
                                            if t_start is not None:
                                                _TURN_STT_TO_TTS.labels(pipeline_label, provider_label).observe(max(0.0, first_tts_ts - t_start))
                                        except Exception:
                                            pass
                                    tts_bytes.extend(tts_chunk)
                        except Exception:
                            logger.debug("TTS synth failed", call_id=call_id, exc_info=True)
                            # If TTS fails but we have tools, continue to tools
                            if not tool_calls:
                                return
                        
                        if tts_bytes:
                            try:
                                playback_id = await self.playback_manager.play_audio(
                                    call_id,
                                    bytes(tts_bytes),
                                    "pipeline-tts",
                                )
                                try:
                                    if playback_id and t_start is not None:
                                        _TURN_RESPONSE_SECONDS.labels(pipeline_label, provider_label).observe(max(0.0, time.time() - t_start))
                                except Exception:
                                    pass
                                if not playback_id:
                                    logger.error(
                                        "Pipeline playback failed",
                                        call_id=call_id,
                                        size=len(tts_bytes),
                                    )
                            except Exception:
                                logger.error("Pipeline playback exception", call_id=call_id, exc_info=True)

                    # 2. Execute Tools (if any)
                    logger.info("DEBUG: Reached tool execution block", tool_calls_present=bool(tool_calls), tool_calls_count=len(tool_calls), tool_calls_type=str(type(tool_calls)), call_id=call_id)
                    if tool_calls:
                        logger.info("DEBUG: Inside tool_calls block", tool_calls_count=len(tool_calls), playback_id=playback_id, call_id=call_id)
                        # Wait for playback to finish before executing tools (especially transfer/hangup)
                        if playback_id:
                            try:
                                # Best effort wait to let user hear the response
                                # In a real implementation, we'd subscribe to PlaybackFinished
                                await asyncio.sleep(len(response_text) * 0.08) # Rough estimate 
                            except Exception:
                                pass

                        logger.info("DEBUG: Before import ToolExecutionContext", call_id=call_id)
                        from src.tools.context import ToolExecutionContext
                        from src.tools.registry import tool_registry
                        logger.info("DEBUG: After imports, before creating context", call_id=call_id)
                        
                        # Create execution context
                        try:
                            logger.info("DEBUG: About to create ToolExecutionContext", call_id=call_id)
                            tool_ctx = ToolExecutionContext(
                                call_id=call_id,
                                caller_channel_id=getattr(session, 'channel_id', call_id),
                                session_store=self.session_store,
                                ari_client=self.ari_client,
                                config=self.config.dict(),
                                provider_name="pipeline"
                            )
                            logger.info("DEBUG: ToolExecutionContext created successfully", call_id=call_id)
                        except Exception as ctx_error:
                            logger.error("DEBUG: ToolExecutionContext creation FAILED", call_id=call_id, error=str(ctx_error), exc_info=True)
                            raise

                        logger.info("DEBUG: Before for loop", tool_calls_len=len(tool_calls), call_id=call_id)
                        for tool_call in tool_calls:
                            try:
                                logger.info("DEBUG: Inside for loop iteration", tool_call=tool_call, call_id=call_id)
                                name = tool_call.get("name")
                                args = tool_call.get("parameters") or {}
                                tool = tool_registry.get(name)
                                
                                logger.info("DEBUG: Processing tool call", name=name, args=args, tool_found=bool(tool), call_id=call_id)
                                
                                if tool:
                                    logger.info("Executing pipeline tool", tool=name, call_id=call_id)
                                    result = await tool.execute(args, tool_ctx)
                                    logger.info("Tool execution result", tool=name, result=result)
                                    
                                    # Handle Hangup (AAVA-85 Fix)
                                    if result.get("will_hangup"):
                                        farewell = result.get("message")
                                        if farewell:
                                            # Add farewell to conversation history for email
                                            conversation_history.append({"role": "assistant", "content": farewell})
                                            session.conversation_history = list(conversation_history)
                                            await self.session_store.upsert_call(session)
                                            logger.info("Farewell added to conversation history", call_id=call_id)
                                            
                                            # Speak farewell
                                            try:
                                                # Re-use TTS synthesis for farewell
                                                fw_bytes = bytearray()
                                                async for chunk in pipeline.tts_adapter.synthesize(call_id, farewell, pipeline.tts_options):
                                                    fw_bytes.extend(chunk)
                                                if fw_bytes:
                                                    pid = await self.playback_manager.play_audio(call_id, bytes(fw_bytes), "pipeline-farewell")
                                                    # Calculate actual duration: mulaw 8kHz = 8000 bytes/sec
                                                    duration_sec = len(fw_bytes) / 8000.0
                                                    # Wait for farewell + small buffer to ensure completion
                                                    await asyncio.sleep(duration_sec + 0.5)
                                                    logger.info("Farewell playback completed", duration_sec=duration_sec, call_id=call_id)
                                            except Exception as e:
                                                logger.error("Farewell TTS failed", error=str(e))
                                        
                                        logger.info("Executing explicit hangup via ARI", call_id=call_id)
                                        try:
                                            channel_id = getattr(session, 'channel_id', call_id)
                                            await self.ari_client.hangup_channel(channel_id)
                                        except Exception as e:
                                            logger.error("ARI hangup failed", error=str(e))
                                        return

                                    # Handle Terminal Transfer
                                    if name in ["transfer"] and result.get("status") == "success":
                                        logger.info("Transfer successful, ending turn loop", tool=name)
                                        return
                                else:
                                    logger.warning("Tool not found", tool=name)
                            except Exception as e:
                                logger.error("Tool execution failed", tool=name, error=str(e), exc_info=True)

                async def maybe_respond(force: bool, from_flush: bool = False) -> None:
                    nonlocal pending_segments, flush_task
                    if not pending_segments:
                        if from_flush:
                            flush_task = None
                        else:
                            await cancel_flush()
                        return
                    aggregated = " ".join(pending_segments).strip()
                    if not aggregated:
                        pending_segments.clear()
                        if from_flush:
                            flush_task = None
                        else:
                            await cancel_flush()
                        return
                    words = len([w for w in aggregated.split() if w])
                    chars = len(aggregated.replace(" ", ""))
                    threshold_met = words >= 3 or chars >= 12
                    if not threshold_met:
                        if force:
                            pending_segments.clear()
                            if from_flush:
                                flush_task = None
                            else:
                                await cancel_flush()
                        else:
                            logger.debug(
                                "Accumulating transcript before LLM",
                                call_id=call_id,
                                preview=aggregated[:80],
                                chars=chars,
                                words=words,
                            )
                        return
                    if from_flush:
                        flush_task = None
                    else:
                        await cancel_flush()
                    await run_turn(aggregated)
                    pending_segments.clear()

                async def schedule_flush() -> None:
                    nonlocal flush_task
                    await cancel_flush()

                    async def _flush() -> None:
                        try:
                            await asyncio.sleep(accumulation_timeout)
                            await maybe_respond(force=True, from_flush=True)
                        except asyncio.CancelledError:
                            pass

                    flush_task = asyncio.create_task(_flush())

                try:
                    while True:
                        transcript = await transcript_queue.get()
                        if transcript is None:
                            await maybe_respond(force=True)
                            break
                        normalized = (transcript or "").strip()
                        if not normalized:
                            if pending_segments and flush_task is None:
                                await schedule_flush()
                            continue
                        pending_segments.append(normalized)
                        await maybe_respond(force=False)
                        if pending_segments:
                            await schedule_flush()
                except asyncio.CancelledError:
                    pass
                finally:
                    await cancel_flush()

            ingest_task = asyncio.create_task(ingest_audio())

            if use_streaming:
                stt_send_task: Optional[asyncio.Task] = None
                stt_recv_task: Optional[asyncio.Task] = None
                dialog_task: Optional[asyncio.Task] = None
                stop_called = False

                try:
                    await pipeline.stt_adapter.start_stream(call_id, pipeline.stt_options or {})
                    stt_send_task = asyncio.create_task(stt_sender())
                    stt_recv_task = asyncio.create_task(stt_receiver())
                    dialog_task = asyncio.create_task(dialog_worker())

                    if stt_send_task:
                        await stt_send_task
                    await pipeline.stt_adapter.stop_stream(call_id)
                    stop_called = True
                    await asyncio.gather(
                        *(task for task in (stt_recv_task, dialog_task) if task is not None),
                        return_exceptions=True,
                    )
                finally:
                    ingest_task.cancel()
                    tasks_to_cancel = []
                    for task in (stt_send_task, stt_recv_task, dialog_task):
                        if task and not task.done():
                            task.cancel()
                            tasks_to_cancel.append(task)
                    await asyncio.gather(ingest_task, *tasks_to_cancel, return_exceptions=True)
                    if not stop_called:
                        await pipeline.stt_adapter.stop_stream(call_id)
            else:
                stt_task = asyncio.create_task(stt_worker())
                dialog_task = asyncio.create_task(dialog_worker())

                try:
                    await dialog_task
                finally:
                    ingest_task.cancel()
                    stt_task.cancel()
                    await asyncio.gather(ingest_task, stt_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Pipeline runner crashed", call_id=call_id, exc_info=True)

    async def _hydrate_transport_from_dialplan(self, session: CallSession, channel_id: str) -> None:
        """Load transport hints (format/rate) provided by the dialplan via channel vars."""
        fmt_token: Optional[str] = None
        rate_value: Optional[int] = None

        # Fetch AI_TRANSPORT_FORMAT (e.g., ulaw, slin16)
        try:
            resp = await self.ari_client.send_command(
                "GET",
                f"channels/{channel_id}/variable",
                params={"variable": "AI_TRANSPORT_FORMAT"},
            )
            if isinstance(resp, dict):
                value = (resp.get("value") or "").strip()
                if value:
                    fmt_token = value
        except Exception:
            logger.debug(
                "Dialplan transport format fetch failed",
                call_id=channel_id,
                exc_info=True,
            )

        # Fetch AI_TRANSPORT_RATE (integer Hz)
        try:
            resp = await self.ari_client.send_command(
                "GET",
                f"channels/{channel_id}/variable",
                params={"variable": "AI_TRANSPORT_RATE"},
            )
            if isinstance(resp, dict):
                raw = (resp.get("value") or "").strip()
                if raw:
                    rate_value = int(float(raw))
        except Exception:
            logger.debug(
                "Dialplan transport rate fetch failed",
                call_id=channel_id,
                exc_info=True,
            )

        if fmt_token is None and rate_value is None:
            return

        canonical_fmt: Optional[str] = None
        if fmt_token is not None:
            canonical_fmt = self._canonicalize_encoding(fmt_token)
            if canonical_fmt:
                session.caller_audio_format = canonical_fmt

        if rate_value is not None and rate_value > 0:
            session.caller_sample_rate = rate_value
        else:
            rate_value = None

        await self._update_transport_profile(
            session,
            fmt=canonical_fmt,
            sample_rate=rate_value,
            source="dialplan",
        )

        try:
            logger.info(
                "Hydrated transport profile from dialplan",
                call_id=session.call_id,
                transport_format=canonical_fmt,
                transport_rate=rate_value,
            )
        except Exception:
            pass

    async def _detect_caller_codec(self, session: CallSession, channel_id: str) -> None:
        """Inspect the caller channel via ARI and record its audio format/sample-rate."""
        preferred_fmt: Optional[str] = None
        variables = (
            "CHANNEL(audionativeformat)",
            "CHANNEL(audioreadformat)",
        )

        for variable in variables:
            try:
                resp = await self.ari_client.send_command(
                    "GET",
                    f"channels/{channel_id}/variable",
                    params={"variable": variable},
                )
            except Exception:
                logger.debug("Codec variable fetch failed", call_id=channel_id, variable=variable, exc_info=True)
                continue

            if isinstance(resp, dict):
                value = (resp.get("value") or "").strip()
                if value:
                    preferred_fmt = value
                    break

        canonical_fmt, sample_rate, reported = self._normalize_audio_format(preferred_fmt)

        await self._update_transport_profile(
            session,
            fmt=canonical_fmt,
            sample_rate=sample_rate,
            source="detected",
        )

        try:
            logger.info(
                "Detected caller codec",
                call_id=session.call_id,
                reported_format=reported,
                normalized_format=canonical_fmt,
                sample_rate=sample_rate,
            )
        except Exception:
            pass

    @staticmethod
    def _normalize_audio_format(raw_format: Optional[str]) -> Tuple[str, int, str]:
        """Map assorted codec tokens to canonical AudioSocket format + sample rate."""
        reported = (raw_format or "").strip()
        token = reported.lower()

        alias_map = {
            "mulaw": "ulaw",
            "mu-law": "ulaw",
            "g711_ulaw": "ulaw",
            "g711ulaw": "ulaw",
            "g711-ula": "ulaw",
            "g711_alaw": "alaw",
            "g711alaw": "alaw",
            "slin": "slin16",
            "slin12": "slin16",
            "slin16": "slin16",
            "linear16": "slin16",
            "pcm16": "slin16",
            "g722": "slin16",
        }

        canonical = alias_map.get(token, token if token else "ulaw")

        # We only stream μ-law or PCM16 internally; fall back to μ-law for others (e.g. alaw).
        if canonical not in {"ulaw", "slin16"}:
            canonical = "ulaw"

        sample_map = {
            "ulaw": 8000,
            "slin16": 16000,
        }
        sample_rate = sample_map.get(canonical, 8000)

        # If the original token hinted at 8 kHz PCM, honor it.
        if canonical == "slin16" and token in {"slin", "slin8"}:
            sample_rate = 8000

        return canonical, sample_rate, reported

    async def _resolve_audio_profile(self, session: CallSession, channel_id: str) -> None:
        """
        P1: Resolve audio profile using TransportOrchestrator.
        
        Reads channel variables (AI_PROVIDER, AI_AUDIO_PROFILE, AI_CONTEXT),
        negotiates with provider capabilities, and applies resolved transport to session.
        """
        # Read channel variables
        channel_vars = {}
        for var_name in ['AI_PROVIDER', 'AI_AUDIO_PROFILE', 'AI_CONTEXT']:
            try:
                resp = await self.ari_client.send_command(
                    "GET",
                    f"channels/{channel_id}/variable",
                    params={"variable": var_name},
                    tolerate_statuses=[404],  # 404 is expected when variable not set
                )
                if isinstance(resp, dict):
                    value = (resp.get("value") or "").strip()
                    if value:
                        channel_vars[var_name] = value
                        logger.debug(
                            f"Channel variable {var_name} read",
                            channel_id=channel_id,
                            variable=var_name,
                            value=value,
                        )
                    else:
                        logger.info(
                            f"Channel variable {var_name} not set (using defaults)",
                            channel_id=channel_id,
                            variable=var_name,
                        )
            except Exception as exc:
                # 404 is expected when variable not set - log as info, not error
                if "404" in str(exc) or "not found" in str(exc).lower():
                    logger.info(
                        f"Channel variable {var_name} not set (using defaults)",
                        channel_id=channel_id,
                        variable=var_name,
                    )
                else:
                    logger.debug(
                        f"Failed to read channel variable {var_name}",
                        channel_id=channel_id,
                        variable=var_name,
                        error=str(exc),
                        exc_info=True,
                    )
        
        # CRITICAL: Store context_name FIRST, before any early returns
        # This ensures pipeline mode gets the context even if provider lookup fails
        session.context_name = channel_vars.get('AI_CONTEXT')
        await self._save_session(session)
        logger.debug(
            "Stored context_name in session",
            call_id=session.call_id,
            context_name=session.context_name,
        )
        
        # Get provider name (precedence: AI_PROVIDER > context > session.provider_name)
        provider_name = channel_vars.get('AI_PROVIDER')
        if not provider_name:
            # Check if context specifies provider
            context_name = channel_vars.get('AI_CONTEXT')
            if context_name:
                context_config = self.transport_orchestrator.get_context_config(context_name)
                if context_config and context_config.provider:
                    provider_name = context_config.provider
        
        if not provider_name:
            provider_name = session.provider_name or self.config.default_provider
        
        # Get provider instance
        provider = self.providers.get(provider_name)
        if not provider:
            logger.warning(
                "Provider not found for audio profile resolution (pipeline mode will use context_name)",
                call_id=session.call_id,
                provider=provider_name,
                available=list(self.providers.keys()),
                context_name=session.context_name,
            )
            return
        
        # Get provider capabilities
        provider_caps = None
        try:
            if hasattr(provider, 'get_capabilities'):
                provider_caps = provider.get_capabilities()
        except Exception as exc:
            logger.debug(
                "Failed to get provider capabilities",
                call_id=session.call_id,
                provider=provider_name,
                error=str(exc),
            )
        
        # Resolve transport profile
        try:
            # Pass provider config so orchestrator can read actual provider requirements
            provider_cfg = getattr(provider, "config", None) if provider else None
            transport = self.transport_orchestrator.resolve_transport(
                provider_name=provider_name,
                provider_caps=provider_caps,
                channel_vars=channel_vars,
                provider_config=provider_cfg,
            )
            
            # Store transport in session (keep as object, not dict, for legacy code compatibility)
            session.transport_profile = transport
            
            # Note: context_name already stored earlier (before provider lookup)
            # so pipeline mode gets it even if provider not found
            
            await self._save_session(session)
            
            # Apply to streaming manager
            # CRITICAL: Do NOT set global sample_rate - it's shared across all calls!
            # Each call must pass target_sample_rate explicitly to start_streaming_playback()
            try:
                self.streaming_playback_manager.audiosocket_format = transport.wire_encoding
                # REMOVED: self.streaming_playback_manager.sample_rate = transport.wire_sample_rate
                # Global sample_rate causes race condition when multiple calls use different rates
                if hasattr(self.streaming_playback_manager, 'chunk_size_ms'):
                    self.streaming_playback_manager.chunk_size_ms = transport.chunk_ms
                if hasattr(self.streaming_playback_manager, 'idle_cutoff_ms'):
                    self.streaming_playback_manager.idle_cutoff_ms = transport.idle_cutoff_ms
            except Exception as exc:
                logger.warning(
                    "Failed to apply transport to streaming manager",
                    call_id=session.call_id,
                    error=str(exc),
                )
            
            # CRITICAL FIX: Apply wire format settings to provider's target config
            # The provider needs to emit the same format/rate that AudioSocket expects
            # BUT: P1 continuous_input providers (OpenAI Realtime, Deepgram Voice Agent)
            # handle their own internal format needs and should NOT be overridden
            try:
                provider = self.providers.get(provider_name)
                if provider and hasattr(provider, 'config'):
                    # Check if this is a continuous_input provider using capabilities
                    is_continuous_input = False
                    try:
                        capabilities = None
                        if hasattr(provider, 'get_capabilities'):
                            try:
                                capabilities = provider.get_capabilities()
                            except Exception:
                                pass
                        
                        if capabilities and capabilities.requires_continuous_audio:
                            is_continuous_input = True
                        else:
                            # Fallback for legacy providers
                            pcfg = getattr(provider, 'config', None)
                            if isinstance(pcfg, dict):
                                is_continuous_input = bool(pcfg.get('continuous_input', False))
                            else:
                                is_continuous_input = bool(getattr(pcfg, 'continuous_input', False))
                    except Exception:
                        is_continuous_input = False
                    
                    if not is_continuous_input:
                        # Only update target config for P2/hybrid providers
                        # Wire format = what AudioSocket channel expects = what provider should emit
                        provider.config.target_encoding = transport.wire_encoding
                        provider.config.target_sample_rate_hz = transport.wire_sample_rate
                        logger.info(
                            "Applied wire format to provider target config",
                            call_id=session.call_id,
                            provider=provider_name,
                            target_encoding=transport.wire_encoding,
                            target_sample_rate_hz=transport.wire_sample_rate,
                        )
                    else:
                        logger.debug(
                            "Skipping wire format override for continuous_input provider",
                            call_id=session.call_id,
                            provider=provider_name,
                            reason="P1 provider manages its own format needs"
                        )
            except Exception as exc:
                logger.warning(
                    "Failed to apply transport settings to provider config",
                    call_id=session.call_id,
                    provider=provider_name,
                    error=str(exc),
                )
            
            # Get context config for prompt/greeting and apply to provider
            context_config = None
            logger.debug(
                "DEBUG: Checking context config",
                call_id=session.call_id,
                transport_context=transport.context if hasattr(transport, 'context') else None,
            )
            if transport.context:
                context_config = self.transport_orchestrator.get_context_config(transport.context)
                logger.debug(
                    "DEBUG: Context config loaded",
                    call_id=session.call_id,
                    context=transport.context,
                    has_config=context_config is not None,
                    has_greeting=context_config.greeting if context_config else None,
                    has_prompt=context_config.prompt if context_config else None,
                )
                if context_config:
                    # Inject context greeting/prompt into provider config NOW (not later)
                    try:
                        # Apply template substitution for personalized greetings
                        greeting_to_apply = context_config.greeting
                        if greeting_to_apply:
                            try:
                                caller_name = getattr(session, 'caller_name', None) or "there"
                                caller_number = getattr(session, 'caller_number', None) or "unknown"
                                greeting_to_apply = greeting_to_apply.format(
                                    caller_name=caller_name,
                                    caller_number=caller_number
                                )
                                logger.debug(
                                    "Applied greeting template substitution for provider",
                                    call_id=session.call_id,
                                    caller_name=caller_name
                                )
                            except (KeyError, ValueError) as e:
                                logger.warning(
                                    "Greeting template substitution failed for provider",
                                    call_id=session.call_id,
                                    error=str(e)
                                )
                        
                        if isinstance(provider.config, dict):
                            if greeting_to_apply:
                                provider.config['greeting'] = greeting_to_apply
                                logger.info(
                                    "Applied context greeting to provider",
                                    call_id=session.call_id,
                                    context=transport.context,
                                    greeting_preview=greeting_to_apply[:50] + "...",
                                )
                            if context_config.prompt:
                                provider.config['prompt'] = context_config.prompt
                                logger.info(
                                    "Applied context prompt to provider",
                                    call_id=session.call_id,
                                    context=transport.context,
                                    prompt_length=len(context_config.prompt),
                                )
                        elif hasattr(provider.config, '__dict__'):
                            if greeting_to_apply and hasattr(provider.config, 'greeting'):
                                setattr(provider.config, 'greeting', greeting_to_apply)
                                logger.info(
                                    "Applied context greeting to provider",
                                    call_id=session.call_id,
                                    context=transport.context,
                                    greeting_preview=greeting_to_apply[:50] + "...",
                                )
                            if context_config.prompt:
                                # Try 'prompt' field first, then 'instructions' (OpenAI uses this)
                                if hasattr(provider.config, 'prompt'):
                                    setattr(provider.config, 'prompt', context_config.prompt)
                                    logger.info(
                                        "Applied context prompt to provider",
                                        call_id=session.call_id,
                                        context=transport.context,
                                        prompt_length=len(context_config.prompt),
                                    )
                                elif hasattr(provider.config, 'instructions'):
                                    setattr(provider.config, 'instructions', context_config.prompt)
                                    logger.info(
                                        "Applied context prompt to provider (as instructions)",
                                        call_id=session.call_id,
                                        context=transport.context,
                                        prompt_length=len(context_config.prompt),
                                    )
                                else:
                                    logger.debug(
                                        "Provider config does not support prompt or instructions field",
                                        call_id=session.call_id,
                                        provider=provider_name,
                                        context=transport.context,
                                    )
                    except Exception as exc:
                        logger.error(
                            "Failed to apply context config to provider",
                            call_id=session.call_id,
                            context=transport.context,
                            error=str(exc),
                            exc_info=True,
                        )
            
            # Note: TransportCard will be emitted by legacy code path
            
            logger.info(
                "Audio profile resolved and applied",
                call_id=session.call_id,
                profile=transport.profile_name,
                provider=provider_name,
                context=transport.context,
                wire_format=f"{transport.wire_encoding}@{transport.wire_sample_rate}Hz",
            )
            
        except Exception as exc:
            logger.error(
                "Audio profile resolution failed",
                call_id=session.call_id,
                provider=provider_name,
                error=str(exc),
                exc_info=True,
            )
    
    @staticmethod
    def _canonicalize_encoding(value: Optional[str]) -> str:
        """Normalize codec tokens to canonical engine values."""
        if not value:
            return ""
        token = value.lower().strip()
        mapping = {
            "mu-law": "ulaw",
            "mulaw": "ulaw",
            "g711_ulaw": "ulaw",
            "g711ulaw": "ulaw",
            "g711-ula": "ulaw",
            # Note: "slin" (8kHz PCM) and "slin16" (16kHz PCM) are distinct formats
            "slin": "slin",
            "slin12": "slin16",
            "slin16": "slin16",
        }
        return mapping.get(token, token)

    @staticmethod
    def _should_force_mulaw(force_flag: bool, audiosocket_fmt: Optional[str]) -> bool:
        """Gate egress μ-law forcing to transports that actually expect μ-law frames."""
        if not force_flag:
            return False
        canonical = Engine._canonicalize_encoding(audiosocket_fmt)
        if canonical in ("", "ulaw", "mulaw", "g711_ulaw", "mu-law"):
            return True
        try:
            logger.info(
                "Disabling egress_force_mulaw for non-μ-law AudioSocket transport",
                audiosocket_format=audiosocket_fmt,
            )
        except Exception:
            pass
        return False

    @staticmethod
    def _infer_transport_from_frame(frame_len: int) -> Tuple[str, int]:
        """Infer transport format/sample-rate from canonical frame lengths."""
        mapping = {
            160: ("ulaw", 8000),   # 20ms @8k μ-law
            320: ("slin16", 8000), # 20ms @8k PCM16
            640: ("slin16", 16000),# 20ms @16k PCM16
            960: ("slin16", 24000),
        }
        fmt, rate = mapping.get(frame_len, ("slin16" if frame_len % 2 == 0 else "ulaw", 8000))
        return fmt, rate

    def _wire_to_pcm16(
        self,
        audio_bytes: bytes,
        wire_fmt: str,
        swap_needed: bool,
        wire_rate: int,
    ) -> Tuple[bytes, int]:
        """Convert wire-format audio to PCM16 little-endian."""
        canonical = self._canonicalize_encoding(wire_fmt) or "ulaw"
        rate = wire_rate or 0
        if rate <= 0:
            try:
                _, inferred_rate = self._infer_transport_from_frame(len(audio_bytes))
            except Exception:
                inferred_rate = 0
            rate = inferred_rate or 8000
        pcm = audio_bytes
        try:
            if canonical in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                pcm = audioop.ulaw2lin(audio_bytes, 2)
                rate = 8000
            else:
                if swap_needed:
                    pcm = audioop.byteswap(audio_bytes, 2)
                else:
                    pcm = audio_bytes
        except Exception:
            pcm = b""
        return pcm or b"", rate

    def _encode_for_provider(
        self,
        call_id: str,
        provider_name: str,
        provider,
        pcm_bytes: bytes,
        pcm_rate: int,
    ) -> Tuple[bytes, str, int]:
        """Encode PCM audio based on provider configuration expectations."""
        if pcm_bytes is None:
            pcm_bytes = b""
        if pcm_rate <= 0:
            pcm_rate = 8000

        expected_enc = ""
        expected_rate = pcm_rate
        gain_target_rms = 0
        gain_max_db = 0.0
        try:
            provider_cfg = getattr(provider, "config", None)
            if provider_cfg is not None:
                # CRITICAL: Read provider-specific fields first (for real-time providers like Google Live, OpenAI)
                # Fall back to wire-format fields for backward compatibility (Deepgram Voice Agent)
                provider_enc = getattr(provider_cfg, "provider_input_encoding", None)
                wire_enc = getattr(provider_cfg, "input_encoding", None)
                expected_enc = self._canonicalize_encoding(provider_enc or wire_enc)
                
                provider_rate = getattr(provider_cfg, "provider_input_sample_rate_hz", None)
                wire_rate = getattr(provider_cfg, "input_sample_rate_hz", None)
                expected_rate = int(provider_rate or wire_rate or pcm_rate)

                # Optional inbound gain configuration (per-provider, disabled by default)
                try:
                    gain_target_rms = int(getattr(provider_cfg, "input_gain_target_rms", 0) or 0)
                except Exception:
                    gain_target_rms = 0
                try:
                    gain_max_db = float(getattr(provider_cfg, "input_gain_max_db", 0.0) or 0.0)
                except Exception:
                    gain_max_db = 0.0
                
                logger.info(
                    "🔧 ENCODE CONFIG - Reading provider config",
                    call_id=call_id,
                    provider=provider_name,
                    provider_enc=provider_enc,
                    wire_enc=wire_enc,
                    provider_rate=provider_rate,
                    wire_rate=wire_rate,
                    expected_enc=expected_enc,
                    expected_rate=expected_rate,
                    pcm_rate=pcm_rate,
                )
        except Exception as e:
            logger.error(
                "🔧 ENCODE CONFIG - Exception reading config",
                call_id=call_id,
                provider=provider_name,
                error=str(e),
                exc_info=True,
            )
            expected_enc = ""
            expected_rate = pcm_rate
            gain_target_rms = 0
            gain_max_db = 0.0

        # Prepare per-call/provider resample state holder
        prov_states = self._resample_state_provider_in.setdefault(call_id, {})
        state_key = f"{provider_name}:{expected_rate}"
        if expected_enc in ("slin16", "linear16", "pcm16", ""):
            if expected_rate <= 0:
                expected_rate = pcm_rate
            if pcm_rate != expected_rate and pcm_bytes:
                logger.info(
                    "🔧 ENCODE RESAMPLE - Resampling needed",
                    call_id=call_id,
                    provider=provider_name,
                    pcm_rate=pcm_rate,
                    expected_rate=expected_rate,
                    pcm_bytes=len(pcm_bytes),
                )
                try:
                    # CRITICAL FIX: audioop.ratecv() produces incorrect output sizes
                    # Example: 320 bytes @ 8kHz → 638 bytes @ 16kHz (should be 640)
                    # This 2-byte misalignment corrupts streaming for Google Live
                    input_bytes = len(pcm_bytes)
                    pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, pcm_rate, expected_rate, None)
                    
                    # Calculate expected output size based on sample rate ratio
                    # input_samples = input_bytes // 2 (2 bytes per sample)
                    # output_samples = input_samples * (expected_rate / pcm_rate)
                    # output_bytes = output_samples * 2
                    expected_bytes = int((input_bytes // 2) * (expected_rate / pcm_rate) * 2)
                    
                    # Force exact size by padding or trimming
                    if len(pcm_bytes) < expected_bytes:
                        # Pad with zeros (silence)
                        padding = expected_bytes - len(pcm_bytes)
                        pcm_bytes += b'\x00' * padding
                        logger.debug(
                            "🔧 ENCODE RESAMPLE - Padded to exact size",
                            call_id=call_id,
                            provider=provider_name,
                            before=len(pcm_bytes) - padding,
                            after=len(pcm_bytes),
                            padding_bytes=padding,
                        )
                    elif len(pcm_bytes) > expected_bytes:
                        # Trim excess
                        excess = len(pcm_bytes) - expected_bytes
                        pcm_bytes = pcm_bytes[:expected_bytes]
                        logger.debug(
                            "🔧 ENCODE RESAMPLE - Trimmed to exact size",
                            call_id=call_id,
                            provider=provider_name,
                            before=len(pcm_bytes) + excess,
                            after=len(pcm_bytes),
                            trimmed_bytes=excess,
                        )
                    
                    pcm_rate = expected_rate
                    logger.info(
                        "🔧 ENCODE RESAMPLE - Resampling completed (corrected)",
                        call_id=call_id,
                        provider=provider_name,
                        new_rate=pcm_rate,
                        new_bytes=len(pcm_bytes),
                        expected_bytes=expected_bytes,
                    )
                except Exception as e:
                    logger.error(
                        "🔧 ENCODE RESAMPLE - Resampling failed",
                        call_id=call_id,
                        provider=provider_name,
                        error=str(e),
                        exc_info=True,
                    )
            else:
                logger.info(
                    "🔧 ENCODE RESAMPLE - No resampling needed",
                    call_id=call_id,
                    provider=provider_name,
                    pcm_rate=pcm_rate,
                    expected_rate=expected_rate,
                )
            
            if provider_name == "google_live":
                return pcm_bytes, "slin16", pcm_rate
            
            # Re-enabled: Gain normalization required for low-volume audio
            # Root cause identified: Incoming audio had RMS=23 (needs ~1400)
            # Without normalization, Google Live cannot understand quiet audio
            # Silence frames during gating prevent echo while maintaining stream continuity
            #
            # NOTE: This is now gated by per-provider config:
            # - input_gain_target_rms <= 0 or input_gain_max_db <= 0.0  => gain disabled (default)
            # - both > 0 => enable normalization with configured target/max gain.
            if pcm_bytes and gain_target_rms > 0 and gain_max_db > 0.0:
                try:
                    # audioop already imported at module level - don't re-import here!
                    current_rms = audioop.rms(pcm_bytes, 2)
                    target_rms = gain_target_rms
                    max_gain_db = gain_max_db
                    
                    if current_rms > 10:  # Only apply if audio has some energy
                        gain_needed = target_rms / current_rms
                        max_gain = 10 ** (max_gain_db / 20.0)
                        gain = min(gain_needed, max_gain)
                        
                        if gain > 1.05:  # Apply if gain needed is >5%
                            pcm_bytes = audioop.mul(pcm_bytes, 2, gain)
                            actual_rms = audioop.rms(pcm_bytes, 2)
                            
                            # CRITICAL: Warn about excessive gain (indicates audio quality issues)
                            # High gain on low-quality audio causes distortion and speech recognition failures
                            if gain > 10.0:
                                logger.warning(
                                    "⚠️ AUDIO QUALITY ISSUE: Excessive gain required!",
                                    call_id=call_id,
                                    provider=provider_name,
                                    gain_multiplier=f"{gain:.1f}x",
                                    rms_before=current_rms,
                                    rms_target=target_rms,
                                    recommendation="Check SIP trunk rxgain configuration - incoming audio too quiet",
                                )
                            
                            logger.info(
                                "🔊 Provider input: Gain applied",
                                call_id=call_id,
                                provider=provider_name,
                                rms_before=current_rms,
                                rms_after=actual_rms,
                                rms_target=target_rms,
                                gain=f"{gain:.2f}",
                            )
                except Exception as e:
                    logger.error(f"Provider input normalization failed: {e}", call_id=call_id, exc_info=True)
            
            return pcm_bytes, "slin16", pcm_rate

        if expected_enc in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
            if expected_rate <= 0:
                expected_rate = 8000
            working = pcm_bytes
            if pcm_rate != expected_rate and working:
                try:
                    state = prov_states.get(state_key)
                    working, state = audioop.ratecv(working, 2, 1, pcm_rate, expected_rate, state)
                    prov_states[state_key] = state
                except Exception:
                    working = pcm_bytes
            try:
                encoded = audioop.lin2ulaw(working, 2)
            except Exception:
                encoded = b""
            return encoded, "ulaw", expected_rate

        # Fallback: return PCM as-is
        return pcm_bytes, "slin16", pcm_rate

    async def _update_transport_profile(self, session: CallSession, *, fmt: Optional[str], sample_rate: Optional[int], source: str) -> None:
        """Persist transport profile updates and sync preferences."""
        profile = session.transport_profile
        
        # Guard: Check if transport profile is initialized
        if profile is None:
            logger.warning(
                "Transport profile not initialized yet, skipping update",
                call_id=session.call_id,
                source=source,
                fmt=fmt,
                sample_rate=sample_rate
            )
            return
        
        # P1: Check if this is new TransportProfile (has wire_encoding) vs legacy (has format)
        if hasattr(profile, 'wire_encoding'):
            # New P1 TransportProfile - don't update, it's immutable per call
            logger.debug(
                "Skipping transport profile update for P1 TransportProfile",
                call_id=session.call_id,
                source=source,
            )
            return
        
        priority_order = {
            "config": 0,
            "dialplan": 1,
            "audiosocket": 2,
            # Provider can refine effective stream source format after transport is known
            "provider": 3,
            "detected": 4,
        }
        incoming_source = source or profile.source
        incoming_priority = priority_order.get(incoming_source, 0)
        current_priority = priority_order.get(profile.source, 0)

        if incoming_priority < current_priority and fmt is not None and sample_rate is not None:
            # Preserve higher-priority source; ignore lower-priority override.
            return
        if not fmt and not sample_rate:
            return
        canonical_fmt = self._canonicalize_encoding(fmt) or session.transport_profile.format
        final_rate = sample_rate or session.transport_profile.sample_rate
        changed = (
            profile.format != canonical_fmt
            or profile.sample_rate != final_rate
            or profile.source != incoming_source
        )
        profile.update(format=canonical_fmt, sample_rate=final_rate, source=incoming_source)
        session.caller_audio_format = canonical_fmt
        session.caller_sample_rate = final_rate
        self.call_audio_preferences[session.call_id] = {
            "format": canonical_fmt,
            "sample_rate": final_rate,
        }
        if changed:
            try:
                await self._save_session(session)
            except Exception:
                logger.debug("Failed to persist transport profile", call_id=session.call_id, exc_info=True)
            try:
                logger.info(
                    "Transport profile resolved",
                    call_id=session.call_id,
                    format=canonical_fmt,
                    sample_rate=final_rate,
                    source=source,
                )
            except Exception:
                pass

    def _update_audio_diagnostics(self, session: CallSession, stage: str, audio_bytes: bytes, encoding: str, sample_rate: int) -> None:
        """Track audio health metrics (RMS/DC offset) for observability."""
        try:
            canonical = self._canonicalize_encoding(encoding) or "slin16"
            if canonical == "ulaw":
                pcm = audioop.ulaw2lin(audio_bytes, 2)
            else:
                pcm = audio_bytes
            rms = audioop.rms(pcm, 2) if pcm else 0
            dc_offset = audioop.avg(pcm, 2) if pcm else 0
            session.audio_diagnostics[stage] = {
                "rms": rms,
                "dc_offset": dc_offset,
                "sample_rate": sample_rate,
                "updated": time.time(),
            }
            _AUDIO_RMS_GAUGE.labels(session.call_id, stage).set(rms)
            _AUDIO_DC_OFFSET.labels(session.call_id, stage).set(dc_offset)
            first_sample_key = f"{stage}_first_sample_logged"
            if not session.audio_diagnostics.get(first_sample_key):
                session.audio_diagnostics[first_sample_key] = True
                logger.info(
                    "Audio diagnostics sample captured",
                    call_id=session.call_id,
                    stage=stage,
                    format=canonical,
                    rms=rms,
                    dc_offset=dc_offset,
                    sample_rate=sample_rate,
                )
            rms_threshold = 50 if canonical == "ulaw" else 200
            alert_key = f"{stage}_low_rms_alerted"
            if rms < rms_threshold and not session.audio_diagnostics.get(alert_key):
                session.audio_diagnostics[alert_key] = True
                logger.warning(
                    "Low audio energy detected; degraded audio quality likely",
                    call_id=session.call_id,
                    stage=stage,
                    format=canonical,
                    rms=rms,
                    threshold=rms_threshold,
                )
            dc_threshold = 600
            dc_alert_key = f"{stage}_dc_alerted"
            if abs(dc_offset) > dc_threshold and not session.audio_diagnostics.get(dc_alert_key):
                session.audio_diagnostics[dc_alert_key] = True
                logger.warning(
                    "Significant DC offset detected in audio stream",
                    call_id=session.call_id,
                    stage=stage,
                    dc_offset=dc_offset,
                    threshold=dc_threshold,
                )
        except Exception:
            logger.debug("Audio diagnostics update failed", call_id=session.call_id, stage=stage, exc_info=True)

    async def _update_audio_diagnostics_by_call(
        self,
        call_id: str,
        stage: str,
        audio_bytes: bytes,
        encoding: str,
        sample_rate: int,
    ) -> None:
        session = await self.session_store.get_by_call_id(call_id)
        if not session:
            return
        self._update_audio_diagnostics(session, stage, audio_bytes, encoding, sample_rate)

    def _emit_transport_card(
        self,
        call_id: Optional[str],
        session: Optional[CallSession],
        *,
        source_encoding: Optional[Any],
        source_sample_rate: Optional[Any],
        target_encoding: Optional[Any],
        target_sample_rate: Optional[Any],
    ) -> None:
        if not call_id or call_id in self._transport_card_logged:
            return

        spm = getattr(self, "streaming_playback_manager", None)
        wire_encoding = None
        wire_rate: Optional[int] = None
        chunk_ms: Optional[int] = None
        idle_cutoff_ms: Optional[int] = None
        if spm is not None:
            try:
                wire_encoding = getattr(spm, "audiosocket_format", None)
            except Exception:
                wire_encoding = None
            try:
                rate_val = getattr(spm, "sample_rate", None)
                wire_rate = int(rate_val) if rate_val else None
            except Exception:
                wire_rate = None
            try:
                chunk_val = getattr(spm, "chunk_size_ms", None)
                chunk_ms = int(chunk_val) if chunk_val else None
            except Exception:
                chunk_ms = None
            try:
                idle_val = getattr(spm, "idle_cutoff_ms", None)
                idle_cutoff_ms = int(idle_val) if idle_val else None
            except Exception:
                idle_cutoff_ms = None

        provider_name = None
        transport_source = None
        transport_fmt = None
        transport_rate: Optional[int] = None
        if session is not None:
            provider_name = getattr(session, "provider_name", None) or getattr(session, "provider", None) or self.config.default_provider
            
            # P1: Handle both new TransportProfile and legacy transport_profile
            if hasattr(session.transport_profile, 'wire_encoding'):
                # New P1 TransportProfile
                transport_source = "p1_profile"
                transport_fmt = session.transport_profile.wire_encoding
                transport_rate = session.transport_profile.wire_sample_rate
            else:
                # Legacy transport_profile
                try:
                    transport_source = session.transport_profile.source
                except Exception:
                    transport_source = None
                try:
                    transport_fmt = session.transport_profile.format
                except Exception:
                    transport_fmt = None
                try:
                    rate = session.transport_profile.sample_rate
                    transport_rate = int(rate) if rate else None
                except Exception:
                    transport_rate = None

        def _canon_rate(value: Optional[Any]) -> Optional[int]:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        payload = {
            "call_id": call_id,
            "provider": provider_name,
            "transport_source": transport_source,
            "wire_encoding": self._canonicalize_encoding(wire_encoding) or None,
            "wire_sample_rate_hz": _canon_rate(wire_rate),
            "transport_encoding": self._canonicalize_encoding(transport_fmt) or None,
            "transport_sample_rate_hz": _canon_rate(transport_rate),
            "provider_encoding": self._canonicalize_encoding(source_encoding) or None,
            "provider_sample_rate_hz": _canon_rate(source_sample_rate),
            "target_encoding": self._canonicalize_encoding(target_encoding) or None,
            "target_sample_rate_hz": _canon_rate(target_sample_rate),
            "chunk_size_ms": _canon_rate(chunk_ms),
            "idle_cutoff_ms": _canon_rate(idle_cutoff_ms),
        }

        try:
            logger.info(
                "TransportCard",
                **{k: v for k, v in payload.items() if v is not None},
            )
            self._transport_card_logged.add(call_id)
        except Exception:
            logger.debug("TransportCard logging failed", call_id=call_id, exc_info=True)

    def _resolve_stream_targets(
        self,
        session: CallSession,
        provider_name: Optional[str],
    ) -> Tuple[str, int, Optional[str]]:
        provider_name = provider_name or getattr(session, "provider_name", None) or self.config.default_provider

        # CRITICAL: Use wire_encoding and wire_sample_rate from TransportProfile
        # TransportProfile (P1) uses wire_encoding/wire_sample_rate, not format/sample_rate
        transport_fmt = self._canonicalize_encoding(
            getattr(session.transport_profile, "wire_encoding", None) or 
            getattr(session.transport_profile, "format", None)
        ) or "ulaw"
        try:
            transport_rate = int(
                getattr(session.transport_profile, "wire_sample_rate", 0) or 
                getattr(session.transport_profile, "sample_rate", 0) or 0
            )
        except Exception:
            transport_rate = 0
        if transport_rate <= 0:
            transport_rate = 8000 if transport_fmt in {"ulaw", "mulaw", "g711_ulaw"} else 16000

        # Always refresh downstream preference view so playback manager aligns with transport
        self.call_audio_preferences[session.call_id] = {
            "format": transport_fmt,
            "sample_rate": transport_rate,
        }

        provider = self.providers.get(provider_name)
        
        # CRITICAL FIX: Read provider INPUT format (what provider receives)
        # NOT target format (what provider outputs)
        # TransportCard should show: provider receives X, wire expects Y
        provider_input_enc = None
        provider_input_rate = None
        provider_target_enc = None
        provider_target_rate = None
        try:
            provider_cfg = getattr(provider, "config", None)
            if provider_cfg:
                # Modern providers: read provider_input_* for what they receive
                provider_input_enc = self._canonicalize_encoding(
                    getattr(provider_cfg, "provider_input_encoding", None) or
                    getattr(provider_cfg, "input_encoding", None)
                )
                raw_input_rate = (
                    getattr(provider_cfg, "provider_input_sample_rate_hz", None) or
                    getattr(provider_cfg, "input_sample_rate_hz", None)
                )
                provider_input_rate = int(raw_input_rate) if raw_input_rate else None
                
                # Also read target for alignment validation
                provider_target_enc = self._canonicalize_encoding(getattr(provider_cfg, "target_encoding", None))
                raw_target_rate = getattr(provider_cfg, "target_sample_rate_hz", None)
                provider_target_rate = int(raw_target_rate) if raw_target_rate else None
        except Exception:
            provider_cfg = None

        # Validate outbound alignment (provider output vs wire expectations)
        remediation: Optional[str] = None
        aligned = True
        if provider_target_enc and provider_target_enc != transport_fmt:
            aligned = False
            remediation = (
                f"Provider target_encoding={provider_target_enc} but transport format={transport_fmt}. "
                f"Update providers.{provider_name}.target_encoding to '{transport_fmt}' in config/ai-agent.yaml."
            )
        if provider_target_rate and provider_target_rate != transport_rate:
            aligned = False
            extra = (
                f"Provider target_sample_rate_hz={provider_target_rate} but transport sample_rate={transport_rate}. "
                f"Update providers.{provider_name}.target_sample_rate_hz to {transport_rate}."
            )
            remediation = f"{remediation} {extra}".strip() if remediation else extra

        session.codec_alignment_ok = aligned
        session.codec_alignment_message = remediation
        try:
            _CODEC_ALIGNMENT.labels(session.call_id, provider_name).set(1 if aligned else 0)
        except Exception:
            pass

        if not aligned and remediation:
            logger.warning(
                "Codec/sample alignment degraded",
                call_id=session.call_id,
                provider=provider_name,
                remediation=remediation,
            )

        # CRITICAL FIX: TransportCard should show INBOUND encoding (what provider receives)
        self._emit_transport_card(
            session.call_id,
            session,
            source_encoding=provider_input_enc,    # ✅ What provider RECEIVES
            source_sample_rate=provider_input_rate, # ✅ What provider RECEIVES
            target_encoding=transport_fmt,          # ✅ What wire EXPECTS
            target_sample_rate=transport_rate,      # ✅ What wire EXPECTS
        )

        return transport_fmt, transport_rate, remediation

    async def _assign_pipeline_to_session(
        self,
        session: CallSession,
        pipeline_name: Optional[str] = None,
    ) -> Optional[PipelineResolution]:
        """Milestone7: Resolve pipeline components for a session and persist metadata."""
        if not getattr(self, "pipeline_orchestrator", None):
            return None
        if not self.pipeline_orchestrator.enabled:
            return None
        try:
            resolution = self.pipeline_orchestrator.get_pipeline(session.call_id, pipeline_name)
        except PipelineOrchestratorError as exc:
            logger.error(
                "Milestone7 pipeline resolution failed",
                call_id=session.call_id,
                requested_pipeline=pipeline_name,
                error=str(exc),
                exc_info=True,
            )
            return None
        except Exception as exc:
            logger.error(
                "Milestone7 pipeline resolution unexpected error",
                call_id=session.call_id,
                requested_pipeline=pipeline_name,
                error=str(exc),
                exc_info=True,
            )
            return None
 
        if not resolution:
            logger.debug(
                "Milestone7 pipeline orchestrator returned no resolution",
                call_id=session.call_id,
                requested_pipeline=pipeline_name,
            )
            return None
 
        component_summary = resolution.component_summary()
        updated = False
 
        if session.pipeline_name != resolution.pipeline_name:
            session.pipeline_name = resolution.pipeline_name
            updated = True
 
        if session.pipeline_components != component_summary:
            session.pipeline_components = component_summary
            updated = True
 
        provider_override = resolution.primary_provider
        if provider_override:
            if provider_override in self.providers:
                if session.provider_name != provider_override:
                    logger.info(
                        "Milestone7 pipeline overriding provider",
                        call_id=session.call_id,
                        previous_provider=session.provider_name,
                        override_provider=provider_override,
                    )
                    session.provider_name = provider_override
                    updated = True
            else:
                logger.debug(
                    "Pipeline requested provider not in monolithic providers; using pipeline adapters directly",
                    call_id=session.call_id,
                    requested_provider=provider_override,
                    current_provider=session.provider_name,
                    available_providers=list(self.providers.keys()),
                )
 
        if updated:
            await self._save_session(session)
 
        if not resolution.prepared:
            resolution.prepared = True
            logger.info(
                "Milestone7 pipeline resolved",
                call_id=session.call_id,
                pipeline=session.pipeline_name,
                components=component_summary,
                provider=session.provider_name,
            )
            options_summary = resolution.options_summary()
            if any(options_summary.values()):
                logger.debug(
                    "Milestone7 pipeline options",
                    call_id=session.call_id,
                    pipeline=session.pipeline_name,
                    options=options_summary,
                )
 
        return resolution
 
    async def _start_provider_session(self, call_id: str) -> None:
        """Start the provider session for a call when media path is ready."""
        try:
            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                logger.error("Start provider session called for unknown call", call_id=call_id)
                return

            # Preserve any per-call override previously applied. Only assign a pipeline
            # here if one has already been selected (e.g., via AI_PROVIDER or active_pipeline)
            pipeline_resolution = None
            if getattr(self.pipeline_orchestrator, "enabled", False):
                if getattr(session, "pipeline_name", None):
                    pipeline_resolution = await self._assign_pipeline_to_session(
                        session, pipeline_name=session.pipeline_name
                    )

            # Pipeline-only mode: if a pipeline is selected for this call, do not start
            # the legacy provider session or play the provider-managed greeting.
            if pipeline_resolution:
                logger.info(
                    "Pipeline-only mode: skipping legacy provider session; greeting will be handled by pipeline",
                    call_id=call_id,
                    pipeline=pipeline_resolution.pipeline_name,
                )
                try:
                    await self._ensure_pipeline_runner(session, forced=True)
                except Exception:
                    logger.debug(
                        "Failed to ensure pipeline runner in _start_provider_session",
                        call_id=call_id,
                        exc_info=True,
                    )
                return

            provider_name = session.provider_name or self.config.default_provider
            provider = self.providers.get(provider_name)

            if not provider:
                fallback_name = self.config.default_provider
                fallback_provider = self.providers.get(fallback_name)
                if fallback_provider:
                    logger.warning(
                        "Milestone7 pipeline provider unavailable; falling back to default provider",
                        call_id=call_id,
                        requested_provider=provider_name,
                        fallback_provider=fallback_name,
                    )
                    provider_name = fallback_name
                    provider = fallback_provider
                    if session.provider_name != fallback_name:
                        session.provider_name = fallback_name
                        await self._save_session(session)
                else:
                    logger.error(
                        "No provider available to start session",
                        call_id=call_id,
                        requested_provider=provider_name,
                        fallback_provider=fallback_name,
                    )
                    return

            if pipeline_resolution:
                logger.info(
                    "Milestone7 pipeline starting provider session",
                    call_id=call_id,
                    pipeline=pipeline_resolution.pipeline_name,
                    components=pipeline_resolution.component_summary(),
                    provider=provider_name,
                )
            elif getattr(self.pipeline_orchestrator, "enabled", False):
                logger.debug(
                    "Milestone7 pipeline orchestrator did not return a resolution; using legacy provider flow",
                    call_id=call_id,
                    provider=provider_name,
                )
            # Set provider input mode based on transport so send_audio can convert properly
            try:
                if hasattr(provider, 'set_input_mode'):
                    if self.config.audio_transport == 'externalmedia':
                        provider.set_input_mode('pcm16_16k')
                    else:
                        # Determine input mode from AudioSocket format
                        as_fmt = None
                        try:
                            if self.config.audiosocket and hasattr(self.config.audiosocket, 'format'):
                                as_fmt = (self.config.audiosocket.format or '').lower()
                        except Exception:
                            as_fmt = None
                        if as_fmt in ('ulaw', 'mulaw', 'g711_ulaw'):
                            provider.set_input_mode('mulaw8k')
                        else:
                            # Default to PCM16 at 8 kHz when AudioSocket is slin16 or unspecified
                            provider.set_input_mode('pcm16_8k')
            except Exception:
                logger.debug("Provider set_input_mode failed or unsupported", exc_info=True)

            # Note: Context greeting/prompt injection now happens earlier in P1 _resolve_audio_profile()
            # to ensure config is set BEFORE provider session starts and reads it.
            
            # Build context dict for providers that need it (Google Live, OpenAI Realtime)
            provider_context = {}
            try:
                if session.context_name:
                    context_config = self.transport_orchestrator.get_context_config(session.context_name)
                    logger.debug(
                        "DEBUG: Building provider context",
                        call_id=call_id,
                        context_name=session.context_name,
                        has_context_config=bool(context_config),
                        config_type=type(context_config).__name__ if context_config else None,
                        has_tools_attr=hasattr(context_config, 'tools') if context_config else False,
                    )
                    if context_config:
                        # Include tools if defined in context
                        if hasattr(context_config, 'tools') and context_config.tools:
                            provider_context['tools'] = context_config.tools
                            logger.debug(
                                "Added tools to provider context",
                                call_id=call_id,
                                tools=context_config.tools,
                            )
                        else:
                            logger.debug(
                                "DEBUG: No tools found in context config",
                                call_id=call_id,
                                has_tools_attr=hasattr(context_config, 'tools'),
                                tools_value=getattr(context_config, 'tools', 'NO_ATTR'),
                            )
                        # Include prompt for reference (though config.instructions should already be set)
                        if hasattr(context_config, 'prompt') and context_config.prompt:
                            provider_context['prompt'] = context_config.prompt
            except Exception as e:
                logger.warning(f"Failed to build provider context: {e}", call_id=call_id, exc_info=True)
            
            # Inject tool execution context into provider if it supports tools (Deepgram, Google Live)
            if hasattr(provider, 'tool_adapter') or hasattr(provider, '_tool_adapter'):
                try:
                    provider._caller_channel_id = session.caller_channel_id
                    provider._bridge_id = session.bridge_id
                    provider._session_store = self.session_store
                    provider._ari_client = self.ari_client
                    provider._full_config = self.config.dict()  # Convert Pydantic model to dict
                    logger.debug(
                        "Injected tool execution context into provider",
                        call_id=call_id,
                        provider=provider_name
                    )
                except Exception as e:
                    logger.warning(f"Failed to inject tool context: {e}", call_id=call_id)

            logger.info("DEBUG: About to call provider.start_session", call_id=call_id, provider=provider_name)
            await provider.start_session(call_id, context=provider_context if provider_context else None)
            logger.info("DEBUG: provider.start_session completed", call_id=call_id, provider=provider_name)
            # If provider supports an explicit greeting (e.g., LocalProvider), trigger it now
            try:
                if hasattr(provider, 'play_initial_greeting'):
                    await provider.play_initial_greeting(call_id)
            except Exception:
                logger.debug("Provider initial greeting failed or unsupported", exc_info=True)
            session.provider_session_active = True
            # Ensure upstream capture is enabled for real-time providers when not gated
            try:
                if not session.tts_playing and not session.audio_capture_enabled:
                    session.audio_capture_enabled = True
            except Exception:
                pass
            await self._save_session(session)
            # Sync gauges if coordinator is present
            if self.conversation_coordinator:
                try:
                    await self.conversation_coordinator.sync_from_session(session)
                except Exception:
                    pass
            logger.info("Provider session started", call_id=call_id, provider=provider_name)
            
            # Record call metadata for dashboard
            try:
                caller_name = getattr(session, 'caller_name', None) or "Unknown"
                caller_number = getattr(session, 'caller_number', None) or "Unknown"
                pipeline_name = getattr(session, 'pipeline_name', None) or "default"
                context_name = getattr(session, 'context_name', None) or "default"
                
                # Set call metadata gauge (value=1 means active)
                _CALL_METADATA.labels(
                    call_id=call_id,
                    caller_name=caller_name,
                    caller_number=caller_number,
                    pipeline=pipeline_name,
                    provider=provider_name,
                    context=context_name
                ).set(1)
                
                logger.debug("Recorded call metadata", 
                            call_id=call_id,
                            caller_name=caller_name,
                            pipeline=pipeline_name,
                            provider=provider_name,
                            context=context_name)
            except Exception as e:
                logger.debug("Failed to record call metadata", call_id=call_id, error=str(e))
                
        except Exception as exc:
            logger.error("Failed to start provider session", call_id=call_id, error=str(exc), exc_info=True)

    async def _on_playback_finished(self, event: Dict[str, Any]):
        """Delegate ARI PlaybackFinished to PlaybackManager for gating and cleanup."""
        try:
            playback_id = None
            playback = event.get("playback", {}) or {}
            playback_id = playback.get("id") or event.get("playbackId")
            if not playback_id:
                logger.debug("PlaybackFinished without playback id", playback_event=event)
                return
            await self.playback_manager.on_playback_finished(playback_id)
        except Exception as exc:
            logger.error("Error in PlaybackFinished handler", error=str(exc), exc_info=True)

    async def _start_health_server(self):
        """Start aiohttp health/metrics server on 0.0.0.0:15000."""
        try:
            app = web.Application()
            app.router.add_get('/live', self._live_handler)
            app.router.add_get('/ready', self._ready_handler)
            app.router.add_get('/health', self._health_handler)
            app.router.add_get('/metrics', self._metrics_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            # Host/port configurable via YAML health block with environment overrides (AAVA-30)
            try:
                # Precedence: env overrides > YAML health.* > defaults
                if "HEALTH_BIND_HOST" in os.environ:
                    health_host = os.getenv('HEALTH_BIND_HOST', '127.0.0.1')
                else:
                    health_host = getattr(getattr(self.config, "health", None), "host", "127.0.0.1")

                if "HEALTH_BIND_PORT" in os.environ:
                    health_port = int(os.getenv('HEALTH_BIND_PORT', '15000'))
                else:
                    health_port = int(getattr(getattr(self.config, "health", None), "port", 15000))
            except Exception:
                health_host = '127.0.0.1'
                health_port = 15000
            site = web.TCPSite(runner, health_host, health_port)
            await site.start()
            self._health_runner = runner
            logger.info("Health endpoint started", host=health_host, port=health_port)
        except Exception as exc:
            logger.error("Failed to start health endpoint", error=str(exc), exc_info=True)

    async def _health_handler(self, request):
        """Return JSON with engine/provider status."""
        try:
            providers = {}
            for name, prov in (self.providers or {}).items():
                ready = True
                try:
                    if hasattr(prov, 'is_ready'):
                        ready = bool(prov.is_ready())
                except Exception:
                    ready = True
                providers[name] = {"ready": ready}

            # Compute readiness
            default_ready = False
            if self.config and getattr(self.config, 'default_provider', None) in (self.providers or {}):
                prov = self.providers[self.config.default_provider]
                try:
                    default_ready = bool(prov.is_ready()) if hasattr(prov, 'is_ready') else True
                except Exception:
                    default_ready = True
            ari_connected = bool(self.ari_client and self.ari_client.running)
            audiosocket_listening = self.audio_socket_server is not None if self.config.audio_transport == 'audiosocket' else True
            is_ready = ari_connected and audiosocket_listening and default_ready

            payload = {
                "status": "healthy" if is_ready else "degraded",
                "ari_connected": ari_connected,
                "rtp_server_running": bool(getattr(self, 'rtp_server', None)),
                "audio_transport": self.config.audio_transport,
                "active_calls": len(await self.session_store.get_all_sessions()),
                "active_playbacks": 0,
                "providers": providers,
                "rtp_server": {},
                "audiosocket": {
                    "listening": audiosocket_listening,
                    "host": getattr(self.config.audiosocket, 'host', None) if self.config.audiosocket else None,
                    "port": getattr(self.config.audiosocket, 'port', None) if self.config.audiosocket else None,
                    "active_connections": (self.audio_socket_server.get_connection_count() if self.audio_socket_server else 0),
                },
                "audiosocket_listening": audiosocket_listening,
                "conversation": {
                    "gating_active": 0,
                    "capture_disabled": 0,
                    "barge_in_total": 0,
                },
                "streaming": {},
                "streaming_details": [],
            }
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response({"status": "error", "error": str(exc)}, status=500)

    async def _live_handler(self, request):
        """Liveness probe: returns 200 if process is up."""
        return web.Response(text="ok", status=200)

    async def _ready_handler(self, request):
        """Readiness probe: 200 only if ARI, transport, and default provider are ready."""
        try:
            ari_connected = bool(self.ari_client and self.ari_client.running)
            transport_ok = True
            if self.config.audio_transport == 'audiosocket':
                transport_ok = self.audio_socket_server is not None
            elif self.config.audio_transport == 'externalmedia':
                transport_ok = self.rtp_server is not None
            provider_ok = False
            if self.config and getattr(self.config, 'default_provider', None) in (self.providers or {}):
                prov = self.providers[self.config.default_provider]
                try:
                    provider_ok = bool(prov.is_ready()) if hasattr(prov, 'is_ready') else True
                except Exception:
                    provider_ok = True

            is_ready = ari_connected and transport_ok and provider_ok
            status = 200 if is_ready else 503
            return web.json_response({
                "ari_connected": ari_connected,
                "transport_ok": transport_ok,
                "provider_ok": provider_ok,
                "ready": is_ready,
            }, status=status)
        except Exception as exc:
            return web.json_response({"ready": False, "error": str(exc)}, status=500)

    async def _metrics_handler(self, request):
        """Expose Prometheus metrics."""
        try:
            data = generate_latest()
            # aiohttp forbids 'charset=' inside content_type arg; pass full header via headers.
            return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})
        except Exception as exc:
            return web.Response(text=str(exc), status=500)


async def main():
    config = load_config()
    # Initialize structured logging according to YAML-configured level (default INFO)
    try:
        level_name = str(getattr(getattr(config, 'logging', None), 'level', 'info')).upper()
        level = getattr(logging, level_name, logging.INFO)
        configure_logging(log_level=level)
    except Exception:
        # Fallback to INFO if configuration not yet available
        configure_logging(log_level="INFO")
    
    # Validate configuration before starting engine (AAVA-21)
    from .config import validate_production_config
    errors, warnings = validate_production_config(config)
    
    if errors:
        logger.error("❌ Configuration validation FAILED", errors=errors, warnings=warnings)
        raise RuntimeError(f"Configuration errors: {errors}")
    
    if warnings:
        logger.warning("⚠️  Configuration warnings", warnings=warnings)
    
    logger.info("✅ Configuration validation passed")
    
    engine = Engine(config)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    service_task = loop.create_task(engine.start())
    await shutdown_event.wait()

    await engine.stop()
    service_task.cancel()
    try:
        await service_task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("AI Voice Agent has shut down.")
