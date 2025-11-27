import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

from websockets.exceptions import ConnectionClosed
from websockets.server import serve
from vosk import Model as VoskModel, KaldiRecognizer
from llama_cpp import Llama
from piper import PiperVoice

# Configure logging level from environment (default INFO)
_level_name = os.getenv("LOCAL_LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(level=_level)

# Debug mode for verbose audio processing logs
# Set LOCAL_DEBUG=1 in .env to enable detailed audio flow logging
DEBUG_AUDIO_FLOW = os.getenv("LOCAL_DEBUG", "0") == "1"

SUPPORTED_MODES = {"full", "stt", "llm", "tts"}
DEFAULT_MODE = "full"
ULAW_SAMPLE_RATE = 8000
PCM16_TARGET_RATE = 16000


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


@dataclass
class SessionContext:
    """# Milestone7: Track per-connection defaults for selective mode handling."""
    call_id: str = "unknown"
    mode: str = DEFAULT_MODE
    recognizer: Optional[KaldiRecognizer] = None
    last_partial: str = ""
    partial_emitted: bool = False
    last_audio_at: float = 0.0
    idle_task: Optional[asyncio.Task] = None
    last_request_meta: Dict[str, Any] = field(default_factory=dict)
    last_final_text: str = ""
    last_final_norm: str = ""
    last_final_at: float = 0.0
    llm_user_turns: List[str] = field(default_factory=list)
    audio_buffer: bytes = b""


class AudioProcessor:
    """Handles audio format conversions for MVP uLaw 8kHz pipeline"""

    @staticmethod
    def resample_audio(input_data: bytes,
                       input_rate: int,
                       output_rate: int,
                       input_format: str = "raw",
                       output_format: str = "raw") -> bytes:
        """Resample audio using sox"""
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{input_format}", delete=False) as input_file:
                input_file.write(input_data)
                input_path = input_file.name

            with tempfile.NamedTemporaryFile(suffix=f".{output_format}", delete=False) as output_file:
                output_path = output_file.name

            # Use sox to resample - specify input format for raw PCM data
            cmd = [
                "sox",
                "-t",
                "raw",
                "-r",
                str(input_rate),
                "-e",
                "signed-integer",
                "-b",
                "16",
                "-c",
                "1",
                input_path,
                "-r",
                str(output_rate),
                "-c",
                "1",
                "-e",
                "signed-integer",
                "-b",
                "16",
                output_path,
            ]

            subprocess.run(cmd, capture_output=True, check=True)

            with open(output_path, "rb") as f:
                resampled_data = f.read()

            os.unlink(input_path)
            os.unlink(output_path)

            return resampled_data

        except Exception as exc:  # pragma: no cover - defensive guard
            logging.error("Audio resampling failed: %s", exc)
            return input_data

    @staticmethod
    def convert_to_ulaw_8k(input_data: bytes, input_rate: int) -> bytes:
        """Convert audio to uLaw 8kHz format for ARI playback"""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as input_file:
                input_file.write(input_data)
                input_path = input_file.name

            with tempfile.NamedTemporaryFile(suffix=".ulaw", delete=False) as output_file:
                output_path = output_file.name

            cmd = [
                "sox",
                input_path,
                "-r",
                str(ULAW_SAMPLE_RATE),
                "-c",
                "1",
                "-e",
                "mu-law",
                "-t",
                "raw",
                output_path,
            ]

            subprocess.run(cmd, capture_output=True, check=True)

            with open(output_path, "rb") as f:
                ulaw_data = f.read()

            os.unlink(input_path)
            os.unlink(output_path)

            return ulaw_data

        except Exception as exc:  # pragma: no cover - defensive guard
            logging.error("uLaw conversion failed: %s", exc)
            return input_data


class LocalAIServer:
    def __init__(self):
        self.stt_model: Optional[VoskModel] = None
        self.llm_model: Optional[Llama] = None
        self.tts_model: Optional[PiperVoice] = None
        self.audio_processor = AudioProcessor()

        # Model paths
        self.stt_model_path = os.getenv(
            "LOCAL_STT_MODEL_PATH", "/app/models/stt/vosk-model-small-en-us-0.15"
        )
        self.llm_model_path = os.getenv(
            "LOCAL_LLM_MODEL_PATH", "/app/models/llm/phi-3-mini-4k-instruct.Q4_K_M.gguf"
        )
        self.tts_model_path = os.getenv(
            "LOCAL_TTS_MODEL_PATH", "/app/models/tts/en_US-lessac-medium.onnx"
        )

        default_threads = max(1, min(16, os.cpu_count() or 1))
        self.llm_threads = int(os.getenv("LOCAL_LLM_THREADS", str(default_threads)))
        self.llm_context = int(os.getenv("LOCAL_LLM_CONTEXT", "768"))
        self.llm_batch = int(os.getenv("LOCAL_LLM_BATCH", "256"))
        self.llm_max_tokens = int(os.getenv("LOCAL_LLM_MAX_TOKENS", "48"))
        self.llm_temperature = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0.2"))
        self.llm_top_p = float(os.getenv("LOCAL_LLM_TOP_P", "0.85"))
        self.llm_repeat_penalty = float(os.getenv("LOCAL_LLM_REPEAT_PENALTY", "1.05"))
        self.llm_system_prompt = os.getenv(
            "LOCAL_LLM_SYSTEM_PROMPT",
            "You are a helpful AI voice assistant. Respond naturally and conversationally to the caller."
        )
        self.llm_stop_tokens = [
            token.strip()
            for token in os.getenv(
                "LOCAL_LLM_STOP_TOKENS",
                "<|user|>,<|assistant|>,<|end|>"
            ).split(",")
            if token.strip()
        ]
        if not self.llm_stop_tokens:
            self.llm_stop_tokens = ["<|user|>", "<|assistant|>", "<|end|>"]
        # Allow disabling mlock by env to avoid startup failures on some hosts
        self.llm_use_mlock = bool(int(os.getenv("LOCAL_LLM_USE_MLOCK", "0")))

        # Audio buffering for STT (20ms chunks need to be buffered for effective STT)
        self.audio_buffer = b""
        self.buffer_size_bytes = PCM16_TARGET_RATE * 2 * 1.0  # 1 second at 16kHz (32000 bytes)
        # Process buffer after N ms of silence (idle finalizer). Configurable via env.
        self.buffer_timeout_ms = int(os.getenv("LOCAL_STT_IDLE_MS", "3000"))

    def _resolve_vosk_model_path(self, path: str) -> str:
        """Resolve the correct Vosk model directory.

        Some archives extract with an extra nesting level. We prefer a directory
        that contains a 'conf' subdirectory which is expected by Vosk.
        """
        try:
            if os.path.isdir(path) and os.path.isdir(os.path.join(path, "conf")):
                return path
            if os.path.isdir(path):
                for entry in os.listdir(path):
                    candidate = os.path.join(path, entry)
                    if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, "conf")):
                        return candidate
        except Exception as exc:  # pragma: no cover - defensive
            logging.debug("Vosk model path resolution skipped", exc_info=True)
        return path

    async def initialize_models(self):
        """Initialize all AI models with proper error handling"""
        logging.info("🚀 Initializing enhanced AI models for MVP...")

        await self._load_stt_model()
        await self._load_llm_model()
        await self.run_startup_latency_check()
        await self._load_tts_model()

        logging.info("✅ All models loaded successfully for MVP pipeline")

    async def _load_stt_model(self):
        """Load STT model with 16kHz support"""
        try:
            # Resolve nested model directory if needed
            resolved_path = self._resolve_vosk_model_path(self.stt_model_path)
            if not os.path.exists(resolved_path):
                raise FileNotFoundError(f"STT model not found at {resolved_path}")

            # Extra sanity: require 'conf' folder inside the model dir
            if not os.path.isdir(os.path.join(resolved_path, "conf")):
                # Provide a helpful listing for debugging
                try:
                    listing = ", ".join(os.listdir(resolved_path))
                except Exception:
                    listing = "<unavailable>"
                raise FileNotFoundError(
                    f"STT model at {resolved_path} does not appear to be a valid Vosk model (missing 'conf'). Contents: {listing}"
                )

            self.stt_model = VoskModel(resolved_path)
            # Keep the resolved path for reference
            self.stt_model_path = resolved_path
            logging.info("✅ STT model loaded: %s (16kHz native)", self.stt_model_path)
        except Exception as exc:
            logging.error("❌ Failed to load STT model: %s", exc)
            raise

    async def _load_llm_model(self):
        """Load LLM model with optimized parameters for faster inference"""
        try:
            if not os.path.exists(self.llm_model_path):
                raise FileNotFoundError(f"LLM model not found at {self.llm_model_path}")

            self.llm_model = Llama(
                model_path=self.llm_model_path,
                n_ctx=self.llm_context,
                n_threads=self.llm_threads,
                n_batch=self.llm_batch,
                n_gpu_layers=0,
                verbose=False,
                use_mmap=True,
                use_mlock=self.llm_use_mlock,
                add_bos=False,
            )
            logging.info("✅ LLM model loaded: %s", self.llm_model_path)
            logging.info(
                "📊 LLM Config: ctx=%s, threads=%s, batch=%s, max_tokens=%s, temp=%s",
                self.llm_context,
                self.llm_threads,
                self.llm_batch,
                self.llm_max_tokens,
                self.llm_temperature,
            )
        except Exception as exc:
            logging.error("❌ Failed to load LLM model: %s", exc)
            raise

    async def run_startup_latency_check(self) -> None:
        """Run a lightweight LLM inference at startup to log baseline latency."""
        if not self.llm_model:
            return

        try:
            session = SessionContext(call_id="startup-latency")
            sample_text = "Hello, can you hear me?"
            prompt, prompt_tokens, truncated, raw_tokens = self._prepare_llm_prompt(
                session, sample_text
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            logging.info(
                "🧪 LLM WARMUP START - Running startup latency check (prompt_tokens=%s raw_tokens=%s model=%s ctx=%s batch=%s max_tokens<=%s)",
                prompt_tokens,
                raw_tokens,
                os.path.basename(self.llm_model_path),
                self.llm_context,
                self.llm_batch,
                min(self.llm_max_tokens, 32),
            )

            # Heartbeat: log progress while the warm-up runs so users see activity
            done = asyncio.Event()

            async def _heartbeat():
                elapsed = 0
                interval = 5
                try:
                    while not done.is_set():
                        await asyncio.sleep(interval)
                        elapsed += interval
                        logging.info(
                            "⏳ LLM WARMUP - In progress (~%ss elapsed, model=%s ctx=%s batch=%s)",
                            elapsed,
                            os.path.basename(self.llm_model_path),
                            self.llm_context,
                            self.llm_batch,
                        )
                except asyncio.CancelledError:
                    pass

            hb_task = asyncio.create_task(_heartbeat())

            await asyncio.to_thread(
                self.llm_model,
                prompt,
                max_tokens=min(self.llm_max_tokens, 32),
                stop=self.llm_stop_tokens,
                echo=False,
                temperature=self.llm_temperature,
                top_p=self.llm_top_p,
                repeat_penalty=self.llm_repeat_penalty,
            )

            latency_ms = round((loop.time() - started) * 1000.0, 2)
            done.set()
            try:
                hb_task.cancel()
            except Exception:
                pass
            logging.info(
                "🤖 LLM STARTUP LATENCY - %.2f ms (prompt_tokens=%s raw_tokens=%s truncated=%s)",
                latency_ms,
                prompt_tokens,
                raw_tokens,
                truncated,
            )
        except Exception as exc:  # pragma: no cover - best-effort metric
            logging.warning(
                "🤖 LLM STARTUP LATENCY CHECK FAILED: %s",
                exc,
                exc_info=True,
            )

    async def _load_tts_model(self):
        """Load TTS model (Piper) with 22kHz support"""
        try:
            if not os.path.exists(self.tts_model_path):
                raise FileNotFoundError(f"TTS model not found at {self.tts_model_path}")

            self.tts_model = PiperVoice.load(self.tts_model_path)
            logging.info("✅ TTS model loaded: %s (22kHz native)", self.tts_model_path)
        except Exception as exc:
            logging.error("❌ Failed to load TTS model: %s", exc)
            raise

    async def reload_models(self):
        """Hot reload all models without restarting the server"""
        logging.info("🔄 Hot reloading models...")
        try:
            await self.initialize_models()
            logging.info("✅ Models reloaded successfully")
        except Exception as exc:
            logging.error("❌ Model reload failed: %s", exc)
            raise

    async def reload_llm_only(self):
        """Hot reload only the LLM model with optimized parameters"""
        logging.info("🔄 Hot reloading LLM model with optimizations...")
        try:
            if self.llm_model:
                del self.llm_model
                self.llm_model = None
                logging.info("🗑️ Previous LLM model unloaded")

            await self._load_llm_model()
            logging.info("✅ LLM model reloaded with optimizations")
            logging.info(
                "📊 Optimized: ctx=%s, batch=%s, temp=%s, max_tokens=%s",
                self.llm_context,
                self.llm_batch,
                self.llm_temperature,
                self.llm_max_tokens,
            )
        except Exception as exc:
            logging.error("❌ LLM reload failed: %s", exc)
            raise

    async def process_stt_buffered(self, audio_data: bytes) -> str:
        """Process STT with buffering for 20ms chunks"""
        try:
            if not self.stt_model:
                logging.error("STT model not loaded")
                return ""

            self.audio_buffer += audio_data
            logging.debug(
                "🎵 STT BUFFER - Added %s bytes, buffer now %s bytes",
                len(audio_data),
                len(self.audio_buffer),
            )

            if len(self.audio_buffer) < self.buffer_size_bytes:
                logging.debug(
                    "🎵 STT BUFFER - Not enough audio yet (%s/%s bytes)",
                    len(self.audio_buffer),
                    self.buffer_size_bytes,
                )
                return ""

            logging.info("🎵 STT PROCESSING - Processing buffered audio: %s bytes", len(self.audio_buffer))

            recognizer = KaldiRecognizer(self.stt_model, PCM16_TARGET_RATE)

            if recognizer.AcceptWaveform(self.audio_buffer):
                result = json.loads(recognizer.Result())
            else:
                result = json.loads(recognizer.FinalResult())

            transcript = result.get("text", "").strip()
            if transcript:
                logging.info("📝 STT RESULT - Transcript: '%s'", transcript)
            else:
                logging.debug("📝 STT RESULT - Transcript empty after buffering")

            self.audio_buffer = b""
            return transcript

        except Exception as exc:
            logging.error("Buffered STT processing failed: %s", exc, exc_info=True)
            return ""

    async def process_stt(self, audio_data: bytes, input_rate: int = PCM16_TARGET_RATE) -> str:
        """Process STT with Vosk only - optimized for telephony audio"""
        try:
            if not self.stt_model:
                logging.error("STT model not loaded")
                return ""

            logging.debug("🎤 STT INPUT - %s bytes at %s Hz", len(audio_data), input_rate)

            if input_rate != PCM16_TARGET_RATE:
                logging.debug(
                    "🎵 STT INPUT - Resampling %s Hz → %s Hz: %s bytes",
                    input_rate,
                    PCM16_TARGET_RATE,
                    len(audio_data),
                )
                resampled_audio = self.audio_processor.resample_audio(
                    audio_data, input_rate, PCM16_TARGET_RATE, "raw", "raw"
                )
            else:
                resampled_audio = audio_data

            recognizer = KaldiRecognizer(self.stt_model, PCM16_TARGET_RATE)

            if recognizer.AcceptWaveform(resampled_audio):
                result = json.loads(recognizer.Result())
            else:
                result = json.loads(recognizer.FinalResult())

            transcript = result.get("text", "").strip()
            if transcript:
                logging.info(
                    "📝 STT RESULT - Vosk transcript: '%s' (length: %s)",
                    transcript,
                    len(transcript),
                )
            else:
                logging.debug("📝 STT RESULT - Vosk transcript empty")
            return transcript

        except Exception as exc:
            logging.error("STT processing failed: %s", exc, exc_info=True)
            return ""

    async def process_llm(self, prompt: str) -> str:
        """Run LLM inference using the prepared Phi-style prompt."""
        try:
            if not self.llm_model:
                logging.warning("LLM model not loaded, using fallback")
                return "I'm here to help you. How can I assist you today?"

            loop = asyncio.get_running_loop()
            started = loop.time()
            output = await asyncio.to_thread(
                self.llm_model,
                prompt,
                max_tokens=self.llm_max_tokens,
                stop=self.llm_stop_tokens,
                echo=False,
                temperature=self.llm_temperature,
                top_p=self.llm_top_p,
                repeat_penalty=self.llm_repeat_penalty,
            )

            choices = output.get("choices", []) if isinstance(output, dict) else []
            if not choices:
                logging.warning("🤖 LLM RESULT - No choices returned, using fallback response")
                return "I'm here to help you. How can I assist you today?"

            response = choices[0].get("text", "").strip()
            latency_ms = round((loop.time() - started) * 1000.0, 2)
            logging.info(
                "🤖 LLM RESULT - Completed in %s ms tokens=%s",
                latency_ms,
                len(response.split()),
            )
            return response

        except Exception as exc:
            logging.error("LLM processing failed: %s", exc, exc_info=True)
            return "I'm here to help you. How can I assist you today?"

    def _count_prompt_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self.llm_model and hasattr(self.llm_model, "tokenize"):
            try:
                tokens = self.llm_model.tokenize(text.encode("utf-8"), add_bos=False)
                return len(tokens)
            except Exception as exc:  # pragma: no cover - defensive guard
                logging.debug("Tokenization failed, falling back to whitespace split: %s", exc)
        return len(text.split())

    def _build_phi_prompt(self, user_text: str) -> str:
        user_text = (user_text or "").strip()
        segments = ["<|system|>", self.llm_system_prompt.strip(), "<|user|>"]
        segments.append(user_text if user_text else "Hello")
        segments.append("<|assistant|>")
        return "\n".join(segments) + "\n"

    @staticmethod
    def _strip_leading_bos(prompt: str) -> str:
        if not prompt:
            return prompt
        cleaned = prompt.lstrip()
        for marker in ("<s>", "<|bos|>"):
            while cleaned.startswith(marker):
                cleaned = cleaned[len(marker):].lstrip()
        return cleaned

    def _prepare_llm_prompt(
        self, session: SessionContext, new_turn: str
    ) -> Tuple[str, int, bool, int]:
        """Append a user turn, trim history to fit context, and report token counts."""
        candidate_turns = list(session.llm_user_turns) + [new_turn]
        raw_user_text = "\n\n".join(candidate_turns).strip()
        raw_prompt = self._build_phi_prompt(raw_user_text)
        raw_tokens = self._count_prompt_tokens(raw_prompt)

        max_prompt_tokens = max(self.llm_context - self.llm_max_tokens - 64, 128)
        trimmed_turns = list(candidate_turns)
        truncated = False
        while trimmed_turns and self._count_prompt_tokens(
            self._build_phi_prompt("\n\n".join(trimmed_turns).strip())
        ) > max_prompt_tokens:
            trimmed_turns.pop(0)
            truncated = True

        trimmed_user_text = "\n\n".join(trimmed_turns).strip()
        prompt_text = self._build_phi_prompt(trimmed_user_text)
        prompt_text = self._strip_leading_bos(prompt_text)
        prompt_tokens = self._count_prompt_tokens(prompt_text)
        session.llm_user_turns = trimmed_turns
        return prompt_text, prompt_tokens, truncated, raw_tokens

    async def process_tts(self, text: str) -> bytes:
        """Process TTS with 8kHz uLaw generation directly"""
        try:
            if not self.tts_model:
                logging.error("TTS model not loaded")
                return b""

            logging.debug("🔊 TTS INPUT - Generating 22kHz audio for: '%s'", text)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
                wav_path = wav_file.name

            # Write WAV data either by letting Piper stream into the wave writer
            # or by consuming a generator for backward compatibility.
            with wave.open(wav_path, "wb") as wav_file:
                # Mono, 16-bit, 22.05 kHz (typical Piper voice rate)
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(22050)
                try:
                    # Newer Piper API: synthesize(text, wav_file)
                    self.tts_model.synthesize(text, wav_file)
                except TypeError:
                    # Fallback: older API returns a generator of frames
                    audio_generator = self.tts_model.synthesize(text)
                    for chunk in audio_generator:
                        if isinstance(chunk, (bytes, bytearray)):
                            wav_file.writeframes(chunk)
                        else:
                            data = getattr(chunk, "audio_int16_bytes", None)
                            if data:
                                wav_file.writeframes(data)

            with open(wav_path, "rb") as wav_file:
                wav_data = wav_file.read()

            ulaw_data = self.audio_processor.convert_to_ulaw_8k(wav_data, 22050)
            os.unlink(wav_path)

            logging.info("🔊 TTS RESULT - Generated uLaw 8kHz audio: %s bytes", len(ulaw_data))
            return ulaw_data

        except Exception as exc:
            logging.error("TTS processing failed: %s", exc, exc_info=True)
            return b""

    def _cancel_idle_timer(self, session: SessionContext) -> None:
        if session.idle_task and not session.idle_task.done():
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if session.idle_task is not current_task:
                session.idle_task.cancel()
        session.idle_task = None

    def _reset_stt_session(self, session: SessionContext, last_text: str = "") -> None:
        """Clear recognizer state after emitting a final transcript."""
        self._cancel_idle_timer(session)
        session.recognizer = None
        session.last_partial = ""
        session.partial_emitted = False
        session.audio_buffer = b""
        session.last_request_meta.clear()
        session.last_final_text = last_text
        session.last_final_norm = _normalize_text(last_text)
        session.last_final_at = monotonic()

    def _ensure_stt_recognizer(self, session: SessionContext) -> Optional[KaldiRecognizer]:
        if not self.stt_model:
            logging.error("STT model not loaded")
            return None

        if session.recognizer is None:
            session.recognizer = KaldiRecognizer(self.stt_model, PCM16_TARGET_RATE)
            session.last_partial = ""
            session.partial_emitted = False
        return session.recognizer

    async def _process_stt_stream(
        self,
        session: SessionContext,
        audio_data: bytes,
        input_rate: int,
    ) -> List[Dict[str, Any]]:
        """Feed audio into the session recognizer and return transcript updates."""
        recognizer = self._ensure_stt_recognizer(session)
        if not recognizer:
            return []

        if input_rate != PCM16_TARGET_RATE:
            logging.debug(
                "🎵 STT INPUT - Resampling %s Hz → %s Hz: %s bytes",
                input_rate,
                PCM16_TARGET_RATE,
                len(audio_data),
            )
            audio_bytes = self.audio_processor.resample_audio(
                audio_data, input_rate, PCM16_TARGET_RATE, "raw", "raw"
            )
        else:
            audio_bytes = audio_data

        updates: List[Dict[str, Any]] = []

        try:
            session.last_audio_at = asyncio.get_running_loop().time()
        except RuntimeError:
            session.last_audio_at = 0.0
        
        # Calculate RMS to detect silent audio (only in debug mode)
        if DEBUG_AUDIO_FLOW:
            try:
                import struct
                import math
                samples = struct.unpack(f"{len(audio_bytes)//2}h", audio_bytes)
                squared_sum = sum(s*s for s in samples)
                rms = math.sqrt(squared_sum / len(samples)) if samples else 0
                logging.debug(
                    "🎤 FEEDING VOSK call_id=%s bytes=%d samples=%d rms=%.2f",
                    session.call_id or "unknown",
                    len(audio_bytes),
                    len(samples),
                    rms,
                )
            except Exception as rms_exc:
                logging.debug("RMS calculation failed: %s", rms_exc)

        try:
            has_final = recognizer.AcceptWaveform(audio_bytes)
            if DEBUG_AUDIO_FLOW:
                logging.debug(
                    "🎤 VOSK PROCESSED call_id=%s has_final=%s",
                    session.call_id or "unknown",
                    has_final,
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            logging.error("STT recognition failed: %s", exc, exc_info=True)
            return updates

        if has_final:
            try:
                result = json.loads(recognizer.Result() or "{}")
            except json.JSONDecodeError:
                result = {}
            text = (result.get("text") or "").strip()
            confidence = result.get("confidence")
            logging.info(
                "📝 STT RESULT - Vosk final transcript: '%s'",
                text,
            )
            updates.append(
                {
                    "text": text,
                    "is_final": True,
                    "is_partial": False,
                    "confidence": confidence,
                }
            )
            return updates

        # Emit partial result to mirror remote streaming providers.
        try:
            partial_payload = json.loads(recognizer.PartialResult() or "{}")
        except json.JSONDecodeError:
            partial_payload = {}
        partial_text = (partial_payload.get("partial") or "").strip()
        if partial_text != session.last_partial or not session.partial_emitted:
            session.last_partial = partial_text
            session.partial_emitted = True
            logging.debug(
                "📝 STT PARTIAL - '%s'",
                partial_text,
            )
            updates.append(
                {
                    "text": partial_text,
                    "is_final": False,
                    "is_partial": True,
                    "confidence": None,
                }
            )

        return updates

    def _normalize_mode(self, data_mode: Optional[str], session: SessionContext) -> str:
        if data_mode and data_mode in SUPPORTED_MODES:
            session.mode = data_mode
            return data_mode
        return session.mode

    async def _send_json(self, websocket, payload: Dict[str, Any]) -> bool:
        try:
            await websocket.send(json.dumps(payload))
            return True
        except ConnectionClosed:
            logging.warning(
                "🌐 WS CLOSED - Failed to send JSON payload type=%s", payload.get("type")
            )
            return False

    async def _send_bytes(self, websocket, data: bytes) -> bool:
        if not data:
            return True
        try:
            await websocket.send(data)
            return True
        except ConnectionClosed:
            logging.warning("🌐 WS CLOSED - Failed to send binary payload (%s bytes)", len(data))
            return False

    async def _emit_stt_result(
        self,
        websocket,
        transcript: str,
        session: SessionContext,
        request_id: Optional[str],
        *,
        source_mode: str,
        is_final: bool,
        is_partial: bool,
        confidence: Optional[float],
    ) -> bool:
        payload = {
            "type": "stt_result",
            "text": transcript,
            "call_id": session.call_id,
            "mode": source_mode,
            "is_final": is_final,
            "is_partial": is_partial,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        if request_id:
            payload["request_id"] = request_id
        return await self._send_json(websocket, payload)

    async def _emit_llm_response(
        self,
        websocket,
        llm_response: str,
        session: SessionContext,
        request_id: Optional[str],
        *,
        source_mode: str,
    ) -> bool:
        text = (llm_response or "").strip()
        if not text:
            logging.info(
                "🤖 LLM RESULT - Empty response, using fallback call_id=%s mode=%s",
                session.call_id,
                source_mode,
            )
            text = "I'm here to help you."
        payload = {
            "type": "llm_response",
            "text": text,
            "call_id": session.call_id,
            "mode": source_mode,
        }
        if request_id:
            payload["request_id"] = request_id
        return await self._send_json(websocket, payload)

    async def _emit_tts_audio(
        self,
        websocket,
        audio_bytes: bytes,
        session: SessionContext,
        request_id: Optional[str],
        *,
        source_mode: str,
    ) -> None:
        if request_id:
            # Milestone7: emit metadata event for selective TTS while keeping binary transport.
            metadata = {
                "type": "tts_audio",
                "call_id": session.call_id,
                "mode": source_mode,
                "request_id": request_id,
                "encoding": "mulaw",
                "sample_rate_hz": ULAW_SAMPLE_RATE,
                "byte_length": len(audio_bytes or b""),
            }
            if not await self._send_json(websocket, metadata):
                return
        if audio_bytes:
            await self._send_bytes(websocket, audio_bytes)

    async def _handle_final_transcript(
        self,
        websocket,
        session: SessionContext,
        request_id: Optional[str],
        *,
        mode: str,
        text: str,
        confidence: Optional[float],
        idle_promoted: bool = False,
    ) -> None:
        clean_text = (text or "").strip()
        normalized_text = _normalize_text(clean_text)
        last_final_text = session.last_final_text
        last_final_norm = session.last_final_norm
        last_final_at = session.last_final_at
        recent_empty = (
            last_final_text == ""
            and last_final_at > 0.0
            and monotonic() - last_final_at < 0.5
        )
        if not clean_text:
            reason = "idle-timeout" if idle_promoted else "recognizer-final"
            if mode == "stt":
                if recent_empty or (idle_promoted and last_final_text == ""):
                    logging.info(
                        "📝 STT FINAL SUPPRESSED - Repeated empty transcript call_id=%s mode=%s",
                        session.call_id,
                        mode,
                    )
                    return
                # For STT mode, emit an empty final so the engine adapter can complete cleanly.
                logging.info(
                    "📝 STT FINAL - Emitting empty transcript call_id=%s mode=%s reason=%s",
                    session.call_id,
                    mode,
                    reason,
                )
                if await self._emit_stt_result(
                    websocket,
                    "",
                    session,
                    request_id,
                    source_mode=mode,
                    is_final=True,
                    is_partial=False,
                    confidence=confidence,
                ):
                    self._reset_stt_session(session, "")
                return
            # For llm/full modes, continue suppressing empty finals to avoid downstream work
            logging.info(
                "📝 STT FINAL SUPPRESSED - Empty transcript call_id=%s mode=%s reason=%s",
                session.call_id,
                mode,
                reason,
            )
            return

        if idle_promoted and normalized_text and normalized_text == last_final_norm:
            logging.info(
                "📝 STT FINAL SUPPRESSED - Duplicate idle transcript call_id=%s mode=%s text=%s",
                session.call_id,
                mode,
                clean_text[:80],
            )
            return

        reason = "idle-timeout" if idle_promoted else "recognizer-final"
        logging.info(
            "📝 STT FINAL - Emitting transcript call_id=%s mode=%s reason=%s confidence=%s preview=%s",
            session.call_id,
            mode,
            reason,
            confidence,
            clean_text[:80],
        )

        stt_sent = await self._emit_stt_result(
            websocket,
            clean_text,
            session,
            request_id,
            source_mode=mode,
            is_final=True,
            is_partial=False,
            confidence=confidence,
        )

        if stt_sent:
            self._reset_stt_session(session, clean_text)

        if mode == "stt":
            return

        # LLM path for llm/full modes: instrument, guard with timeout, and fallback on failure
        if normalized_text and session.llm_user_turns:
            last_turn_norm = _normalize_text(session.llm_user_turns[-1])
            if normalized_text == last_turn_norm:
                logging.info(
                    "🧠 LLM SKIPPED - Duplicate final transcript call_id=%s mode=%s text=%s",
                    session.call_id,
                    mode,
                    clean_text[:80],
                )
                return

        prompt_text, prompt_tokens, truncated, raw_tokens = self._prepare_llm_prompt(
            session, clean_text
        )
        logging.info(
            "🧠 LLM PROMPT - call_id=%s tokens=%s raw_tokens=%s max_ctx=%s turns=%s truncated=%s preview=%s",
            session.call_id,
            prompt_tokens,
            raw_tokens,
            self.llm_context,
            len(session.llm_user_turns),
            truncated,
            prompt_text[:120],
        )

        infer_timeout = float(os.getenv("LOCAL_LLM_INFER_TIMEOUT_SEC", "20.0"))
        try:
            logging.info(
                "🧠 LLM START - Generating response call_id=%s mode=%s preview=%s",
                session.call_id,
                mode,
                prompt_text[:80],
            )
            llm_response = await asyncio.wait_for(
                self.process_llm(prompt_text), timeout=infer_timeout
            )
        except asyncio.TimeoutError:
            logging.warning(
                "🧠 LLM TIMEOUT - Using fallback call_id=%s mode=%s timeout=%.1fs",
                session.call_id,
                mode,
                infer_timeout,
            )
            llm_response = "I'm here to help you. Could you please repeat that?"
        except Exception as exc:
            logging.error(
                "🧠 LLM ERROR - Using fallback call_id=%s mode=%s error=%s",
                session.call_id,
                mode,
                str(exc),
                exc_info=True,
            )
            llm_response = "I'm here to help you. Could you please repeat that?"

        if not await self._emit_llm_response(
            websocket,
            llm_response,
            session,
            request_id,
            source_mode=mode if mode != "full" else "llm",
        ):
            return

        if mode == "full" and llm_response:
            audio_response = await self.process_tts(llm_response)
            await self._emit_tts_audio(
                websocket,
                audio_response,
                session,
                request_id,
                source_mode="full",
            )

    def _schedule_idle_finalizer(
        self,
        websocket,
        session: SessionContext,
        request_id: Optional[str],
        mode: str,
    ) -> None:
        self._cancel_idle_timer(session)
        session.last_request_meta = {"mode": mode, "request_id": request_id}

        async def _idle_promote() -> None:
            try:
                timeout_sec = max(self.buffer_timeout_ms / 1000.0, 0.1)
                await asyncio.sleep(timeout_sec)
                recognizer = session.recognizer
                if recognizer is None:
                    return
                try:
                    result = json.loads(recognizer.FinalResult() or "{}")
                except json.JSONDecodeError:
                    result = {}
                text = (result.get("text") or "").strip()
                confidence = result.get("confidence")
                logging.info(
                    "📝 STT IDLE FINALIZER - Triggering final after %s ms silence call_id=%s mode=%s preview=%s",
                    self.buffer_timeout_ms,
                    session.call_id,
                    mode,
                    text[:80],
                )
                await self._handle_final_transcript(
                    websocket,
                    session,
                    request_id,
                    mode=mode,
                    text=text,
                    confidence=confidence,
                    idle_promoted=True,
                )
            except asyncio.CancelledError:
                return
            finally:
                session.idle_task = None

        session.idle_task = asyncio.create_task(_idle_promote())

    async def _handle_audio_payload(
        self,
        websocket,
        session: SessionContext,
        data: Dict[str, Any],
        *,
        incoming_bytes: Optional[bytes] = None,
    ) -> None:
        """
        Decode audio payload and route it through the pipeline according to the requested mode.
        """
        mode = self._normalize_mode(data.get("mode"), session)
        request_id = data.get("request_id")
        call_id = data.get("call_id")
        if call_id:
            session.call_id = call_id
        
        if DEBUG_AUDIO_FLOW:
            logging.debug(
                "🎤 AUDIO PAYLOAD RECEIVED call_id=%s mode=%s request_id=%s",
                call_id or "unknown",
                mode,
                request_id or "none",
            )

        if incoming_bytes is None:
            encoded_audio = data.get("data", "")
            if not encoded_audio:
                logging.warning("Audio payload missing 'data'")
                return
            try:
                audio_bytes = base64.b64decode(encoded_audio)
                if DEBUG_AUDIO_FLOW:
                    logging.debug(
                        "🎤 AUDIO DECODED call_id=%s bytes=%d base64_len=%d",
                        call_id or "unknown",
                        len(audio_bytes),
                        len(encoded_audio),
                    )
            except Exception as exc:
                logging.warning("Failed to decode base64 audio payload: %s", exc)
                return
        else:
            audio_bytes = incoming_bytes
            logging.info(
                "🎤 AUDIO (binary) call_id=%s bytes=%d",
                call_id or "unknown",
                len(audio_bytes),
            )

        if not audio_bytes:
            logging.debug("Audio payload empty after decoding")
            return

        input_rate = int(data.get("rate", PCM16_TARGET_RATE))
        if DEBUG_AUDIO_FLOW:
            logging.debug(
                "🎤 ROUTING TO STT call_id=%s mode=%s bytes=%d rate=%d",
                call_id or "unknown",
                mode,
                len(audio_bytes),
                input_rate,
            )

        stt_modes = {"stt", "llm", "full"}
        if mode in stt_modes:
            session.last_request_meta = {"mode": mode, "request_id": request_id}
            stt_events = await self._process_stt_stream(session, audio_bytes, input_rate)

            final_emitted = False
            partial_seen = False

            for event in stt_events:
                text = event.get("text", "")
                confidence = event.get("confidence")
                if event.get("is_partial"):
                    partial_seen = True
                    await self._emit_stt_result(
                        websocket,
                        text,
                        session,
                        request_id,
                        source_mode=mode,
                        is_final=False,
                        is_partial=True,
                        confidence=confidence,
                    )
                    continue

                if event.get("is_final"):
                    final_emitted = True
                    await self._handle_final_transcript(
                        websocket,
                        session,
                        request_id,
                        mode=mode,
                        text=text,
                        confidence=confidence,
                        idle_promoted=False,
                    )

            if final_emitted:
                return

            # No final yet; keep an idle finalizer running so short utterances resolve.
            if session.recognizer is not None or partial_seen:
                self._schedule_idle_finalizer(websocket, session, request_id, mode)
            return

        if mode == "tts":
            logging.warning("Received audio payload with mode=tts; expected text request. Skipping.")
            return

    async def _handle_tts_request(
        self,
        websocket,
        session: SessionContext,
        data: Dict[str, Any],
    ) -> None:
        text = data.get("text", "").strip()
        if not text:
            logging.warning("TTS request missing 'text'")
            return

        mode = self._normalize_mode(data.get("mode"), session)
        if mode not in {"tts", "full"}:
            # Milestone7: allow callers to force binary TTS even outside default 'tts' mode.
            logging.debug("Overriding session mode to 'tts' for explicit TTS request")
            mode = "tts"

        request_id = data.get("request_id")
        call_id = data.get("call_id")
        if call_id:
            session.call_id = call_id

        audio_response = await self.process_tts(text)
        await self._emit_tts_audio(
            websocket,
            audio_response,
            session,
            request_id,
            source_mode=mode,
        )

    async def _handle_llm_request(
        self,
        websocket,
        session: SessionContext,
        data: Dict[str, Any],
    ) -> None:
        text = data.get("text", "").strip()
        if not text:
            logging.warning("LLM request missing 'text'")
            return

        mode = self._normalize_mode(data.get("mode"), session)
        request_id = data.get("request_id")
        call_id = data.get("call_id")
        if call_id:
            session.call_id = call_id

        logging.info(
            "🧠 LLM REQUEST - Received call_id=%s mode=%s preview=%s",
            session.call_id,
            mode or "llm",
            text[:80],
        )

        infer_timeout = float(os.getenv("LOCAL_LLM_INFER_TIMEOUT_SEC", "20.0"))
        try:
            logging.info(
                "🧠 LLM START - Generating response call_id=%s mode=%s",
                session.call_id,
                mode or "llm",
            )
            llm_response = await asyncio.wait_for(
                self.process_llm(text), timeout=infer_timeout
            )
        except asyncio.TimeoutError:
            logging.warning(
                "🧠 LLM TIMEOUT - Using fallback call_id=%s mode=%s timeout=%.1fs",
                session.call_id,
                mode or "llm",
                infer_timeout,
            )
            llm_response = "I'm here to help you. Could you please repeat that?"
        except Exception as exc:
            logging.error(
                "🧠 LLM ERROR - Using fallback call_id=%s mode=%s error=%s",
                session.call_id,
                mode or "llm",
                str(exc),
                exc_info=True,
            )
            llm_response = "I'm here to help you. Could you please repeat that?"

        await self._emit_llm_response(
            websocket,
            llm_response,
            session,
            request_id,
            source_mode=mode or "llm",
        )

    async def _handle_json_message(self, websocket, session: SessionContext, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logging.warning("❓ Invalid JSON message: %s", message)
            return

        msg_type = data.get("type")
        if not msg_type:
            logging.warning("JSON payload missing 'type': %s", data)
            return

        if msg_type == "set_mode":
            # Milestone7: allow clients to pre-select default mode for subsequent binary frames.
            requested = data.get("mode", DEFAULT_MODE)
            if requested in SUPPORTED_MODES:
                session.mode = requested
                logging.info("Session mode updated to %s", session.mode)
            else:
                logging.warning("Unsupported mode requested: %s", requested)
            call_id = data.get("call_id")
            if call_id:
                session.call_id = call_id
            response = {
                "type": "mode_ready",
                "mode": session.mode,
                "call_id": session.call_id,
            }
            await self._send_json(websocket, response)
            return

        if msg_type == "audio":
            await self._handle_audio_payload(websocket, session, data)
            return

        if msg_type == "tts_request":
            await self._handle_tts_request(websocket, session, data)
            return

        if msg_type == "llm_request":
            await self._handle_llm_request(websocket, session, data)
            return

        if msg_type == "reload_models":
            logging.info("🔄 RELOAD REQUEST - Hot reloading all models...")
            await self.reload_models()
            response = {
                "type": "reload_response",
                "status": "success",
                "message": "All models reloaded successfully",
            }
            await self._send_json(websocket, response)
            return

        if msg_type == "reload_llm":
            logging.info("🔄 LLM RELOAD REQUEST - Hot reloading LLM with optimizations...")
            await self.reload_llm_only()
            response = {
                "type": "reload_response",
                "status": "success",
                "message": (
                    "LLM model reloaded with optimizations (ctx="
                    f"{self.llm_context}, batch={self.llm_batch}, temp={self.llm_temperature}, "
                    f"max_tokens={self.llm_max_tokens})"
                ),
            }
            await self._send_json(websocket, response)
            return

        logging.warning("❓ Unknown message type: %s", msg_type)

    async def _handle_binary_message(self, websocket, session: SessionContext, message: bytes) -> None:
        logging.info("🎵 AUDIO INPUT - Received binary audio: %s bytes", len(message))
        await self._handle_audio_payload(
            websocket,
            session,
            data={"mode": session.mode},
            incoming_bytes=message,
        )

    async def handler(self, websocket):
        """Enhanced WebSocket handler with MVP pipeline and hot reloading"""
        logging.info("🔌 New connection established: %s", websocket.remote_address)
        session = SessionContext()
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    await self._handle_binary_message(websocket, session, message)
                else:
                    await self._handle_json_message(websocket, session, message)
        except Exception as exc:
            logging.error("❌ WebSocket handler error: %s", exc, exc_info=True)
        finally:
            self._reset_stt_session(session)
            logging.info("🔌 Connection closed: %s", websocket.remote_address)


async def main():
    """Main server function"""
    server = LocalAIServer()
    await server.initialize_models()

    async with serve(
        server.handler,
        "0.0.0.0",
        8765,
        ping_interval=30,
        ping_timeout=30,
        max_size=None,
    ):
        logging.info("🚀 Enhanced Local AI Server started on ws://0.0.0.0:8765")
        logging.info(
            "📋 Pipeline: ExternalMedia (8kHz) → STT (16kHz) → LLM → TTS (8kHz uLaw) "
            "- now with #Milestone7 selective mode support"
        )
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    asyncio.run(main())
