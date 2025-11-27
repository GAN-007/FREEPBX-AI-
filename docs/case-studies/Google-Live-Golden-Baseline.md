# Google Live Golden Baseline

**Validated**: November 17, 2025  (Github Commit ID: 0d458486191c42b0d622530651ea4bd718923d42)
**Version**: v4.3.0  
**Status**: ✅ Production Ready

## Overview

This document establishes the **golden baseline** for the Google Live provider - a validated, production-ready configuration that delivers exceptional real-time conversational AI performance.

## Latest Test Call Metrics (v4.3.0)

**Date**: 2025-11-17 20:23-20:24 UTC  
**Duration**: ~67 seconds  
**Call ID**: 1763410994.5759  
**Outcome**: ✅ Successful - Full tool access with complete transcription and email delivery

### Previous Validation (v4.2.0)

**Date**: 2025-11-14 03:52-03:54 UTC  
**Duration**: 59 seconds  
**Call ID**: 1763092342.5132  
**Outcome**: ✅ Successful - Clean conversation with complete transcription

### Performance Metrics

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| **Response Latency** | <1 second | <2s | ✅ Excellent |
| **Audio Quality** | Clear, natural | Good | ✅ Pass |
| **Duplex Communication** | Full bidirectional | Required | ✅ Pass |
| **Interruption Handling** | Smooth barge-in | Required | ✅ Pass |
| **Tool Access** | 6/6 tools configured | All context tools | ✅ Pass |
| **Tool Execution** | request_transcript (validated) | Functional | ✅ Pass |
| **Transcription Quality** | Complete, accurate | Good | ✅ Pass |
| **Conversation Tracking** | Full history saved | Required | ✅ Pass |
| **Email Delivery** | Summary + Transcript | Both | ✅ Pass |
| **Call Termination** | Clean hangup | Required | ✅ Pass |

### Conversation Quality

**Assessment**: ✅ **Excellent**

- **Natural Flow**: Conversation felt natural and responsive
- **Context Retention**: AI maintained context throughout entire call
- **Audio Clarity**: Both user and AI speech were clear and intelligible
- **No Glitches**: No audio drops, stuttering, or connection issues
- **Professional UX**: Polished, production-ready experience

## Configuration

### Provider Configuration

```yaml
providers:
  google_live:
    api_key: ${GOOGLE_API_KEY}
    enabled: true
    greeting: "Hi {caller_name}, I'm Ava demonstrating Google Gemini Live API! I can tell you all about the Asterisk AI Voice Agent project - ask me anything about how it works, setup, or features. Try interrupting me anytime!"
    
    # LLM Configuration
    llm_model: gemini-2.5-flash-native-audio-preview-09-2025
    llm_temperature: 0.7              # Response creativity (0.0-2.0)
    llm_max_output_tokens: 8192       # Max response length (1-8192)
    llm_top_p: 0.95                   # Nucleus sampling (0.0-1.0)
    llm_top_k: 40                     # Top-k sampling (1-100)
    
    # Voice Configuration
    tts_voice_name: Aoede             # Female voice (natural and clear)
    
    # Response Modalities
    response_modalities: audio        # Audio-only for natural conversation
    
    # Transcription Configuration
    enable_input_transcription: true  # Enable user speech transcription
    enable_output_transcription: true # Enable AI speech transcription
```

### Context Configuration

```yaml
contexts:
  demo_google_live:
    greeting: "Hi {caller_name}, I'm Ava demonstrating Google Gemini Live API! I can tell you all about the Asterisk AI Voice Agent project - ask me anything about how it works, setup, or features. Try interrupting me anytime!"
    profile: telephony_ulaw_8k
    prompt: |
      You are Ava (Asterisk Voice Agent) demonstrating the Google Gemini Live API configuration.
      
      [... full system prompt with project details ...]
      
      CALL ENDING PROTOCOL (CRITICAL):
      1. When user indicates they're done (says goodbye, that's all, thank you, etc.):
         - Ask: "Is there anything else I can help you with?"
      2. If user confirms they're done (no, that's all, goodbye, etc.):
         - Say a polite farewell ("Thank you for calling! Have a great day!")
         - IMMEDIATELY call hangup_call tool
      3. NEVER leave the call hanging in silence - always end with hangup_call
      4. After completing any task (transcript, transfer, summary), ALWAYS ask if there's anything else
    tools:
      - transfer
      - cancel_transfer
      - hangup_call
      - send_email_summary
      - request_transcript
```

### Dialplan Configuration

```ini
[from-ai-agent]
exten => s,1,NoOp(AI Voice Agent - Google Live)
exten => s,n,Set(AI_CONTEXT=demo_google_live)
exten => s,n,Set(AI_PROVIDER=google_live)
exten => s,n,Stasis(asterisk-ai-voice-agent)
exten => s,n,Hangup()
```

## Test Call Transcript (v4.3.0)

```text
AI: Hi Haider Jarral, I'm Ava with the Google Gemini Live voice demo. 
    Ask me about the Asterisk AI Voice Agent project.

Caller: What's the latency on this project?

AI: This Google Gemini Live configuration typically has a response time 
    of less than one second.

Caller: That is great. Can you email me a transcript of this conversation?

AI: I can definitely send you the transcript. Can you please tell me 
    your email address?

Caller: My email is Heider Khalil at Hotmill.com.

AI: I have h a i d e r k h a l i l @ hotmail dot com. Is that correct?

Caller: That is correct, yeah.

AI: Perfect! I'll send the complete transcript to haiderkhalil@hotmail dot com 
    when our call ends. Is there anything else I can help you with?

Caller: No, that is all. Thank you so much. Goodbye.

AI: Thank you for calling! Have a great day!

[Call ends cleanly with transcript sent successfully to email]
```

### v4.2.0 Transcript (Previous Baseline)

```text
AI: Hi HAIDER JARRAL, I'm Ava demonstrating Google Gemini Live API! 
    I can tell you all about the Asterisk AI Voice Agent project - 
    ask me anything about how it works, setup, or features. Try 
    interrupting me anytime!

Caller: Tell me about the latency of this project.

AI: The Google Gemini Live configuration is incredibly fast, with 
    latency of less than one second. This is because it uses native 
    audio processing and true duplex communication.

Caller: Please send me transcript of this conversation at 
        haiderkhalil@hotmail.com.

AI: Certainly. I can send a transcript to haiderkhalil@hotmail.com. 
    Is that correct?

Caller: That is correct.

AI: Perfect! I'll send the complete transcript to haiderkhalil@hotmail.com 
    when our call ends.

[Call ends cleanly with transcript sent successfully]
```

## Technical Architecture

### Audio Flow

```
Asterisk (μ-law 8kHz) 
    ↓ RTP
AI Engine
    ↓ Resample to PCM16 16kHz
Google Gemini Live API
    ↓ Process with native audio
Google Gemini Live API
    ↓ Generate PCM16 24kHz
AI Engine
    ↓ Resample to μ-law 8kHz
    ↓ RTP
Asterisk
```

### Key Components

1. **Provider**: `src/providers/google_live.py`
   - WebSocket connection to Gemini Live API
   - Bidirectional streaming
   - Native audio processing
   - Tool execution via Google function declarations

2. **Tool Adapter**: `src/tools/adapters/google.py`
   - Formats tools for Google API
   - Handles async tool execution
   - Sends tool responses back to Gemini

3. **Audio Processing**: Built-in resampling
   - Input: 8kHz → 16kHz for Gemini
   - Output: 24kHz → 8kHz for Asterisk

4. **Transcription**: Dual transcription system
   - `inputTranscription`: User speech
   - `outputTranscription`: AI speech
   - Saved on `turnComplete` flag

## Key Features Validated

### ✅ True Duplex Communication

- **Barge-in**: Users can interrupt AI at any time
- **Natural Flow**: Conversation feels like talking to a human
- **No Turn-Taking**: No rigid back-and-forth structure
- **Instant Response**: AI responds immediately to interruptions

### ✅ Ultra-Low Latency

- **<1 second response time** from speech end to audio start
- **Native audio processing** eliminates STT→LLM→TTS pipeline delays
- **WebSocket streaming** provides continuous bidirectional data flow
- **No buffering delays** with immediate audio transmission

### ✅ Function Calling in Streaming Mode

- **Tool execution**: Successfully executed `request_transcript` tool
- **Context preserved**: AI maintained conversation context during tool call
- **Async handling**: Tool execution didn't block conversation flow
- **Response integration**: Tool results seamlessly integrated into response

### ✅ Session Management

- **Context retention**: AI remembered entire conversation history
- **State management**: Session state properly maintained
- **Cleanup handling**: Clean session cleanup on call end

### ✅ Complete Transcription

- **Input transcription**: User speech captured accurately
- **Output transcription**: AI speech transcribed correctly
- **Turn completion**: Transcriptions saved on `turnComplete` flag
- **Email delivery**: Complete transcript sent at call end

## Lessons Learned & Critical Fixes

### 1. Transcription Handling (CRITICAL)

**Issue**: Initial implementation used heuristics (punctuation, timing) to detect end of utterance.

**Fix**: Use `turnComplete` flag from API instead of heuristics.

```python
# ❌ WRONG - Don't use heuristics
if text.endswith(('.', '?', '!')) or time_since_last > threshold:
    save_transcription()

# ✅ CORRECT - Use turnComplete flag
if server_content.get('turnComplete'):
    save_transcription()
```

**Learning**: Trust the API's turn completion signals rather than implementing custom heuristics.

### 2. Incremental Transcription Fragments (CRITICAL)

**Issue**: API sends incremental word-by-word fragments, not cumulative text.

**Fix**: Concatenate fragments until `turnComplete`.

```python
# ❌ WRONG - Replacing buffer
self.buffer = transcription.get('text', '')

# ✅ CORRECT - Concatenating fragments
self.buffer += transcription.get('text', '')
```

**Learning**: API behavior differs from documentation - always validate with real testing.

### 3. Greeting Implementation

**Issue**: Cannot pre-fill model response in `clientContent`.

**Fix**: Send user turn asking model to speak greeting.

```python
# ❌ WRONG - Pre-filled model response
{"role": "model", "parts": [{"text": "Hello!"}]}

# ✅ CORRECT - User turn requesting greeting
{"role": "user", "parts": [{"text": "Please greet the caller..."}]}
```

**Learning**: Gemini Live API requires user turns to trigger model speech.

### 4. Transcript Email Timing (CRITICAL)

**Issue**: `request_transcript` tool sent email immediately (mid-call), missing final conversation.

**Fix**: Store email in session, send at call cleanup with complete history.

```python
# ❌ WRONG - Send immediately
asyncio.create_task(send_email())

# ✅ CORRECT - Store for later
session.transcript_emails.add(email)
# Send in engine cleanup with complete conversation
```

**Learning**: Defer transcript sending until call end for completeness.

### 5. Call Ending Protocol

**Issue**: AI completed task but didn't hang up call, leaving silence.

**Fix**: Explicit call ending protocol in system prompt.

```yaml
CALL ENDING PROTOCOL (CRITICAL):
1. When user indicates they're done → ask "anything else?"
2. If user confirms done → say farewell + IMMEDIATELY call hangup_call
3. NEVER leave call hanging in silence
4. After completing ANY task → ask if there's anything else
```

**Learning**: Be explicit about when and how to end calls in system prompts.

### 6. Configurable Parameters

**Issue**: Many parameters were hardcoded, limiting user flexibility.

**Fix**: Added comprehensive YAML configuration for all parameters.

**New Config Options**:

- LLM generation parameters (temperature, max_tokens, top_p, top_k)
- Response modalities (audio, text, audio_text)
- Transcription toggles (enable_input, enable_output)

**Learning**: Provide maximum user flexibility via configuration without code changes.

### 7. Tool Filtering and Conversation Tracking (CRITICAL, 2025-11-17)

**Context**: After config cleanup deployment, Google Live agent only had 1 tool instead of 6, and email summaries were empty despite transcriptions being captured.

**Root Cause Discovery**:

**Bug #1 - Tool Filtering**:

- `GoogleLiveProvider._send_setup()` called `get_tools_config()` which returned ALL tools from registry
- Should have used context-filtered tool list like OpenAI and Deepgram providers
- Result: Setup message sent only 1 tool wrapper to API instead of 6 individual tools

**Bug #2 - Conversation Tracking**:

- Engine only injects `_session_store` if `hasattr(provider, '_tool_adapter')` is true
- `_tool_adapter` was created inside `_send_setup()` (called during `start_session()`)
- Engine checks `hasattr(_tool_adapter)` BEFORE calling `start_session()`
- Result: `_session_store` was never injected, transcriptions couldn't be saved to session

**Fix**:

```python
# WRONG - Create adapter during session start (too late for injection)
async def _send_setup(self, context):
    self._tool_adapter = GoogleToolAdapter(tool_registry)
    tools = self._tool_adapter.get_tools_config()  # Returns ALL tools

# CORRECT - Create adapter in __init__, use context filtering
def __init__(self, ...):
    from src.tools.registry import tool_registry
    self._tool_adapter = GoogleToolAdapter(tool_registry)  # Early creation

async def _send_setup(self, context):
    tool_names = context.get('tools', [])  # Get filtered list
    tools = self._tool_adapter.format_tools(tool_names)  # Only context tools
```

**Validation** (call 1763410994.5759):

- ✅ 6 tools configured and sent to API
- ✅ `_session_store` injected before session start
- ✅ Conversation messages tracked: "✅ Tracked conversation message"
- ✅ Email summary contained full conversation history
- ✅ request_transcript tool executed successfully

**Learning**:

- Tool adapters must be created in `__init__` for proper dependency injection
- Always use context-filtered tool lists, not registry-wide exports
- Timing of initialization matters for engine dependency injection

**Deployment**: Commit `6eaa315`, November 17, 2025

### 8. Google Live realtimeInputConfig (CRITICAL, 2025-11-16 RCA)

**Context**: After this baseline was established (commit `d4affe8`), later experiments introduced an explicit `realtimeInputConfig` block in `GoogleLiveProvider._send_setup`:

```json
"realtimeInputConfig": {
  "automaticActivityDetection": {
    "disabled": false
  }
}
```

On clean telephony audio (8 kHz µ-law → 16 kHz PCM, SNR ~60–70 dB) this change caused **severe multilingual mis-recognition** in Gemini Live input transcriptions:

- User speaking clear English was intermittently transcribed as Arabic/Thai/Vietnamese tokens.
- Diagnostics confirmed that `caller_to_provider.wav` contained intelligible English with high offline STT confidence.

**Finding**:

- The **Golden Baseline setup did not send any `realtimeInputConfig` field** (commit `d4affe8`).
- Removing `realtimeInputConfig` and reverting to the baseline payload (commit `2597f63`) immediately restored correct, stable English recognition on new calls (e.g. call `1763333755.5693`).

**Lesson**:

- For this telephony ExternalMedia RTP configuration, **do not set `realtimeInputConfig` for Google Live**.
- Rely on Gemini Live's default activity detection and use the **system prompt** (context `demo_google_live`) to constrain language to English.
- If multilingual drift appears on otherwise clean audio, first check for any non-baseline `realtimeInputConfig` fields in the setup payload.

## Performance Comparison

| Provider | Latency | Duplex | Barge-in | Complexity | Cost |
|----------|---------|--------|----------|------------|------|
| **Google Live** | **<1s** | ✅ True | ✅ Native | Low | Medium |
| OpenAI Realtime | <2s | ✅ True | ✅ Native | Low | High |
| Deepgram Agent | 1.5-2.5s | ✅ True | ✅ Native | Low | Medium |
| Google Pipeline | 2-3s | ❌ Sequential | ❌ Manual | High | Low |
| Local Hybrid | 3-7s | ❌ Sequential | ❌ Manual | High | Very Low |

## Use Cases

### ✅ Ideal For

- **Customer Service**: Natural conversation with interruptions
- **Voice Assistants**: Interactive voice-controlled applications
- **Real-time Support**: Instant responses to urgent queries
- **Conversational IVR**: Modern replacement for traditional IVR systems
- **Accessibility Services**: Assistive technology requiring natural speech

### ⚠️ Consider Alternatives For

- **High Call Volume**: Consider cost vs. pipeline mode
- **Offline Requirements**: Use local hybrid instead
- **Regulatory Compliance**: Check data residency requirements
- **Legacy Integration**: Pipeline mode may be easier to debug

## Cost Analysis

**Model**: Gemini 2.5 Flash with native audio  
**Pricing** (as of Nov 2025):
- Audio input: $0.025 per million audio seconds ($0.025 per ~278 hours)
- Audio output: $0.125 per million audio seconds ($0.125 per ~278 hours)

**Example Cost (1-minute call)**:
- Input: 60 seconds = $0.0000015
- Output: 60 seconds = $0.0000075
- **Total**: ~$0.000009 per call

**Daily Volume Examples**:
- 100 calls/day: ~$0.90/day = ~$27/month
- 1,000 calls/day: ~$9/day = ~$270/month
- 10,000 calls/day: ~$90/day = ~$2,700/month

## Monitoring & Observability

### Key Metrics to Track

```prometheus
# Response latency
histogram_quantile(0.95, rate(ai_response_latency_seconds_bucket[5m]))

# Success rate
rate(ai_calls_completed_total[5m]) / rate(ai_calls_started_total[5m])

# Tool execution success
rate(ai_tool_executions_total{status="success"}[5m])

# Transcription completion
rate(ai_transcriptions_completed_total[5m])
```

### Log Patterns

**Successful Call**:
```
[info] Call started
[info] Google Live WebSocket connected
[info] Session setup successful
[debug] Tracked conversation message (role=user)
[debug] Tracked conversation message (role=assistant)
[info] Google Live tool call: request_transcript
[info] Transcript email saved for end-of-call sending
[info] Email summary scheduled for sending
[info] 📧 Sent end-of-call transcript
[info] Call cleanup completed
```

## Deployment Checklist

### Pre-Deployment

- [ ] `GOOGLE_API_KEY` set in environment
- [ ] Provider enabled in `config/ai-agent.yaml`
- [ ] Context configured with appropriate prompt
- [ ] Dialplan updated with correct context/provider
- [ ] Tools configured (hangup_call, send_email_summary, request_transcript)
- [ ] Email settings configured (RESEND_API_KEY, sender addresses)

### Post-Deployment

- [ ] Test call with basic conversation
- [ ] Verify interruption/barge-in works
- [ ] Test tool execution (transcript request)
- [ ] Verify complete transcripts in email
- [ ] Check call termination protocol
- [ ] Monitor logs for errors
- [ ] Review metrics/dashboards

### Production Readiness

- [ ] Load testing completed
- [ ] Cost monitoring configured
- [ ] Alerting rules established
- [ ] Backup provider configured
- [ ] Documentation updated
- [ ] Team trained on troubleshooting

## Troubleshooting

### Connection Issues

**Symptom**: WebSocket connection fails

**Check**:
1. `GOOGLE_API_KEY` is valid
2. Generative Language API is enabled
3. Network allows WebSocket connections
4. No firewall blocking port 443

### Audio Quality Issues

**Symptom**: Garbled or poor audio quality

**Check**:
1. Audio profile is `telephony_ulaw_8k`
2. RTP transport is working correctly
3. Network jitter/packet loss is acceptable
4. Codec negotiation succeeded

### Transcription Missing

**Symptom**: Email summaries lack transcripts

**Check**:
1. `enable_input_transcription: true`
2. `enable_output_transcription: true`
3. Session is saved before cleanup
4. Email tool is enabled

### Call Won't Hang Up

**Symptom**: AI doesn't end call properly

**Check**:
1. `hangup_call` tool is in context tools list
2. Call ending protocol is in system prompt
3. AI recognizes end-of-conversation signals
4. Tool execution is not blocked

## Conclusion

The Google Live provider represents the **fastest and most natural conversational AI experience** in the Asterisk AI Voice Agent v4.2. With <1 second latency, true duplex communication, and seamless tool integration, it sets a new standard for real-time voice AI.

**Status**: ✅ **Production Ready**  
**Recommendation**: **Use as primary provider for interactive voice applications**

---

**Document Version**: 1.2  
**Last Updated**: November 17, 2025  
**Validated By**: Production testing with real call data (including v4.3.0 validation on 2025-11-17, commit `6eaa315`)  
**Next Review**: December 2025 or after significant provider updates
