# Milestone 10 — Transport Orchestrator & Audio Profiles

## Objective

Make the engine provider-agnostic and format-agnostic with declarative audio profiles and automatic capability negotiation. Enable per-call provider and profile selection via Asterisk channel variables without YAML edits.

## Success Criteria

- Switching `AI_AUDIO_PROFILE` channel variable changes transport plan; call remains stable.
- Provider ACK empty → remediation logged; call continues with fallback.
- Multi-provider parity demonstrated (Deepgram + OpenAI Realtime both ≥ 64 dB SNR).
- Dynamic profile switching confirmed working without configuration changes.
- Zero-change upgrade path for existing deployments (backward compatibility).

## Dependencies

- Milestones 8-9 complete (transport stabilization, audio gating, golden baselines established).
- Deepgram and OpenAI Realtime providers operational.
- AudioSocket and ExternalMedia RTP transports working.

## Work Breakdown

### 10.1 TransportOrchestrator Class

**Objective**: Centralize audio routing and format management.

**Implementation** (`src/core/transport_orchestrator.py`):
- Create `TransportOrchestrator` class for dynamic profile resolution
- Parse audio profiles from YAML configuration
- Match provider capabilities with requested profiles
- Generate transport plan for each call
- Handle ACK parsing and capability negotiation

**Key Features**:
- Profile validation at startup
- Runtime profile resolution per call
- Provider capability matching
- Fallback handling for missing ACKs
- Legacy config synthesis for backward compatibility

### 10.2 Audio Profiles System

**Objective**: Declarative YAML-defined profiles with validation.

**Profile Structure**:
```yaml
audio_profiles:
  telephony_ulaw_8k:
    wire:
      encoding: mulaw
      sample_rate: 8000
      endianness: little
    provider_input:
      encoding: mulaw
      sample_rate: 8000
    provider_output:
      encoding: mulaw
      sample_rate: 8000
      
  openai_realtime_24k:
    wire:
      encoding: slin16
      sample_rate: 16000
    provider_input:
      encoding: pcm16
      sample_rate: 24000
    provider_output:
      encoding: pcm16
      sample_rate: 24000
      
  wideband_pcm_16k:
    wire:
      encoding: slin16
      sample_rate: 16000
    provider_input:
      encoding: pcm16
      sample_rate: 16000
    provider_output:
      encoding: pcm16
      sample_rate: 16000
```

**Profile Features**:
- Separate wire (AudioSocket/RTP) and provider (API) formats
- Explicit sample rate declarations
- Encoding specifications (mulaw, alaw, pcm16)
- Endianness configuration
- Per-call profile override capability

### 10.3 Channel Variable Overrides

**Three Override Variables** (all optional, fallback to YAML):

1. **`AI_PROVIDER`**:
   - Which provider to use (deepgram, openai_realtime, local_hybrid)
   - Example: `Set(AI_PROVIDER=openai_realtime)`

2. **`AI_AUDIO_PROFILE`**:
   - Which transport profile (telephony_ulaw_8k, openai_realtime_24k, etc.)
   - Example: `Set(AI_AUDIO_PROFILE=telephony_responsive)`

3. **`AI_CONTEXT`**:
   - Semantic context mapping to YAML `contexts.*`
   - Maps to prompt/greeting/profile overrides
   - Example: `Set(AI_CONTEXT=demo_openai)`

**Asterisk Dialplan Integration**:
```
exten => 1000,1,NoOp(AI Voice Agent Test)
 same => n,Set(AI_PROVIDER=deepgram)
 same => n,Set(AI_AUDIO_PROFILE=telephony_responsive)
 same => n,Set(AI_CONTEXT=demo_deepgram)
 same => n,Stasis(ai-agent,demo)
 same => n,Hangup()
```

### 10.4 Provider Capability Negotiation

**ACK Parsing**:
- Deepgram: `SettingsApplied` event with actual settings used
- OpenAI: `session.updated` event with accepted configuration
- Extract actual formats/rates from provider acknowledgment
- Compare with requested configuration
- Log warnings if mismatch detected

**Fallback Handling**:
- If ACK empty or missing → log remediation advice
- Continue call with requested settings (best effort)
- Metrics track negotiation success/failure per provider

**Validation**:
- Startup validation of profile definitions
- Runtime validation of provider responses
- Format compatibility checking

### 10.5 Legacy Config Synthesis

**Objective**: Zero-change upgrade path for existing deployments.

**Backward Compatibility**:
- Synthesize old-style provider configs from profiles when needed
- Map legacy settings to new profile system transparently
- No breaking changes to existing YAML files
- Deprecation warnings for old-style configs (but still work)

**Migration Path**:
- Phase 1: Both old and new configs work (v4.0)
- Phase 2: Deprecation warnings logged (v4.1)
- Phase 3: Old configs removed (v5.0)

## Deliverables

- `TransportOrchestrator` class implemented and tested
- Audio profiles system in YAML with validation
- Channel variable override support (AI_PROVIDER, AI_AUDIO_PROFILE, AI_CONTEXT)
- Provider capability negotiation with ACK parsing
- Legacy config synthesis for backward compatibility
- Documentation: Architecture.md updated with profile system

## Verification Checklist

### Pre-Deployment
- [ ] TransportOrchestrator integrated with engine
- [ ] Audio profiles defined in YAML
- [ ] Channel variable parsing implemented
- [ ] ACK parsing working for Deepgram and OpenAI
- [ ] Legacy config synthesis functional

### Deepgram Validation Test
- [ ] Call with `telephony_responsive` profile
- [ ] SNR ≥ 64 dB (golden baseline maintained)
- [ ] `SettingsApplied` ACK received and parsed
- [ ] Profile settings applied correctly
- [ ] Clean two-way conversation

### OpenAI Realtime Validation Test
- [ ] Call with `openai_realtime_24k` profile
- [ ] SNR ≥ 64 dB (golden baseline maintained)
- [ ] `session.updated` ACK received and parsed
- [ ] 24kHz PCM16 format confirmed
- [ ] Zero self-interruption (Milestone 9 behavior preserved)

### Dynamic Switching Test
- [ ] Change `AI_AUDIO_PROFILE` via channel variable
- [ ] Profile switch takes effect without YAML edit
- [ ] Call quality maintained across switches
- [ ] Logs show profile resolution

### Backward Compatibility Test
- [ ] Old-style config still works
- [ ] Deprecation warnings logged appropriately
- [ ] No breaking changes to existing deployments

## Golden Baseline References

### Deepgram
**Call ID**: 1761504353.2179  
**Date**: October 26, 2025  
**Profile**: `telephony_responsive`  
**Metrics**: SNR 66.8 dB, clean conversation

### OpenAI Realtime
**Call ID**: 1761505357.2187  
**Date**: October 26, 2025  
**Profile**: `openai_realtime_24k`  
**Metrics**: SNR 64.77 dB, perfect gating, zero self-interruption

## Handover Notes

- This milestone establishes the foundation for provider-agnostic operation.
- Profile system allows easy addition of new providers without engine changes.
- Channel variables enable per-call customization without configuration edits.
- ACK parsing ensures provider and engine are in sync on formats.
- Next milestones (11-13) build on this foundation for diagnostics and tooling.

## Related Issues

- **Feature**: Provider-agnostic architecture (implemented)
- **Feature**: Audio profiles system (implemented)
- **Feature**: Channel variable overrides (implemented)
- **Enhancement**: ACK parsing and validation (implemented)

## Configuration Example

```yaml
# Audio Profiles
audio_profiles:
  telephony_ulaw_8k:
    wire:
      encoding: mulaw
      sample_rate: 8000
    provider_input:
      encoding: mulaw
      sample_rate: 8000
    provider_output:
      encoding: mulaw
      sample_rate: 8000
      
  openai_realtime_24k:
    wire:
      encoding: slin16
      sample_rate: 16000
    provider_input:
      encoding: pcm16
      sample_rate: 24000
    provider_output:
      encoding: pcm16
      sample_rate: 24000

# Contexts (map to profiles and prompts)
contexts:
  demo_deepgram:
    audio_profile: telephony_ulaw_8k
    greeting: "Hello! I'm using Deepgram for speech recognition."
    system_prompt: "You are a helpful assistant using Deepgram."
    
  demo_openai:
    audio_profile: openai_realtime_24k
    greeting: "Hi! I'm powered by OpenAI's Realtime API."
    system_prompt: "You are a helpful assistant using OpenAI Realtime."
```

---

**Status**: ✅ Completed October 26, 2025  
**Key Innovation**: Provider-agnostic architecture with declarative profiles
