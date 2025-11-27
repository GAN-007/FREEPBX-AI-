# Local AI Server WebSocket Protocol

This document describes the WebSocket API exposed by the local AI server at `ws://127.0.0.1:8765` (configurable via `LOCAL_WS_URL`). It supports selective operation modes for STT, LLM, TTS, and a full pipeline.

- Address: `ws://<host>:8765` (default `ws://127.0.0.1:8765`)
- Modes: `full`, `stt`, `llm`, `tts` (default `full`)
- Binary messages: raw PCM16 mono audio frames
- JSON messages: control, text requests, or base64 audio frames

Source of truth:

- Server: `local_ai_server/main.py`
  - Message handling: `_handle_json_message()`, `_handle_binary_message()`
  - Streaming STT: `_process_stt_stream()`
  - LLM pipeline: `process_llm()`, `_emit_llm_response()`
  - TTS pipeline: `process_tts()`, `_emit_tts_audio()`

---

## Connection and Modes

1) Optionally set a default mode for subsequent binary audio frames.

Request:

```json
{
  "type": "set_mode",
  "mode": "stt",           
  "call_id": "1234-5678"   
}
```

Response:

```json
{
  "type": "mode_ready",
  "mode": "stt",
  "call_id": "1234-5678"
}
```

Notes:

- Supported modes: `full`, `stt`, `llm`, `tts`.
- `call_id` is optional but useful for correlating events.
- If you never call `set_mode`, the default is `full`.

---

## Message Types (JSON)

- `set_mode` → Changes session mode; responds with `mode_ready`.
- `audio` → Base64 PCM16 audio for STT/LLM/FULL flows.
- `llm_request` → Ask LLM with text; responds with `llm_response`.
- `tts_request` → Synthesize TTS from text; responds with `tts_audio` metadata then a binary message containing μ-law bytes.
- `reload_models` → Reload all models; responds with `reload_response`.
- `reload_llm` → Reload only LLM; responds with `reload_response`.

### Common fields

- `call_id` (string, optional): Correlate the request with your call/session.
- `request_id` (string, optional): Correlate multiple responses to a single request.

---

## Audio Streaming (STT / FULL)

You can stream audio via:

- JSON frames: `{ "type": "audio", "data": "<base64 pcm16>", "rate": 16000, "mode": "full" }`
- Binary frames: send raw PCM16 bytes directly after `set_mode`.

Recommended input: PCM16 mono at 16 kHz. If you send another rate, the server resamples to 16 kHz internally using sox.

### JSON audio example (full pipeline)

Request:

```json
{
  "type": "audio",
  "mode": "full",
  "rate": 16000,
  "call_id": "1234-5678",
  "request_id": "r1",
  "data": "<base64 pcm16 chunk>"
}
```

Expected responses (sequence):

- `stt_result` (zero or more partials)
- `stt_result` (one final)
- `llm_response`
- `tts_audio` (metadata) + a following binary frame with μ-law 8 kHz audio bytes

Example events:

```json
{ "type": "stt_result", "text": "hello", "call_id": "1234-5678", "mode": "full", "is_final": false, "is_partial": true, "request_id": "r1" }
{ "type": "stt_result", "text": "hello there", "call_id": "1234-5678", "mode": "full", "is_final": true, "is_partial": false, "request_id": "r1", "confidence": 0.91 }
{ "type": "llm_response", "text": "Hi there, how can I help you?", "call_id": "1234-5678", "mode": "llm", "request_id": "r1" }
{ "type": "tts_audio", "call_id": "1234-5678", "mode": "full", "request_id": "r1", "encoding": "mulaw", "sample_rate_hz": 8000, "byte_length": 16347 }
```

Immediately after the `tts_audio` metadata, you will receive one binary WebSocket message containing the μ-law bytes.

### Binary audio example (stt-only)

1) Set mode:

```json
{ "type": "set_mode", "mode": "stt", "call_id": "abc" }
```

2) Send binary PCM16 frames (no JSON wrapper). The server will emit:

```json
{ "type": "stt_result", "text": "...", "call_id": "abc", "mode": "stt", "is_final": false, "is_partial": true }
{ "type": "stt_result", "text": "...", "call_id": "abc", "mode": "stt", "is_final": true,  "is_partial": false }
```

Notes:

- The server uses an idle finalizer (`LOCAL_STT_IDLE_MS`, default 3000 ms) to promote a final transcript if no more audio arrives; duplicate/empty finals are suppressed per `local_ai_server/main.py`.

---

## LLM-only

Request:

```json
{
  "type": "llm_request",
  "text": "What are your business hours?",
  "call_id": "1234-5678",
  "request_id": "q1"
}
```

Response:

```json
{
  "type": "llm_response",
  "text": "We're open from 9am to 5pm, Monday through Friday.",
  "call_id": "1234-5678",
  "mode": "llm",
  "request_id": "q1"
}
```

---

## TTS-only

Request:

```json
{
  "type": "tts_request",
  "text": "Hello, how can I help you?",
  "call_id": "1234-5678",
  "request_id": "t1"
}
```

Response sequence:

```json
{ "type": "tts_audio", "call_id": "1234-5678", "mode": "tts", "request_id": "t1", "encoding": "mulaw", "sample_rate_hz": 8000, "byte_length": 12446 }
```

Then one binary WebSocket message will follow with the μ-law 8 kHz audio bytes suitable for telephony playback.

---

## Hot Reload

- Reload all models:

```json
{ "type": "reload_models" }
```

Response:

```json
{ "type": "reload_response", "status": "success", "message": "All models reloaded successfully" }
```

- Reload LLM only:

```json
{ "type": "reload_llm" }
```

Response:

```json
{ "type": "reload_response", "status": "success", "message": "LLM model reloaded with optimizations (ctx=..., batch=..., temp=..., max_tokens=...)" }
```

---

## Client Examples

Additional example code (including an espeak-ng based lightweight TTS demo) lives under `docs/local-ai-server/examples/`.

### Python: TTS request and save μ-law file

```python
import asyncio, websockets, json

async def tts():
    async with websockets.connect("ws://127.0.0.1:8765", max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "tts_request",
            "text": "Hello, how can I help you?",
            "call_id": "demo",
            "request_id": "t1",
        }))
        meta = json.loads(await ws.recv())
        assert meta["type"] == "tts_audio"
        pcm = await ws.recv()  # binary μ-law bytes
        with open("out.ulaw", "wb") as f:
            f.write(pcm)

asyncio.run(tts())
```

### Python: STT-only with binary frames

```python
import asyncio, websockets, json

async def stt(pcm_bytes):
    async with websockets.connect("ws://127.0.0.1:8765", max_size=None) as ws:
        await ws.send(json.dumps({"type": "set_mode", "mode": "stt", "call_id": "demo"}))
        await ws.recv()  # mode_ready
        await ws.send(pcm_bytes)  # raw PCM16 mono @ 16kHz
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                continue
            evt = json.loads(msg)
            if evt.get("type") == "stt_result" and evt.get("is_final"):
                print("Final:", evt["text"])
                break

# pcm_bytes = ... load/generate 16kHz PCM16 mono
# asyncio.run(stt(pcm_bytes))
```

---

## Environment Variables and Tuning

Server-side (see `local_ai_server/main.py`):

- Models: `LOCAL_STT_MODEL_PATH`, `LOCAL_LLM_MODEL_PATH`, `LOCAL_TTS_MODEL_PATH`
- LLM performance: `LOCAL_LLM_THREADS`, `LOCAL_LLM_CONTEXT`, `LOCAL_LLM_BATCH`, `LOCAL_LLM_MAX_TOKENS`, `LOCAL_LLM_TEMPERATURE`, `LOCAL_LLM_TOP_P`, `LOCAL_LLM_REPEAT_PENALTY`, `LOCAL_LLM_SYSTEM_PROMPT`, `LOCAL_LLM_STOP_TOKENS`
- STT idle promote: `LOCAL_STT_IDLE_MS` (default 3000 ms)
- LLM timeout: `LOCAL_LLM_INFER_TIMEOUT_SEC` (default 20.0)
- Logging: `LOCAL_LOG_LEVEL` (default INFO)

Engine-side (see `config/ai-agent.*.yaml` and `.env.example`):

- `providers.local.ws_url` (default `${LOCAL_WS_URL:-ws://127.0.0.1:8765}`)
- Timeouts: `${LOCAL_WS_CONNECT_TIMEOUT}`, `${LOCAL_WS_RESPONSE_TIMEOUT}`
- Chunk size (ms): `${LOCAL_WS_CHUNK_MS}`

Dependencies:

- sox (used for resampling and μ-law conversion). The container image includes it; if running outside Docker ensure `sox` is installed.

---

## Expected Event Order (Full Pipeline)

For a single request_id and continuous audio segment in `full` mode:

1. `stt_result` (0..N partial)
2. `stt_result` (1 final)
3. `llm_response`
4. `tts_audio` metadata
5. Binary μ-law audio bytes (8 kHz)

Duplicate/empty finals are suppressed; see `_handle_final_transcript()` for details.

---

## Error Responses

When the server encounters an error processing a request, it responds with an error message:

```json
{
  "type": "error",
  "error": "Error description",
  "call_id": "1234-5678",
  "request_id": "r1",
  "details": {
    "error_type": "timeout" | "invalid_request" | "processing_error",
    "component": "stt" | "llm" | "tts",
    "message": "Detailed error message"
  }
}
```

Common error types:

- **timeout**: Component took too long (e.g., LLM inference timeout)
- **invalid_request**: Malformed JSON or missing required fields
- **processing_error**: Internal error during STT/LLM/TTS processing

Example:

```json
{
  "type": "error",
  "error": "LLM inference timeout after 20.0 seconds",
  "call_id": "abc-123",
  "request_id": "llm-1",
  "details": {
    "error_type": "timeout",
    "component": "llm",
    "message": "Increase LOCAL_LLM_INFER_TIMEOUT_SEC or reduce max_tokens"
  }
}
```

---

## Common Issues and Resolutions

- STT returns empty often
  - Cause: utterances too short. Increase chunk size or allow idle finalizer (`LOCAL_STT_IDLE_MS`), ensure PCM16 @ 16kHz input.
- No TTS audio received
  - Ensure you listen for binary frames after `tts_audio` metadata. Mode `tts` or `full` produces metadata followed by a binary message.
- LLM timeout (slow responses)
  - Increase `LOCAL_LLM_INFER_TIMEOUT_SEC`; reduce `LOCAL_LLM_MAX_TOKENS`; use faster model or fewer threads context.
- Model load failures
  - Check paths: `LOCAL_*_MODEL_PATH`; run `make model-setup`; verify models exist inside the container.
- Resample or μ-law conversion errors
  - Ensure `sox` is installed in the environment. Logs will show conversion failures.
- Mode mismatch warnings
  - Sending audio with `mode=tts` is ignored. Use `tts_request` (text in) for TTS.
- High memory usage
  - Lower `LOCAL_LLM_CONTEXT`, `LOCAL_LLM_BATCH`; tune threads; consider a smaller model.

---

## Performance Characteristics

### Models (Default Installation)

- **STT**: Vosk `vosk-model-en-us-0.22` (16kHz native)
  - Size: ~40MB
  - Latency: 100-300ms (streaming with partials)
  - Accuracy: Good for conversational speech

- **LLM**: Phi-3-mini-4k `phi-3-mini-4k-instruct.Q4_K_M.gguf`
  - Size: 2.3GB
  - Warmup: ~110 seconds (first load)
  - Inference: 2-5 seconds per response
  - Context: 4096 tokens
  - Quality: Good for conversational AI (better than TinyLlama, less than GPT-4)

- **TTS**: Piper `en_US-lessac-medium.onnx` (22kHz native)
  - Size: ~60MB
  - Latency: 500-1000ms
  - Output: μ-law @ 8kHz
  - Quality: Natural, clear voice

### Typical Latencies (End-to-End)

- **STT only**: 100-300ms
- **LLM inference**: 2-5 seconds (depends on response length)
- **TTS synthesis**: 500-1000ms
- **Full pipeline turn**: 3-7 seconds total

### Concurrency

- **Single server**: ~10-20 concurrent calls (CPU-bound)
- **Bottleneck**: LLM inference (most CPU intensive)
- **Scaling**: Deploy multiple containers with load balancer

---

## Versioning and Compatibility

- Protocol is stable for v4.0 GA track. Message types and fields correspond to the implementation in `local_ai_server/main.py`.
- The engine's local provider uses the same contract to support pipelines defined in `config/ai-agent.*.yaml`.
