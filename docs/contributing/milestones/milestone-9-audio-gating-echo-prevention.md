# Milestone 9 — Audio Gating & Echo Prevention

## Objective

Enable production-ready OpenAI Realtime provider with echo prevention and natural conversation flow. Implement provider-specific audio gating with VAD-based interrupt detection to prevent the agent from hearing its own TTS output and self-interrupting.

## Success Criteria

- Clean audio with no self-interruption during agent responses.
- OpenAI's server-side VAD handles turn-taking naturally without local interference.
- Audio gate stays open properly when agent speaking (correct behavior).
- VAD configuration `webrtc_aggressiveness: 1` prevents false echo detection.
- Zero buffered chunks during normal operation (gate not fluttering).
- SNR ≥ 64 dB with natural conversation flow.

## Dependencies

- Milestone 8 complete (transport stabilization, golden baseline established).
- OpenAI Realtime provider functional (Milestone 6).
- AudioSocket transport working with clean audio.

## Work Breakdown

### 9.1 Audio Gating Manager

**Objective**: Centralize gating/barge-in decisions with provider-specific behavior.

**Implementation** (`src/core/audio_gating_manager.py`):
- Create `AudioGatingManager` class with provider-specific gating strategies.
- Support opt-in per provider (some providers need gating, others don't).
- VAD-based interrupt detection with configurable thresholds.
- Integration with `ConversationCoordinator` for state management.

**Key Features**:
- Provider-specific gating enable/disable
- VAD energy threshold and confidence checking
- Interrupt detection with cooldown periods
- Metrics for gating events (open/close/interrupt)

### 9.2 VAD Configuration Tuning

**Problem**: `webrtc_aggressiveness: 0` (very sensitive) was detecting echo as "speech" with 0.4 confidence, causing gate to flutter open/closed 50+ times per call.

**Root Cause**:
- OpenAI Realtime has sophisticated server-side echo cancellation
- Local VAD fighting it caused problems
- Echo leaked through gaps in fluttering gate → OpenAI detected own audio → self-interruption loop

**Solution**:
- Set `webrtc_aggressiveness: 1` (balanced mode)
- Level 1 does NOT detect echo as speech
- Gate stays open correctly during agent speech
- OpenAI's server-side echo cancellation works properly

**Configuration**:
```yaml
vad:
  webrtc_aggressiveness: 1  # CRITICAL for OpenAI Realtime
  enhanced_enabled: true
  confidence_threshold: 0.6
  energy_threshold: 1500
```

### 9.3 Provider Integration

**OpenAI Realtime Specific Changes**:
- Enable provider-specific gating for `openai_realtime`
- Configure server-side VAD in `session.audio.input.turn_detection`
- Use 24kHz PCM16 input/output format
- Proper event handling for `response.audio.done` vs `response.done`

**Gating Behavior**:
- Gate closes when TTS playback starts
- Gate opens when playback completes
- No premature reopening during agent speech
- Barge-in detection optional (can rely on server-side VAD)

### 9.4 Metrics and Observability

**New Metrics**:
- `ai_agent_gating_buffered_chunks` - Chunks held during gate closure
- `ai_agent_gating_closures` - Number of gate close events
- `ai_agent_gating_interrupts` - Detected interrupt events
- Per-call gating statistics in session metrics

**Logging**:
- Gate state transitions (open/close)
- VAD confidence scores
- Interrupt detection events
- Buffer depth during gating

## Deliverables

- `AudioGatingManager` class implemented and tested
- OpenAI Realtime golden baseline documented
- VAD configuration tuned and validated
- Metrics for gating events exposed
- Documentation: `OPENAI_REALTIME_GOLDEN_BASELINE.md`

## Verification Checklist

### Pre-Deployment
- [ ] AudioGatingManager integrated with engine
- [ ] VAD configuration set to `webrtc_aggressiveness: 1`
- [ ] Provider-specific gating enabled for OpenAI Realtime
- [ ] Metrics endpoint exposing gating counters

### Test Call (Golden Baseline)
- [ ] Duration ≥ 30 seconds with multiple turns
- [ ] Zero self-interruption events
- [ ] Buffered chunks ≈ 0 (no gate flutter)
- [ ] Gate closures ≤ number of agent turns
- [ ] SNR ≥ 64 dB
- [ ] Natural conversation flow (no awkward pauses)

### Regression Check
- [ ] Compare against Milestone 8 baseline (should be equal or better)
- [ ] User feedback confirms improved behavior
- [ ] Logs show proper gate state transitions
- [ ] No "agent hearing itself" behavior

## Golden Baseline Reference

**Call ID**: 1761449250.2163  
**Date**: October 26, 2025  
**Provider**: OpenAI Realtime  
**Transport**: AudioSocket

**Metrics**:
- Duration: 45.9s
- SNR: 64.7 dB
- Buffered chunks: 0 (vs 50 with aggressiveness: 0)
- Gate closures: 1 (vs 50+ with aggressiveness: 0)
- Self-interruption events: 0
- User feedback: "much better results" ✅

**Configuration Key**:
```yaml
vad:
  webrtc_aggressiveness: 1  # CRITICAL - level 0 causes echo detection
```

## Handover Notes

- This golden baseline establishes the correct VAD configuration for OpenAI Realtime.
- **DO NOT** change `webrtc_aggressiveness` from 1 without extensive testing - level 0 causes self-interruption loops.
- OpenAI's server-side echo cancellation is sophisticated; let it handle turn-taking naturally.
- Gate staying open during agent speech is CORRECT behavior (not a bug).
- Next milestone (Milestone 10) uses this foundation for provider-agnostic operation.

## Related Issues

- **Bug**: Self-interruption with webrtc_aggressiveness: 0 (fixed)
- **Enhancement**: Provider-specific gating (added)
- **Documentation**: OpenAI Realtime golden baseline (created)

## Configuration Evolution

### Initial (Broken) - aggressiveness: 0
```yaml
vad:
  webrtc_aggressiveness: 0  # Too sensitive!
```
**Result**: 50+ gate closures, echo detected as speech, self-interruption loop

### Fixed - aggressiveness: 1
```yaml
vad:
  webrtc_aggressiveness: 1  # Balanced mode
  enhanced_enabled: true
  confidence_threshold: 0.6
  energy_threshold: 1500
```
**Result**: Clean conversation, no self-interruption, gate stays open correctly

## Key Insights

1. **OpenAI Realtime has built-in echo cancellation** - don't fight it with aggressive local VAD
2. **Gate staying open is correct** - agent needs to hear call progress, server handles turn detection
3. **Level 1 is optimal** - level 0 too sensitive (detects echo), level 2+ may miss legitimate barge-ins
4. **Server-side VAD preferred** - local VAD should complement, not compete with provider features

---

**Status**: ✅ Completed October 26, 2025  
**Golden Baseline**: OPENAI_REALTIME_GOLDEN_BASELINE.md
