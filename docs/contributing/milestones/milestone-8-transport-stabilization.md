# Milestone 8 — Transport Stabilization

## Objective

Eliminate audio garble and pacing issues by enforcing AudioSocket invariants and proper format handling. Establish golden baseline metrics for production quality audio with zero underflows and perfect provider byte alignment.

## Success Criteria

- Golden metrics match baseline within 10% tolerance (SNR ≥ 64 dB, drift ≈ 0%, underflows ≈ 0).
- No garbled greeting; clean two-way conversation end-to-end.
- AudioSocket wire format matches YAML configuration regardless of caller codec.
- TransportCard present in logs at call start showing wire/provider formats.
- No egress byte-swap logic executing (all swapping removed).

## Dependencies

- Milestones 1–7 complete (SessionStore, pipelines, streaming transport).
- Existing Deepgram/OpenAI providers functional.
- AudioSocket transport configured and operational.

## Work Breakdown

### 8.1 AudioSocket Format Override Fix

**Problem**: Caller's codec (e.g., ulaw) was overriding YAML wire format settings, causing format mismatches between AudioSocket and providers.

**Solution**:
- Modify `src/engine.py::_handle_stasis_start()` to ignore caller codec for wire format.
- Enforce little-endian PCM16 on AudioSocket wire as configured in YAML.
- Remove all egress byte-swap logic that was compensating for the bug.
- Add one-shot TransportCard logging at call start showing:
  - Wire format (AudioSocket)
  - Provider input/output formats
  - Sample rates and encodings

**Implementation** (commit `1a049ce`):
- Updated AudioSocket initialization to enforce YAML format.
- Removed `_swap_bytes_if_needed()` calls from egress path.
- Added `_log_transport_card()` method for format visibility.

### 8.2 Pacer Idle Cutoff

**Problem**: Long tails after call completion causing underflows and extended cleanup time.

**Solution**:
- Add pacer idle cutoff (1200ms default) to stop streaming when no provider chunks arrive.
- Prevent infinite playback loops when provider stops sending audio.
- Ensure wall_seconds ≈ content duration (no long tails).

**Implementation**:
- Modified `StreamingPlaybackManager` to track last chunk timestamp.
- Added configurable `pacer_idle_cutoff_ms` to YAML streaming settings.
- Automatic cleanup when idle threshold exceeded.

### 8.3 Chunk Size Auto Mode

**Problem**: Fixed 20ms chunks not optimal for all provider scenarios.

**Solution**:
- Set `chunk_size_ms: auto` as default.
- Reframe provider chunks to match pacer cadence dynamically.
- Maintain 20ms as default when auto-detection used.

### 8.4 Provider Bytes Tracking

**Problem**: Provider byte counters were incorrect, breaking timing calculations.

**Solution**:
- Fix byte counting in provider integrations.
- Ensure received bytes match expected (ratio ≈ 1.0).
- Track in session metrics for RCA analysis.

## Deliverables

- Code changes merged on `develop` with AudioSocket format fix.
- Git tag: `v1.0-p0-transport-stable`
- TransportCard logging implemented.
- Golden baseline metrics documented:
  - SNR: 64.6-68.2 dB
  - Drift: 0%
  - Underflows: 0
  - Provider bytes ratio: 1.0

## Verification Checklist

### Pre-Deployment
- [ ] AudioSocket format matches YAML regardless of caller codec
- [ ] No egress byte-swap code executing
- [ ] TransportCard logs at call start

### Test Call (Golden Baseline)
- [ ] Clean greeting playback (no garble)
- [ ] Two-way conversation sustained
- [ ] Zero underflows reported
- [ ] wall_seconds ≈ content duration (no long tail)
- [ ] SNR ≥ 64 dB
- [ ] Provider bytes ratio ≈ 1.0

### Post-Call Analysis
- [ ] RCA metrics match golden baseline
- [ ] No format mismatch warnings
- [ ] User confirms clean audio quality

## Golden Baseline Reference

**Call ID**: 1761424308.2043  
**Date**: October 25, 2025  
**Provider**: Deepgram Voice Agent  
**Transport**: AudioSocket

**Metrics**:
- Duration: ~23s
- SNR: 64.6-68.2 dB (excellent)
- Underflows: 0
- Provider bytes ratio: 1.0 (perfect)
- Drift: 0%
- User feedback: "Audio pipeline is working really well."

## Handover Notes

- This milestone establishes the golden baseline for all future audio quality comparisons.
- Transport format now enforced correctly; providers must match YAML wire format.
- Next milestone (Milestone 9) builds on this foundation to add echo prevention for OpenAI Realtime.
- All future RCA analysis should compare against these golden metrics.

## Related Issues

- **Bug**: AudioSocket format override (fixed)
- **Bug**: Provider bytes tracking error (fixed)
- **Enhancement**: TransportCard visibility (added)
- **Enhancement**: Pacer idle cutoff (added)

## Configuration Changes

### Before (Broken)
```yaml
audiosocket:
  format: slin16  # Would be overridden by caller codec
```

### After (Fixed)
```yaml
audiosocket:
  format: slin16  # Now enforced regardless of caller
streaming:
  chunk_size_ms: auto  # Dynamic reframing
  pacer_idle_cutoff_ms: 1200  # Prevent long tails
```

---

**Status**: ✅ Completed October 25, 2025  
**Tag**: `v1.0-p0-transport-stable`
