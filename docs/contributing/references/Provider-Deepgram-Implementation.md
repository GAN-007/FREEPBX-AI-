# Deepgram Provider Implementation & API Reference

Complete technical reference for Deepgram Voice Agent integration in Asterisk AI Voice Agent.

## Overview

**File**: `src/providers/deepgram.py`

Deepgram Voice Agent is a monolithic real-time streaming provider with built-in STT (Speech-to-Text), LLM reasoning, and TTS (Text-to-Speech) in a single WebSocket connection.

**Key Characteristics**:
- Single WebSocket endpoint for bidirectional audio + control
- Native function calling support (tool execution)
- Low latency (1-2 seconds response time)
- Supports telephony audio formats (μ-law @ 8kHz)

---

## Configuration

### YAML Configuration

```yaml
providers:
  deepgram:
    api_key: ${DEEPGRAM_API_KEY}
    enabled: true
    greeting: "Hello! How can I help you?"
    
    # LLM Configuration
    llm_model: nova-2-conversationalai
    llm_temperature: 1.0
    
    # Voice Configuration
    tts_voice: aura-asteria-en
    
    # Audio Configuration
    encoding: mulaw
    sample_rate: 8000
    enable_endpoint: true
    
    # Conversation Settings
    context_handling: extended
    interim_results: true
```

### Internal Agent Configuration

The provider translates our YAML config into Deepgram's agent configuration:

```python
agent_config = {
    "type": "Settings",
    "audio": {
        "input": {
            "encoding": "mulaw",  # or "linear16"
            "sample_rate": 8000   # or 16000, 24000
        },
        "output": {
            "encoding": "linear16",
            "sample_rate": 24000,
            "container": "none"
        }
    },
    "agent": {
        "language": "en-US",
        "listen": {
            "provider": {
                "type": "deepgram",
                "model": "nova-2-conversationalai",
                "smart_format": False
            }
        },
        "think": {
            "provider": {
                "type": "open_ai",
                "model": "gpt-4o",
                "temperature": 0.6
            },
            "functions": [...]  # Tool schemas
        },
        "speak": {
            "provider": {
                "type": "deepgram",
                "model": "aura-asteria-en"
            }
        },
        "greeting": "Hello! How can I help you?"
    }
}
```

---

## Function Calling (Critical)

### Field Name: `functions` not `tools`

**CRITICAL**: Deepgram uses `functions` field name, NOT `tools` (OpenAI naming).

```python
# ✅ CORRECT (Deepgram):
agent_config["agent"]["think"]["functions"] = [...]

# ❌ WRONG (OpenAI naming):
agent_config["agent"]["think"]["tools"] = [...]
```

### Event Type: `FunctionCallRequest`

**CRITICAL**: Use exact string `"FunctionCallRequest"` for event detection.

```python
# ✅ CORRECT:
if event_type == "FunctionCallRequest":
    # Handle tool call

# ❌ WRONG:
if event_type == "function_call":
```

### Schema Format

Direct array format (no `type` wrapper):

```python
functions = [{
    "name": "transfer",
    "description": "Transfer call to destination",
    "parameters": {
        "type": "object",
        "properties": {
            "destination": {
                "type": "string",
                "description": "Extension, queue, or ring group"
            }
        },
        "required": ["destination"]
    }
}]
```

**NOT** wrapped in `{"type": "function", "function": {...}}` like OpenAI Chat Completions.

### Function Call Response

Must respond within 10 seconds:

```python
response = {
    "type": "FunctionCallResponse",
    "function_call_id": event["function_call_id"],
    "output": json.dumps(result)
}
await ws.send(json.dumps(response))
```

---

## WebSocket API

### Endpoint

```python
# Via SDK (recommended):
from deepgram import DeepgramClient
connection = deepgram.agent.v1.connect()

# Raw WebSocket endpoints:
wss://agent.deepgram.com/v1/agent
wss://agent.deepgram.com/v1/agent/converse
```

**Recommendation**: Use SDK to abstract WebSocket details.

### Authentication

```http
Authorization: Token <YOUR_DEEPGRAM_API_KEY>
```

Token-based short-lived access also supported via Token API.

### Connection Flow

1. **Connect** to WebSocket endpoint with auth header
2. **Send** `Settings` message with agent configuration
3. **Wait** for `SettingsApplied` acknowledgment
4. **Stream** audio chunks (μ-law or PCM16)
5. **Receive** agent audio, transcripts, and function calls
6. **Send** keep-alive messages periodically

---

## Server-to-Client Messages

### `Welcome`
```json
{
  "type": "Welcome",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### `SettingsApplied`
```json
{
  "type": "SettingsApplied"
}
```

Confirms agent configuration accepted. **Wait for this before streaming audio.**

### `ConversationText`
```json
{
  "type": "ConversationText",
  "role": "user",
  "content": "What's the weather like today?"
}
```

### `UserStartedSpeaking`
```json
{
  "type": "UserStartedSpeaking"
}
```

User speech detected - useful for barge-in handling.

### `AgentAudio`
Binary or JSON-wrapped audio frames matching negotiated `audio.output` format (e.g., linear16 @ 24000 Hz).

### `AgentAudioDone`
```json
{
  "type": "AgentAudioDone"
}
```

Agent finished speaking - response complete.

### `FunctionCallRequest`
```json
{
  "type": "FunctionCallRequest",
  "function_call_id": "call_abc123",
  "function_name": "transfer",
  "input": "{\"destination\": \"sales\"}"
}
```

**CRITICAL**: Must respond with `FunctionCallResponse` within 10 seconds.

### `Error`
```json
{
  "type": "Error",
  "message": "Error details..."
}
```

---

## Client-to-Server Messages

### `Settings` (Initial Configuration)

Send immediately after connecting:

```json
{
  "type": "Settings",
  "tags": ["production", "telephony"],
  "audio": {
    "input": {"encoding": "mulaw", "sample_rate": 8000},
    "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"}
  },
  "agent": {
    "language": "en-US",
    "listen": {"provider": {"type": "deepgram", "model": "nova-2-conversationalai"}},
    "think": {"provider": {"type": "open_ai", "model": "gpt-4o"}, "functions": []},
    "speak": {"provider": {"type": "deepgram", "model": "aura-asteria-en"}},
    "greeting": "Hello! How can I help you?"
  }
}
```

### `FunctionCallResponse`

```json
{
  "type": "FunctionCallResponse",
  "function_call_id": "call_abc123",
  "output": "{\"status\": \"success\", \"message\": \"Transfer initiated\"}"
}
```

### Keep-Alive Messages

SDK sends periodic keep-alive (e.g., `AgentKeepAlive`). For raw WebSocket, mirror SDK behavior.

---

## Audio Formats

### Recommended: PCM16 @ 16000 Hz

```yaml
encoding: linear16
sample_rate: 16000
```

**Benefits**:
- Matches Deepgram's internal format (no transcoding)
- Higher quality than μ-law
- Lower latency

### Telephony: μ-law @ 8000 Hz

```yaml
encoding: mulaw
sample_rate: 8000
```

**Benefits**:
- Native telephony format (no conversion from Asterisk)
- Lower bandwidth
- Standard for PSTN

### Output: PCM16 @ 24000 Hz

```yaml
audio.output:
  encoding: linear16
  sample_rate: 24000
  container: none
```

**Our Implementation**: Accept PCM16 @ 24000 from Deepgram, then:
1. Apply DC-blocker (single-pole high-pass filter)
2. Resample to 8000 Hz
3. Convert to μ-law
4. Stream to Asterisk via AudioSocket

---

## Key Events & Handling

### Event Flow

```
User Speech → UserStartedSpeaking
              ↓
          ConversationText (transcript)
              ↓
          FunctionCallRequest (if tool needed)
              ↓
          FunctionCallResponse (our response)
              ↓
          AgentAudio (TTS response)
              ↓
          AgentAudioDone
```

### Barge-In Handling

When `UserStartedSpeaking` received while agent is speaking:
1. Stop current TTS playback
2. Clear audio buffer
3. Prepare for new user input

---

## Common Issues & Fixes

### Issue: Function Calls Not Working

**Symptoms**:
- AI doesn't invoke tools
- No `FunctionCallRequest` events
- Tools available but never called

**Cause**: Using `tools` instead of `functions` field name

**Fix**:
```python
# Change this:
agent_config["agent"]["think"]["tools"] = functions

# To this:
agent_config["agent"]["think"]["functions"] = functions
```

### Issue: Function Call Event Not Detected

**Symptoms**:
- Events received but not processed
- Tool execution never triggered

**Cause**: Wrong event type string

**Fix**:
```python
# Change this:
if event_type == "function_call":

# To this:
if event_type == "FunctionCallRequest":
```

### Issue: Function Call Timeout

**Symptoms**:
- `Error` message: "Function call timeout"
- Tool execution incomplete

**Cause**: Not responding within 10 seconds

**Fix**: Send `FunctionCallResponse` immediately:
```python
async def handle_function_call(event):
    result = execute_tool(event["function_name"], event["input"])
    
    # Send response immediately (don't wait)
    response = {
        "type": "FunctionCallResponse",
        "function_call_id": event["function_call_id"],
        "output": json.dumps(result)
    }
    await ws.send(json.dumps(response))
```

### Issue: Low RMS Warnings

**Symptoms**:
- Log spam: "Low RMS value detected"
- No actual audio issue

**Cause**: Natural silence periods detected as low energy

**Fix**: Filter in logging config or ignore (not a real problem)

### Issue: Garbled Audio

**Symptoms**:
- Static or distorted audio
- Incomprehensible speech

**Cause**: Encoding/sample rate mismatch

**Fix**: Verify configuration matches:
```yaml
# Asterisk → ai-engine → Deepgram
encoding: mulaw
sample_rate: 8000

# Deepgram → ai-engine → Asterisk
audio.output.encoding: linear16
audio.output.sample_rate: 24000
```

---

## Audio Pipeline (Telephony)

### Input Path (Asterisk → Deepgram)

```
Asterisk AudioSocket (μ-law 8kHz)
    ↓
ai-engine receives
    ↓
Send to Deepgram (μ-law 8kHz)
```

**Configuration**:
```python
audio_config = {
    "input": {
        "encoding": "mulaw",
        "sample_rate": 8000
    }
}
```

### Output Path (Deepgram → Asterisk)

```
Deepgram sends PCM16 24kHz
    ↓
ai-engine receives
    ↓
DC-blocker filter
    ↓
Resample to 8kHz
    ↓
Convert to μ-law
    ↓
Asterisk AudioSocket
```

**Processing Details**:
- **DC-blocker**: Single-pole high-pass filter removes DC offset
- **Resampler**: Polyphase resampling (24000 → 8000)
- **μ-law compand**: ITU-T G.711 μ-law encoding
- **Frame size**: 20ms (160 samples @ 8kHz)

---

## Best Practices for Telephony

### Audio Correctness
- Wait for `SettingsApplied` before streaming audio
- Keep wire format explicit and consistent
- Apply DC-blocker in PCM16 before μ-law conversion
- Use soft limiting to avoid clipping

### Pacing & Timing
- Use 20ms audio frames
- Warm-up buffer: 300-400ms
- Low-watermark: 200-300ms
- Pacer must align to fixed 20ms ticks and correct accumulated error

### Diagnostics
- Log acknowledgment of `audio.output.*` settings
- Capture first 200ms of audio for analysis
- Log first-chunk inferred encoding/rate
- Save wire audio (`out-`/`mix-`) for RCA (Root Cause Analysis)

### Function Calling
- Use `functions` field name (not `tools`)
- Detect `FunctionCallRequest` event type (exact string)
- Respond within 10 seconds with `FunctionCallResponse`
- Include full tool schema in Settings message

---

## Production Validation

### Golden Baseline

**Validated**: November 17, 2025  
**Call ID**: 1763411551.5763  
**Status**: ✅ Production Ready

**Performance Metrics**:
- Response Latency: 1-2 seconds ✅
- Audio Quality: Clear, natural ✅
- Duplex Communication: Full bidirectional ✅
- Tool Execution: 2/2 tools tested (transcript + hangup) ✅
- Conversation Tracking: Full history saved ✅

**See**: `docs/case-studies/Deepgram-Agent-Golden-Baseline.md`

---

## References

### Deepgram Documentation
- Configure Voice Agent: https://developers.deepgram.com/docs/configure-voice-agent
- Settings Configuration: https://developers.deepgram.com/docs/voice-agent-settings-configuration
- Getting Started: https://developers.deepgram.com/docs/voice-agent
- Audio & Playback: https://developers.deepgram.com/docs/voice-agent-audio-playback
- Function Calling: https://developers.deepgram.com/docs/voice-agents-function-calling
- Authentication: https://developers.deepgram.com/reference/authentication

### Project Documentation
- User Setup: `docs/Provider-Deepgram-Setup.md`
- Common Pitfalls: `docs/contributing/COMMON_PITFALLS.md#deepgram-function-calling`
- Golden Baseline: `docs/case-studies/Deepgram-Agent-Golden-Baseline.md`

---

## Ambiguities & SDK Notes

### Raw WebSocket URL
Multiple URLs appear in documentation:
- `wss://agent.deepgram.com/v1/agent`
- `wss://agent.deepgram.com/v1/agent/converse`

**Recommendation**: Use SDK's `connect()` method to abstract endpoint selection.

### AgentAudio Schema
Full JSON schema for `AgentAudio` event not specified in public docs. SDK abstracts payload format.

### Encoding/Rate Lists
Supported encodings and sample rates presented via examples, not exhaustive enumerations. Documented combinations:
- Input: `linear16` (8000, 16000, 24000), `mulaw` (8000)
- Output: `linear16` (24000), `mp3` (with bitrate/container)

**Note**: Legacy reference path `reference/voice-agent/agent` can 404. Prefer docs links above for canonical information.
