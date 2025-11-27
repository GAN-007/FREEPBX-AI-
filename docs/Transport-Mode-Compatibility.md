# Transport & Playback Mode Compatibility Guide

**Last Updated**: November 19, 2025  
**Issue**: Linear AAVA-28, AAVA-85

## Overview

This document defines the **validated and supported** combinations of audio transport, provider mode, and playback methods.

---

## Validated Configurations

### ✅ Configuration 1: ExternalMedia RTP + Hybrid Pipelines + File Playback

**Use Case**: Modular STT → LLM → TTS pipelines

**Configuration**:
```yaml
audio_transport: externalmedia
active_pipeline: hybrid_support  # or any pipeline
downstream_mode: stream  # ignored by pipelines
```

**Technical Details**:
- **Transport**: ExternalMedia RTP (direct UDP audio stream)
- **Provider Mode**: Pipeline (modular adapters)
- **Playback Method**: File-based (PlaybackManager)
- **Audio Flow**:
  - Caller audio → RTP Server → ai-engine → Pipeline STT
  - TTS bytes → File → Asterisk Announcer channel → Caller
  - **No bridge conflict**: RTP ingestion separate from file playback

**Status**: ✅ **VALIDATED** (Call 1761698845.2619)
- Clean two-way conversation
- Proper gating (no feedback loop)
- No audio routing issues

**Why This Works**:
- RTP audio ingestion doesn't use Asterisk bridge
- File playback uses Announcer channel in bridge
- No routing conflict between ingestion and playback

---

### ✅ Configuration 2: AudioSocket + Full Agent + Streaming Playback

**Use Case**: Monolithic providers with integrated STT/LLM/TTS (Deepgram Voice Agent, OpenAI Realtime)

**Configuration**:
```yaml
audio_transport: audiosocket
active_pipeline: ""  # Disable pipelines
default_provider: deepgram  # or openai_realtime
downstream_mode: stream
```

**Technical Details**:
- **Transport**: AudioSocket (Asterisk channel in bridge)
- **Provider Mode**: Full Agent (monolithic)
- **Playback Method**: Streaming (StreamingPlaybackManager)
- **Audio Flow**:
  - Caller audio → AudioSocket channel → ai-engine → Provider
  - Provider TTS stream → StreamingPlaybackManager → AudioSocket → Caller
  - **No Announcer**: Streaming playback doesn't create extra channels

**Status**: ✅ **VALIDATED**
- Clean audio routing
- No bridge conflicts
- Real-time streaming

**Why This Works**:
- AudioSocket channel in bridge for bidirectional audio
- StreamingPlaybackManager sends audio directly to AudioSocket
- No Announcer channel needed

---

### ✅ Configuration 3: AudioSocket + Pipelines + File Playback

> **Note**: AudioSocket + pipelines was previously unstable due to audio routing issues; validated as stable as of **v4.0 (November 2025)** after fixes in commits `fbbe5b9`, `181b210`, and `fbaaf2e`.

**Use Case**: Modular STT → LLM → TTS pipelines with AudioSocket transport

**Configuration**:
```yaml
audio_transport: audiosocket
active_pipeline: local_hybrid  # or any pipeline
downstream_mode: stream  # Ignored by pipelines
```

**Technical Details**:
- **Transport**: AudioSocket (Asterisk channel in bridge)
- **Provider Mode**: Pipeline (modular adapters)
- **Playback Method**: File-based (PlaybackManager)
- **Audio Flow**:
  - Caller audio → AudioSocket channel → ai-engine → Pipeline STT
  - TTS bytes → File → Asterisk Announcer channel → Caller
  - **Bridge coexistence**: Both AudioSocket and Announcer channels work together

**Status**: ✅ **VALIDATED** (Call 1763610866.6294, November 19, 2025)
- Clean two-way conversation
- Continuous audio frame flow (277 frames, 54.57s)
- Multiple playback cycles (greeting + responses + farewell)
- Tool execution functional (hangup with farewell)

**Why This Now Works**:
1. **Pipeline audio routing fix** (commit `fbbe5b9`, Oct 27):
   - Pipeline mode check added BEFORE continuous_input provider routing
   - Audio correctly routed to pipeline queues
2. **Pipeline gating enforcement** (commit `181b210`, Oct 28):
   - Gating checks added to prevent feedback loop
   - Agent doesn't hear own TTS playback
3. **AudioSocket stability improvements**:
   - Single-frame issue resolved
   - Asterisk now continuously sends frames to AudioSocket even with Announcer present

**Historical Context** (archived):
- **Pre-October 2025**: AudioSocket + Pipeline was unstable
- **Issue**: Bridge routing conflict, single-frame reception
- **Evidence**: Call 1761699424.2631 (only 1 frame received)
- **Resolution**: Series of fixes in October 2025
- **Current Status**: Fully functional and production-ready

---

## Configuration Matrix

| Transport | Provider Mode | Playback Method | Gating | Status |
|-----------|--------------|-----------------|--------|--------|
| **ExternalMedia RTP** | Pipeline | File (PlaybackManager) | ✅ Working | ✅ **VALIDATED** |
| **AudioSocket** | Full Agent | Streaming (StreamingPlaybackManager) | ✅ Working | ✅ **VALIDATED** |
| **AudioSocket** | Pipeline | File (PlaybackManager) | ✅ Working | ✅ **VALIDATED** (v4.0+) |

---

## Decision Guide

### Use ExternalMedia RTP When:
- ✅ Running hybrid pipelines (modular STT/LLM/TTS)
- ✅ Need file-based playback
- ✅ Want clean audio routing (no bridge conflicts)
- ✅ Modern deployment

### Use AudioSocket When:
- ✅ Running full agent providers (Deepgram Voice Agent, OpenAI Realtime)
- ✅ Running pipelines (validated as of v4.0)
- ✅ Need streaming playback (full agents) or file playback (pipelines)
- ✅ Legacy compatibility requirements

---

## Configuration Examples

### Example 1: Production Pipeline (Recommended)

```yaml
# config/ai-agent.yaml
audio_transport: externalmedia
active_pipeline: hybrid_support
downstream_mode: stream  # Ignored by pipelines

pipelines:
  hybrid_support:
    stt: deepgram_stt
    llm: openai_llm
    tts: deepgram_tts
    options:
      stt:
        streaming: true
        encoding: linear16
        sample_rate: 16000
```

**Result**: Clean two-way conversation with proper gating ✅

---

### Example 2: Full Agent (Streaming)

```yaml
# config/ai-agent.yaml
audio_transport: audiosocket
active_pipeline: ""  # Disable pipelines
default_provider: deepgram
downstream_mode: stream

providers:
  deepgram:
    enabled: true
    continuous_input: true
    # ... provider config
```

**Result**: Real-time streaming conversation ✅

---

## Troubleshooting

### Symptom: Only hear greeting, nothing after

**Possible Causes**:
1. Using pre-v4.0 version with AudioSocket + Pipeline
2. Audio gating misconfiguration
3. Pipeline STT not receiving audio

**Solutions**:
1. Upgrade to v4.0 or later (includes AudioSocket + Pipeline fixes)
2. Check gating logs for feedback loop
3. Verify pipeline audio routing in logs
4. If issues persist, use `audio_transport: externalmedia` as fallback

### Symptom: No audio frames after initial connection

**Check**:
1. Verify transport mode in logs
2. Check for Announcer channel in bridge
3. Confirm downstream_mode being honored

**Fix**: Use validated configuration from this document

---

## Implementation Notes

### Why Pipelines Always Use File Playback

**Code Location**: `src/engine.py:4242`

```python
# Pipeline runner hardcoded to file playback
playback_id = await self.playback_manager.play_audio(
    call_id,
    bytes(tts_bytes),
    "pipeline-tts",
)
```

**Reason**: Pipelines were designed for discrete request/response cycles with file artifacts.

**Future**: Could add `downstream_mode` check to enable streaming for pipelines (4-6 hour effort).

### Why Full Agents Respect downstream_mode

**Code Location**: `src/engine.py:3598, 3669`

```python
# Full agents check downstream_mode
use_streaming = self.config.downstream_mode != "file"

if use_streaming:
    await self.streaming_playback_manager.start_streaming_playback(...)
else:
    await self.playback_manager.play_audio(...)
```

**Reason**: Full agents were designed for continuous streaming with optional file fallback.

---

## Related Issues

- **Linear AAVA-28**: Pipeline STT streaming implementation & gating fixes
- **Commits**:
  - `181b210`: Pipeline gating enforcement
  - `fbaaf2e`: Fallback safety margin increase
  - `294e55e`: Deepgram STT streaming support

---

## Validation History

| Date | Transport | Mode | Result | Call ID | Notes |
|------|-----------|------|--------|---------|-------|
| 2025-10-28 | RTP | Pipeline | ✅ Pass | 1761698845.2619 | Clean two-way, no feedback |
| 2025-10-28 | AudioSocket | Pipeline | ❌ Fail | 1761699424.2631 | Pre-fix: Only greeting heard |
| 2025-10-28 | AudioSocket | Full Agent | ✅ Pass | Multiple | Streaming playback |
| 2025-11-19 | AudioSocket | Full Agent (Google Live) | ✅ Pass | 1763610697.6282 | 186 frames, 36.34s, clean conversation |
| 2025-11-19 | AudioSocket | Full Agent (Deepgram) | ✅ Pass | 1763610742.6286 | 176 frames, 34.36s, clean conversation |
| 2025-11-19 | AudioSocket | Full Agent (OpenAI) | ✅ Pass | 1763610785.6290 | 360 frames, 71.29s, tool execution |
| 2025-11-19 | AudioSocket | Pipeline (local_hybrid) | ✅ Pass | 1763610866.6294 | 277 frames, 54.57s, post-fix validation |

---

## Recommendations

1. **Production (v4.0+)**: Both **ExternalMedia RTP** and **AudioSocket** are validated for pipeline deployments
2. **Transport Selection**:
   - **AudioSocket**: Simpler configuration, single transport mechanism for all modes
   - **ExternalMedia RTP**: Separate ingestion path, proven in production longer
3. **Pre-v4.0 Systems**: Use **ExternalMedia RTP** for pipelines (AudioSocket + Pipeline had known issues)
4. **Monitoring**: Always check transport logs during deployment validation
5. **Upgrades**: When upgrading from pre-v4.0, AudioSocket + Pipeline becomes a supported option

---

**For questions or issues, see**:
- [Architecture.md](./Architecture.md)
- [ROADMAP.md](./ROADMAP.md)
- Linear issue AAVA-28
