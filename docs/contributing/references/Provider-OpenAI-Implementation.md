# OpenAI Realtime API Implementation

Technical implementation for OpenAI Realtime API integration.

## Overview

**File**: `src/providers/openai.py`

OpenAI Realtime: WebSocket-based streaming with built-in STT, LLM (GPT-4o-realtime), and TTS.

## Configuration

```yaml
provider_name: openai
openai:
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-realtime-preview-2024-10-01
  voice: alloy
  modalities: ["audio", "text"]
  turn_detection:
    type: server_vad
  tools: []
```

## Tool Execution (Critical)

### Schema Format

**Flat structure** (different from Chat Completions API):

```python
# ✅ CORRECT (Realtime API):
{
    "type": "function",
    "name": "hangup_call",
    "description": "End the call",
    "parameters": {...}
}

# ❌ WRONG (Chat Completions format):
{
    "type": "function",
    "function": {  # Nested - DON'T DO THIS
        "name": "hangup_call",
        ...
    }
}
```

### Implementation

Use `to_openai_realtime_schema()` method:

```python
# src/tools/adapters/openai.py
schemas = self.registry.to_openai_realtime_schema()  # Flat format
```

**Error if wrong format**:
```
Missing required parameter: 'session.tools[0].name'
```

## VAD Configuration (Critical)

**Recommended Setting**:

```yaml
vad:
  webrtc_aggressiveness: 1  # CRITICAL for OpenAI
  enhanced_enabled: true
  confidence_threshold: 0.6
  energy_threshold: 1500
```

**Why webrtc_aggressiveness: 1**:
- Level 0 is TOO SENSITIVE (detects echo as speech)
- Causes gate flutter (50+ open/close per call)
- OpenAI has built-in server-side echo cancellation
- Level 1 ignores echo, lets OpenAI handle turn-taking

## Modalities

**MUST include both `audio` and `text`**:

```yaml
modalities: ["audio", "text"]  # Required
```

Audio-only NOT supported:
```
Invalid modalities: ['audio'].
Supported combinations: ['text'] and ['audio', 'text'].
```

## Key Events

### Session Events
- `session.created`: Session initialized
- `session.updated`: Configuration applied

### Conversation Events
- `conversation.item.created`: User/agent turn
- `input_audio_buffer.speech_started`: User speaking
- `input_audio_buffer.speech_stopped`: User stopped

### Response Events
- `response.created`: Agent response started
- `response.audio.delta`: Audio chunk
- `response.audio.done`: Audio complete
- `response.done`: Response fully complete

### Function Calling
- `response.function_call_arguments.delta`: Tool args streaming
- `response.function_call_arguments.done`: Args complete

## Audio Gating

**Gate on**: `response.audio.delta` (agent speaking)  
**Gate off**: `response.done` (response complete)

**Not**: `response.audio.done` (can arrive before all audio processed)

## Common Issues

### Echo/Self-Interruption

**Symptom**: Agent cuts itself off, echo loops

**Fix**: Set `webrtc_aggressiveness: 1`

### Tools Not Working

**Symptom**: AI says "goodbye" but call doesn't hang up

**Fix**: Verify tool schema format (flat, not nested)

**Check logs**:
```
✅ "OpenAI session configured with 6 tools"
✅ "OpenAI function call detected: hangup_call"
```

### Partial Audio

**Symptom**: Greeting cuts off after 1-2 seconds

**Known Limitation**: OpenAI may generate partial audio sometimes. Cannot be prevented.

**Mitigation**: Handle gracefully with VAD re-enable on `response.done`

## Debugging

### Enable Debug Logging

```yaml
logging:
  level: DEBUG
  providers:
    openai: DEBUG
```

### Success Patterns

```
[info] OpenAI Realtime session created
[info] Session updated with tools: 6
[info] response.audio.delta: 3200 bytes
[info] OpenAI function call detected: hangup_call
[info] response.done received
```

### Error Patterns

```
[error] missing_required_parameter: session.tools[0].name
→ Tool schema format wrong (using nested instead of flat)

[warn] AI used farewell phrase without invoking hangup_call tool
→ Tools not configured or schema invalid
```

## Performance

- **Latency**: ~300-800ms first audio
- **Quality**: Excellent (alloy, echo, fable, onyx, nova, shimmer voices)
- **Stability**: High (handles reconnection)
- **Cost**: Pay-per-token + audio time

## References

- OpenAI Realtime API Docs: https://platform.openai.com/docs/guides/realtime
- User Setup: `docs/Provider-OpenAI-Setup.md` (to be created)
- Golden Baseline: `docs/case-studies/OpenAI-Realtime-Golden-Baseline.md`
- Common Pitfalls: `docs/contributing/COMMON_PITFALLS.md#pitfall-1`
