# Milestone 5 — Streaming Transport Production Readiness

## Objective

Promote the experimental AudioSocket downstream streaming path to production quality with adaptive pacing, configurable defaults, and regression coverage. After this milestone the default install must deliver clear greetings and low-latency turns without manual tuning.

## Success Criteria

- Default configuration (fresh clone, no manual tweaks) produces a clean two-way Deepgram call with < 1 restart of the streaming pipeline per turn.
- Telemetry logs report jitter buffer depth, restarts, and fallback triggers for every call.
- Operators can adjust streaming quality through documented YAML settings without editing code.
- Regression guide updated with new streaming checklist and tuning notes.

## Dependencies

- Milestones 1–4 complete (SessionStore, provider switching, model setup, ConversationCoordinator).
- Existing Deepgram AudioSocket integration working end-to-end.

## Work Breakdown

### 5.1 Configurable Streaming Settings

- Add the following fields to `config/ai-agent.yaml` under `streaming`:
  - `min_start_ms`
  - `low_watermark_ms`
  - `fallback_timeout_ms`
  - `provider_grace_ms`
  - `logging_level` (optional override per component)
- Update `src/config.py` models and validation.
- Document defaults and overrides in `docs/Architecture.md` (see "Streaming Transport Defaults" section) and double-check `docs/ROADMAP.md` references.

### 5.2 StreamingPlaybackManager Enhancements

- Modify `src/core/streaming_playback_manager.py` to:
  - Convert `min_start_ms`/`low_watermark_ms` into chunk counts at runtime.
  - Pause/resume output when depth < low watermark instead of tearing down streams.
  - Reset fallback timer whenever a frame is actually transmitted.
  - Respect `provider_grace_ms` before discarding late provider chunks after cleanup.
- Add structured debug logging guarded by the new logging level.

### 5.3 Telemetry & Metrics

- Extend `SessionStore` to capture per-call streaming metrics (depth, restarts, fallbacks, codec info).
- Expose new Prometheus counters/gauges (see `docs/Architecture.md` for list).
- Emit a summary tuning hint at INFO level when a call ends (e.g., suggest increasing `min_start_ms`).

### 5.4 Regression & Documentation

- Update `docs/regressions/deepgram-call-framework.md` with the new test procedure and expected metrics.
- Update `call-framework.md` regression template with Streaming QA section.
- Add a “Streaming Defaults & Tuning” section to `docs/Architecture.md` and highlight YAML keys.

## Deliverables

- Code changes merged on `develop` with updated tests.
- Documentation updates committed (`docs/Architecture.md`, roadmap, regression guide).
- Sample telemetry output included in the regression log.

## Verification Checklist

- Clean call log shows no more than one `STREAMING STARTED` event per turn.
- INFO logs include tuning summary (even if all metrics look healthy).
- Regression doc updated with date, call ID, and audio quality assessment.

## Handover Notes

- Leave comments in `docs/milestones/milestone-6-openai-provider.md` if any follow-up is required for provider integration.
- Flag any config or logging changes that alter deployment procedures so IDE rule files can be updated.

---

## 5.6 Barge-In Functionality (Subtask)

### Objective

Allow the caller to interrupt agent TTS playback (“barge-in”) cleanly: detect sustained inbound speech while TTS is active, stop the current playback deterministically, re-enable capture, and forward the caller’s audio to the provider with minimal latency and no self-echo.

### Success Criteria

- Caller speech during an agent turn reliably interrupts playback within ≤300 ms.
- No self-echo: provider does not ingest the agent’s downlink audio at the start of a turn.
- Duplicate `PlaybackFinished` warnings are eliminated (single registration).
- Metrics show barge-in events and capture gating transitions per call.

### Design Outline

- Engine gating and detection
  - `src/engine.py::_audiosocket_handle_audio`:
    - While TTS playback is active (via `session.audio_capture_enabled == False`), monitor inbound frames.
    - Qualify “speech” using a small rolling window (e.g., ≥200–300 ms of frames and/or RMS energy threshold) to mitigate false positives.
    - On trigger, invoke barge-in:
      - Stop the active playback (see ARI stop API below).
      - Clear the gating token via `ConversationCoordinator.on_tts_end(call_id, playback_id, reason="barge-in")`.
      - Continue forwarding inbound frames to the provider; set a short cooldown to avoid rapid re-triggers.
  - Keep initial self-echo protection window (≈200 ms) after playback starts to mask early downlink leakage, but do not block beyond barge-in detection.

- ARI playback control
  - Extend `src/ari_client.py` with `stop_playback(playback_id: str)` and/or `stop_all_bridge_playbacks(bridge_id: str)` (DELETE `/playbacks/{playbackId}` or iterate active IDs) so barge-in can preempt audio deterministically.
  - Ensure `Engine` tracks the current playback ID in `SessionStore` (`PlaybackRef`) so we can cancel the correct one.

- State and coordination
  - `src/core/conversation_coordinator.py` maintains gating (`audio_capture_enabled`) and emits metrics.
  - Add `note_audio_during_tts(call_id)` on pre-trigger frames to increment barge-in attempts counter.

### Metrics (Milestone 8 tie-in)

- Gauges (existing):
  - `ai_agent_audio_capture_enabled{call_id}`
  - `ai_agent_tts_gating_active{call_id}`
- Counters (existing/extend):
  - `ai_agent_barge_in_events_total{call_id}` (increment on successful barge-in trigger)
- Optional timings:
  - Time-to-barge-in from playback start; number of false/true barge-in detections per call.

### Configuration (YAML)

- Under `streaming:` or a new `barge_in:` section:
  - `barge_in.enabled: true`
  - `barge_in.min_ms: 250` (minimum continuous speech window to trigger)
  - `barge_in.energy_threshold: <int>` (optional RMS threshold)
  - `barge_in.cooldown_ms: 500` (post-trigger cooldown)
  - `barge_in.initial_protection_ms: 200` (self-echo guard after TTS start)

### Test Plan

- Place test calls with early caller speech during agent greeting/turn.
- Verify logs:
  - `Dropping inbound AudioSocket audio during TTS playback` only within the protection window.
  - A single barge-in trigger per interruption: playback cancelled; `audio_capture_enabled` flips to true; inbound frames flow to provider.
  - No duplicate `PlaybackFinished` warnings.
- Validate `/metrics` barge-in counters Increased; `/health` conversation summary reflects capture transitions.

### Risks & Mitigations

- False positives (background noise) → require both time window and energy threshold; add cooldown.
- ARI stop timing races → use deterministic playback IDs and handle 404 safely if already finished.
- Echo leakage window → maintain small initial protection period; consider tighter bridge mix or echo cancellation (out of scope for this milestone).
