# Google Provider Setup Guide

## Overview

Google AI integration provides two modes for the Asterisk AI Voice Agent:

1. **Google Live (Recommended)** - Real-time bidirectional streaming with Gemini 2.5 Flash, native audio processing, ultra-low latency (<1s), and true duplex communication
2. **Google Cloud Pipeline** - Traditional pipeline with separate STT (Chirp 3), LLM (Gemini), and TTS (Neural2) components

This guide covers setup for both modes.

## Quick Start

### 1. Enable Google Cloud APIs

In your Google Cloud Console, enable these APIs:

1. **Cloud Speech-to-Text API**: https://console.cloud.google.com/apis/library/speech.googleapis.com
2. **Cloud Text-to-Speech API**: https://console.cloud.google.com/apis/library/texttospeech.googleapis.com
3. **Generative Language API**: https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com

### 2. Configure API Key

Add your Google API key to `.env`:

```bash
# Google Cloud AI (required for google_cloud_* pipelines)
GOOGLE_API_KEY=your_api_key_here
```

**OR** use a service account (recommended for production):

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
```

### 3. Configure Asterisk Dialplan

**For Google Live (Recommended):**

```ini
[from-ai-agent]
exten => s,1,NoOp(AI Voice Agent - Google Live)
exten => s,n,Set(AI_CONTEXT=demo_google_live)
exten => s,n,Set(AI_PROVIDER=google_live)
exten => s,n,Stasis(asterisk-ai-voice-agent)
exten => s,n,Hangup()
```

**For Google Cloud Pipeline:**

```ini
[from-ai-agent]
exten => s,1,NoOp(AI Voice Agent - Google Cloud Pipeline)
exten => s,n,Set(AI_CONTEXT=demo_google)
exten => s,n,Set(AI_PROVIDER=google_cloud_full)
exten => s,n,Stasis(asterisk-ai-voice-agent)
exten => s,n,Hangup()
```

**CRITICAL**: Both `AI_CONTEXT` and `AI_PROVIDER` must be set:
- `AI_CONTEXT` - Selects the context (greeting, prompt, profile)
- `AI_PROVIDER` - Selects the provider (google_live, google_cloud_full, etc.)

### 4. Restart Asterisk

```bash
asterisk -rx "dialplan reload"
```

## Available Pipelines

### google_cloud_full (Recommended)
**Best quality and features**

- **STT**: Google Chirp 3 (latest_long model, 16kHz)
- **LLM**: Gemini 2.5 Flash (latest stable, fast, intelligent)
- **TTS**: Neural2-A (natural female voice)
- **Cost**: ~$0.0024/min
- **Use Cases**: Customer service, demos, quality-focused deployments

**Dialplan:**
```ini
exten => s,n,Set(AI_CONTEXT=demo_google)
exten => s,n,Set(AI_PROVIDER=google_cloud_full)
```

---

### google_cloud_cost_optimized
**Budget-friendly option**

- **STT**: Google Standard model (8kHz telephony)
- **LLM**: Gemini 2.5 Flash
- **TTS**: Standard-C voice
- **Cost**: ~$0.0015/min (38% lower than full)
- **Use Cases**: High-volume, cost-sensitive deployments

**Dialplan:**
```ini
exten => s,n,Set(AI_CONTEXT=demo_google_cost)
exten => s,n,Set(AI_PROVIDER=google_cloud_cost_optimized)
```

---

### google_hybrid_openai
**Best LLM quality**

- **STT**: Google Chirp 3
- **LLM**: OpenAI GPT-4o-mini (superior reasoning)
- **TTS**: Google Neural2-A
- **Cost**: ~$0.003/min
- **Use Cases**: Complex conversations, reasoning tasks

**Dialplan:**
```ini
exten => s,n,Set(AI_CONTEXT=demo_google_hybrid)
exten => s,n,Set(AI_PROVIDER=google_hybrid_openai)
```

**Note**: Requires both `GOOGLE_API_KEY` and `OPENAI_API_KEY`.

## Cost Comparison

| Pipeline | STT | LLM | TTS | Est. Cost/min |
|----------|-----|-----|-----|---------------|
| google_cloud_full | Chirp 3 | Gemini 2.5 | Neural2 | $0.0024 |
| google_cloud_cost_optimized | Standard | Gemini 2.5 | Standard | $0.0015 |
| google_hybrid_openai | Chirp 3 | GPT-4o-mini | Neural2 | $0.003 |
| deepgram (reference) | Nova-2 | GPT-4o-mini | Aura | $0.0043 |

*Estimates based on typical 3-minute call with 60% talk time*

## Troubleshooting

### Issue: Greeting plays but no responses

**Cause**: Google Cloud APIs not enabled or API key lacks permissions.

**Solution**: 
1. Enable all three APIs in Google Cloud Console (see Quick Start)
2. Verify API key has permissions for Speech-to-Text, Text-to-Speech, and Generative Language
3. Restart ai-engine container

### Issue: Falls back to local_hybrid pipeline

**Cause**: Missing `AI_PROVIDER` channel variable in dialplan.

**Solution**: Add both variables to dialplan:
```ini
exten => s,n,Set(AI_CONTEXT=demo_google)
exten => s,n,Set(AI_PROVIDER=google_cloud_full)
```

### Issue: "Pipeline not found" error

**Cause**: Typo in pipeline name or pipelines not validating on startup.

**Solution**: 
1. Check `docker logs ai_engine` for pipeline validation results
2. Verify spelling: `google_cloud_full`, `google_cloud_cost_optimized`, `google_hybrid_openai`
3. Ensure all three should show "Pipeline validation SUCCESS"

### Verify Configuration

Check engine logs:
```bash
docker logs ai_engine 2>&1 | grep -E "google|Pipeline validation|Engine started"
```

Expected output:
```
Pipeline validation SUCCESS ... pipeline=google_cloud_full
Pipeline validation SUCCESS ... pipeline=google_cloud_cost_optimized
Pipeline validation SUCCESS ... pipeline=google_hybrid_openai
Engine started and listening for calls
```

## Advanced Configuration

### Custom Voices

Edit `config/ai-agent.yaml` to change TTS voices:

```yaml
pipelines:
  google_cloud_full:
    options:
      tts:
        voice_name: "en-US-Neural2-C"  # Male voice
        # Other options: Neural2-A (female), Neural2-D (male), Neural2-F (female)
```

### Multi-Language Support

Change STT language in pipeline options:

```yaml
pipelines:
  google_cloud_full:
    options:
      stt:
        language_code: "es-ES"  # Spanish
        # Chirp 3 supports 100+ languages
```

### Adjust Response Speed

Modify speaking rate:

```yaml
pipelines:
  google_cloud_full:
    options:
      tts:
        speaking_rate: 1.1  # 10% faster (range: 0.25 to 4.0)
```

## IAM Roles Required

If using service account authentication, grant these roles:

- **Cloud Speech-to-Text User** (`roles/speech.user`)
- **Cloud Text-to-Speech User** (`roles/texttospeech.user`)
- **Generative AI User** (`roles/aiplatform.user`)
- **Gemini Live API User** (`roles/generativelanguage.liveapi.user`) - for Google Live

---

# Google Gemini Live API (Real-Time Agent)

## Overview

**Google Live** (`AI_PROVIDER=google_live`) is a **real-time bidirectional streaming** voice agent similar to OpenAI Realtime. It provides:

- ✅ **Native audio processing** (no separate STT/TTS)
- ✅ **Built-in Voice Activity Detection (VAD)**
- ✅ **True barge-in support** (interrupt naturally)
- ✅ **Ultra-low latency** (<1s)
- ✅ **Function calling in streaming mode**
- ✅ **Session management for context**

### When to Use Google Live vs Pipeline Mode

| Feature | Google Live | Pipeline Mode |
|---------|------------|---------------|
| **Architecture** | Native audio end-to-end | Sequential STT→LLM→TTS |
| **Latency** | <1s | 1.5-2.5s |
| **Barge-in** | ✅ Yes (automatic) | ❌ No |
| **Turn-taking** | Duplex (simultaneous) | Sequential (wait for TTS) |
| **Best for** | Conversations, support | Debugging, batch processing |

## Setup

### 1. Enable Gemini Live API

Enable the **Gemini Live API** in Google Cloud Console:
https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com

### 2. Configure API Key

Use the same `GOOGLE_API_KEY` from pipeline setup:

```bash
GOOGLE_API_KEY=your_api_key_here
```

### 3. Update Dialplan

```asterisk
[from-ai-agent]
exten => s,1,NoOp(Google Live API Demo)
same => n,Set(AI_CONTEXT=demo_google_live)
same => n,Set(AI_PROVIDER=google_live)  ; Use real-time agent
same => n,Stasis(asterisk-ai-voice-agent)
same => n,Hangup()
```

### 4. Test the Agent

Call your extension and:
- **Say something** → Agent responds in real-time
- **Interrupt the agent mid-sentence** → It stops and listens
- **Have a natural conversation** → Context is maintained

## Architecture

### Audio Flow

```
Asterisk (8kHz µ-law) 
    ↓ Transcode to 16kHz PCM
    ↓ Stream to Gemini Live API (WebSocket)
    ↓ Receive 24kHz PCM audio
    ↓ Resample to 8kHz µ-law
    ↓ Back to Asterisk
```

### Key Components

1. **GoogleLiveProvider** (`src/providers/google_live.py`)
   - WebSocket management
   - Audio transcoding (µ-law ↔ PCM, 8kHz ↔ 16kHz/24kHz)
   - Session setup and lifecycle

2. **GoogleToolAdapter** (`src/tools/adapters/google.py`)
   - Function calling support
   - Tool execution integration

3. **Audio Resampling**
   - Input: 8kHz µ-law → 16kHz PCM (Gemini input)
   - Output: 24kHz PCM → 8kHz µ-law (Asterisk output)

## Configuration

Google Live uses the Google provider config:

```yaml
google:
  api_key: ${GOOGLE_API_KEY}
  llm_model: "gemini-2.5-flash"  # Model with Live API support
  tts_voice_name: "en-US-Neural2-A"
  initial_greeting: "Hi! I'm powered by Google Gemini Live API."
```

## Features

### 1. Barge-In (Interruption)

**Automatic** - No configuration needed:
- User speaks → Gemini detects via built-in VAD
- Agent stops talking immediately
- User's speech is processed

### 2. Function Calling

Tools work seamlessly in streaming mode:

```python
# Example: Transfer tool
"What's your name?" → User responds
Agent calls transfer_tool(extension="6000")
"Transferring you now..."
```

### 3. Session Management

Conversation context is maintained automatically:
- History tracked per WebSocket session
- Context carries across turns
- No need to re-introduce agent

## Limitations (AAVA-75 Findings)

### Google Pipeline Mode Limitations

| Issue | Impact | Workaround |
|-------|--------|-----------|
| **No barge-in** | Must wait for TTS to finish | Use `google_live` instead |
| **Sequential turns** | Higher latency (1.5-2.5s) | Use `google_live` for <1s |
| **System prompt repetition** | Fixed in v4.0 | Update to latest version |
| **Thinking tokens** | Need 256+ max_output_tokens | Config already updated |

### Google Live API Considerations

| Consideration | Details |
|---------------|---------|
| **Audio format** | Requires 16kHz PCM input (resampling needed for 8kHz) |
| **WebSocket** | Persistent connection (manage reconnection) |
| **API availability** | Preview/beta (check quota limits) |
| **Latency** | Network-dependent (test in your environment) |

## Cost Comparison

| Mode | Cost per Minute | Components |
|------|----------------|------------|
| **Pipeline** | ~$0.0024 | STT + LLM + TTS separate |
| **Live API** | ~$0.003* | All-in-one native audio |

*Estimated based on preview pricing

## Troubleshooting

### Issue: WebSocket Connection Fails

**Symptoms**: `Failed to start Google Live session`

**Solutions**:
1. Verify API key: `echo $GOOGLE_API_KEY`
2. Check API enabled: https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com
3. Check quota limits in Cloud Console

### Issue: No Audio Received

**Symptoms**: Agent doesn't respond, silence

**Solutions**:
1. Check logs for `Google Live audio output` messages
2. Verify audio transcoding: `grep "resample" logs/ai-engine.log`
3. Test with pipeline mode first to isolate issue

### Issue: Google Live mis-hears English as other languages (CRITICAL)

**Context**:

- Golden Baseline commit `d4affe8` used the default Gemini Live setup payload (no `realtimeInputConfig`).
- A later change added an explicit `realtimeInputConfig.automaticActivityDetection.disabled=false` block to the Google Live setup message.

**Symptoms**:

- Caller speaks clear English over telephony (8 kHz µ-law) but input transcriptions from Google Live show Arabic/Thai/Vietnamese tokens.
- RCA captures (`caller_to_provider.wav`) show clean English audio with high offline STT confidence.

**Root Cause & Fix**:

- For the ExternalMedia RTP telephony profile, explicitly sending `realtimeInputConfig` caused Gemini Live to mis-classify language on otherwise clean audio.
- Removing `realtimeInputConfig` and reverting to the Golden Baseline setup (commit `2597f63`) restores stable English recognition.

**Guidance**:

- **Do NOT set `realtimeInputConfig` for `google_live`** in this configuration.
- Rely on Gemini Live's default activity detection and constrain language via the system prompt in `demo_google_live`.
- If multilingual drift appears again, compare the setup payload against commit `d4affe8` and ensure `realtimeInputConfig` has not been reintroduced.

### Issue: Barge-In Not Working

**Note**: Barge-in is **automatic** with Google Live. If not working:
1. Confirm using `AI_PROVIDER=google_live` (not pipeline)
2. Check VAD is active: `grep "VAD" logs/ai-engine.log`
3. Verify WebSocket messages: `grep "inputTranscription" logs/ai-engine.log`

## Migration from Pipeline to Live

### Before (Pipeline Mode)
```asterisk
Set(AI_CONTEXT=demo_google)
; AI_PROVIDER defaults to google_cloud_full
Stasis(asterisk-ai-voice-agent)
```

### After (Live API Mode)
```asterisk
Set(AI_CONTEXT=demo_google_live)
Set(AI_PROVIDER=google_live)  ; Enable real-time agent
Stasis(asterisk-ai-voice-agent)
```

## Support

- **Issues**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/issues
- **Docs**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/tree/main/docs
- **Linear**: Task AAVA-75 (Google Cloud Integration - CLOSED with findings)
