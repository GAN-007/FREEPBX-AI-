# AI Voice Agent Roadmap

This roadmap tracks the development and evolution of the Asterisk AI Voice Agent. It combines the foundational work (Milestones 1-7), v4.0 GA production-readiness work (Transport Orchestrator & Audio Profiles), and future enhancements.

Each milestone includes scope, implementation details, and verification criteria.

## Milestone 1 — SessionStore-Only State (✅ Completed)

- **Goal**: Remove remaining legacy dictionaries from `engine.py` and rely exclusively on `SessionStore` / `PlaybackManager` for call state.
- **Tasks**:
  - Replace reads/writes to `active_calls`, `caller_channels`, etc. with SessionStore helpers.
  - Update cleanup paths so `/health` reflects zero active sessions after hangup.
  - Add lightweight logging when SessionStore migrations hit unknown fields.
- **Acceptance**:
  - Single test call shows: ExternalMedia channel created, RTP frames logged, gating tokens add/remove, `/health` returns `active_calls: 0` within 5s of hangup.
  - No `active_calls[...]` or similar dict mutations remain in the codebase.

## Milestone 2 — Provider Switch CLI (✅ Completed)

- **Goal**: Provide one command to switch the active provider, restart the engine, and confirm readiness.
- **What We Shipped**:
  - Added `scripts/switch_provider.py` and Makefile targets `provider-switch`, `provider-switch-remote`, and `provider-reload` for local + server workflows.
  - Health endpoint now reports the active provider readiness so the change can be validated at a glance.
- **Verification**:
  - `make provider=<name> provider-reload` updates `config/ai-agent.yaml`, restarts `ai-engine`, and the next call uses the requested provider. Logged on 2025-09-22 during regression.

## Milestone 3 — Model Auto-Fetch (✅ Completed)

- **Goal**: Automatically download and cache local AI models based on the host architecture.
- **What We Shipped**:
  - Added `models/registry.json` and the `scripts/model_setup.py` utility to detect hardware tier, download the right STT/LLM/TTS bundles, and verify integrity.
  - Makefile task `make model-setup` (documented in Agents/Architecture) calls the script and skips work when models are already cached.
- **Verification**:
  - First-run downloads populate `models/` on both laptops and the server; subsequent runs detect cached artifacts and exit quickly. Local provider boots cleanly after `make model-setup`.

## Milestone 4 — Conversation Coordinator & Metrics (✅ Completed)

- **Goal**: Centralize gating/barge-in decisions and expose observability.
- **What We Shipped**:
  - Introduced `ConversationCoordinator` (SessionStore-integrated) plus Prometheus gauges/counters for capture state and barge-in attempts.
  - Health endpoint now exposes a `conversation` summary and `/metrics` serves Prometheus data.
- **Verification**:
  - 2025-09-22 regression call shows coordinator toggling capture around playback, `ai_agent_tts_gating_active` returning to zero post-call, and `/metrics` scrape succeeding from the server.

## Milestone 5 — Streaming Transport Production Readiness (✅ Completed)

- **Goal**: Promote the AudioSocket streaming path to production quality with adaptive pacing, configurable defaults, and telemetry. Details and task breakdown live in `docs/milestones/milestone-5-streaming-transport.md`.
- **What We Shipped**:
  - Configurable streaming defaults in `config/ai-agent.yaml` (`min_start_ms`, `low_watermark_ms`, `fallback_timeout_ms`, `provider_grace_ms`, `jitter_buffer_ms`).
  - Post‑TTS end protection window (`barge_in.post_tts_end_protection_ms`) to prevent agent self‑echo when capture resumes.
  - Deepgram input alignment to 8 kHz (`providers.deepgram.input_sample_rate_hz: 8000`) to match AudioSocket frames.
  - AudioSocket default format set to μ-law with provider guardrails (`audiosocket.format=ulaw`, Deepgram/OpenAI `input_encoding=ulaw`) so inbound frames are decoded correctly.
  - Expanded YAML comments with tuning guidance for operators.
  - Regression docs updated with findings and resolutions.
- **Verification (2025‑09‑24 13:17 PDT)**:
  - Two-way telephonic conversation acceptable end‑to‑end; no echo‑loop in follow‑on turns.
  - Gating toggles around playback as expected; post‑TTS guard drops residual frames.
  - Deepgram regression replay shows no “low RMS” warnings once μ-law alignment is in place.
  - Operators can fine‑tune behaviour via YAML without code changes.

## Milestone 6 — OpenAI Realtime Voice Agent (✅ Completed)

- **Goal**: Add an OpenAI Realtime provider so Deepgram ↔️ OpenAI switching happens via configuration alone. Milestone instructions: `docs/milestones/milestone-6-openai-realtime.md`.
- **Dependencies**: Milestone 5 complete; OpenAI API credentials configured.
- **Primary Tasks**:
  - Implement `src/providers/openai_realtime.py` with streaming audio events.
  - Extend configuration schema and env documentation (`README.md`, `docs/Architecture.md`).
  - Align provider payloads with the latest OpenAI Realtime guide:
    - Use `session.update` with nested `audio` schema and `output_modalities` (e.g., `session.audio.input.format`, `session.audio.input.turn_detection`, `session.audio.output.format`, `session.audio.output.voice`).
    - Remove deprecated/unknown fields (e.g., `session.input_audio_sample_rate_hz`).
    - Use `response.create` without `response.audio`; rely on session audio settings. For greeting, send explicit `response.instructions`.
    - Add `event_id` on client events and handle `response.done`, `response.output_audio.delta`, `response.output_audio_transcript.*`.
  - Greeting behavior: send `response.create` immediately on connect with explicit directive (e.g., “Please greet the user with the following: …”).
  - VAD/commit policy:
    - When server VAD is enabled (`session.audio.input.turn_detection`), stream with `input_audio_buffer.append` only; do not `commit`.
    - When VAD is disabled, serialize commits and aggregate ≥160 ms per commit.
- **What We Shipped**:
  - Implemented `src/providers/openai_realtime.py` with robust event handling and transcript parsing.
  - Fixed keepalive to use native WebSocket `ping()` frames (no invalid `{"type":"ping"}` payloads).
  - Resampled AudioSocket PCM16 (8 kHz) to 24 kHz before commit and advertised 24 kHz PCM16 input/output in `session.update` so OpenAI Realtime codecs stay in sync.
  - μ-law alignment: requested `g711_ulaw` from OpenAI and passed μ-law bytes directly to Asterisk (file playback path), eliminating conversion artifacts.
  - Greeting on connect using `response.create` with explicit instructions.
  - Hardened error logging to avoid structlog conflicts; added correlation and visibility of `input_audio_buffer.*` acks.
  - Added YAML streaming tuning knobs (`min_start_ms`, `low_watermark_ms`, `jitter_buffer_ms`, `provider_grace_ms`) and wired them into `StreamingPlaybackManager`.
  - Refreshed `examples/pipelines/cloud_only_openai.yaml` so the monolithic OpenAI pipeline defaults to the 24 kHz settings and works out-of-the-box.

- **Verification (2025‑09‑25 08:59 PDT)**:
  - Successful regression call with initial greeting; two-way conversation sustained.
  - Multiple agent turns played cleanly (e.g., 16000B ≈2.0s and 40000B ≈5.0s μ-law files) with proper gating and `PlaybackFinished`.
  - No OpenAI `invalid_request_error` on keepalive; ping fix validated.

- **Acceptance**:
  - Setting `default_provider: openai_realtime` results in a successful regression call with greeting and two-way audio.
  - Logs show `response.created` → output audio chunks → playback start/finish with gating clear; no `unknown_parameter` errors.

## Milestone 7 — Configurable Pipelines & Hot Reload (✅ Completed)

- **Goal**: Support multiple named pipelines (STT/LLM/TTS) defined in YAML, with hot reload for rapid iteration. See `docs/milestones/milestone-7-configurable-pipelines.md`.
- **What We Shipped**:
  - YAML pipelines with `active_pipeline` switching and safe hot reload.
  - Pipeline adapters for Local, OpenAI (Realtime + Chat), Deepgram (STT/TTS), and Google (REST) with option merging.
  - Engine integration that injects pipeline components per call and preserves in‑flight sessions across reloads.
  - Logging defaults and knobs; streaming transport integration consistent with Milestone 5.
- **Validation (2025‑09‑27 → 2025‑09‑28)**:
  - Local‑only pipeline (TinyLlama) 2‑minute regression passed: greeting, STT finals, LLM replies (6–13 s), local TTS playback.
  - Hybrid pipeline A: local STT + OpenAI LLM + local TTS passed (two‑way conversation, stable gating).
  - Hybrid pipeline B: local STT + OpenAI LLM + Deepgram TTS passed (fast greeting/turns, clean playback).
  - Evidence captured in `docs/regressions/local-call-framework.md` with timestamps, byte sizes, and latency notes.
- **Acceptance**:
  - Swapping `active_pipeline` applies on the next call after reload.
  - Custom pipeline regressions succeed using YAML only.
  - Changing OpenAI/Deepgram endpoints or voice/model via YAML takes effect on next call.

## Milestone 8 — Transport Stabilization (✅ Completed Oct 25, 2025)

- **Goal**: Eliminate audio garble and pacing issues by enforcing AudioSocket invariants and proper format handling. Details in `docs/milestones/milestone-8-transport-stabilization.md`.
- **Tag**: `v1.0-p0-transport-stable`
- **What We Shipped**:
  - Fixed critical AudioSocket format override bug (commit `1a049ce`) preventing caller codec from overriding YAML wire format settings.
  - Enforced little-endian PCM16 on AudioSocket wire; removed all egress byte-swap logic.
  - Added pacer idle cutoff (1200ms) to prevent underflows and long tails.
  - Set `chunk_size_ms: auto` with 20ms default; reframe provider chunks to pacer cadence.
  - One-shot TransportCard logging at call start showing wire/provider formats.
- **Verification (2025-10-25, Call 1761424308.2043)**:
  - Clean two-way conversation end-to-end; user confirmed "Audio pipeline is working really well."
  - Zero underflows observed; wall_seconds ≈ content duration (no long tails).
  - SNR: 64.6-68.2 dB (excellent); provider bytes ratio 1.0 (perfect alignment).
  - TransportCard present in logs; no egress swap messages anywhere.
- **Acceptance**:
  - Golden metrics match baseline within 10% tolerance.
  - No garbled greeting; underflows ≈ 0; correct AudioSocket format per YAML.

## Milestone 9 — Audio Gating & Echo Prevention (✅ Completed Oct 26, 2025)

- **Goal**: Enable production-ready OpenAI Realtime provider with echo prevention and natural conversation flow. Corresponds to ROADMAPv4 P0.5 milestone.
- **What We Shipped**:
  - Audio Gating Manager (`src/core/audio_gating_manager.py`) with VAD-based interrupt detection.
  - Provider-specific gating (opt-in per provider) integrated with OpenAI Realtime.
  - Critical VAD configuration: `webrtc_aggressiveness: 1` (balanced mode) to prevent false echo detection.
  - 24kHz PCM16 input/output format for OpenAI; server-side VAD integration.
  - Golden baseline configuration documented in `OPENAI_REALTIME_GOLDEN_BASELINE.md`.
- **Verification (2025-10-26, Call 1761449250.2163)**:
  - Duration: 45.9s, SNR: 64.7 dB; zero self-interruption events.
  - Buffered: 0 chunks (vs 50 with aggressiveness: 0); gate closures: 1 time (vs 50+).
  - Natural conversation flow validated; user feedback: "much better results" ✅
- **Acceptance**:
  - Clean audio with no self-interruption; OpenAI's server VAD handles turn-taking naturally.
  - Gate stays open properly when agent speaking (correct behavior).

## Milestone 10 — Transport Orchestrator & Audio Profiles (✅ Completed Oct 26, 2025)

- **Goal**: Provider-agnostic operation with per-call audio profile selection and automatic capability negotiation. Implementation of ROADMAPv4 P1 milestone.
- **Dependencies**: Milestones 8-9 complete; golden baselines established.
- **What We Shipped**:
  - `TransportOrchestrator` class (`src/core/transport_orchestrator.py`) for dynamic profile resolution.
  - Audio profiles system in YAML: `telephony_ulaw_8k`, `openai_realtime_24k`, `wideband_pcm_16k`, `telephony_responsive`.
  - Per-call channel variable overrides (all optional; fallback to YAML):
    - `AI_PROVIDER` — which provider (deepgram, openai_realtime, local_hybrid)
    - `AI_AUDIO_PROFILE` — which transport profile
    - `AI_CONTEXT` — semantic context mapping to YAML `contexts.*` for prompt/greeting/profile
  - Provider capability negotiation with ACK parsing (Deepgram `SettingsApplied`, OpenAI `session.updated`).
  - Legacy config synthesis for backward compatibility; zero-change upgrade path.
- **Verification (2025-10-26)**:
  - Deepgram validation (Call 1761504353.2179): SNR 66.8 dB, `telephony_responsive` profile applied correctly.
  - OpenAI Realtime validation (Call 1761505357.2187): SNR 64.77 dB, `openai_realtime_24k` profile, perfect gating.
  - Dynamic profile switching via `AI_AUDIO_PROFILE` confirmed working without YAML edits.
- **Acceptance**:
  - Switching `AI_AUDIO_PROFILE` changes transport plan; call remains stable.
  - Provider ACK empty → remediation logged; call continues with fallback.
  - Multi-provider parity (Deepgram + OpenAI) demonstrated.

## Milestone 11 — Post-Call Diagnostics & Troubleshooting (✅ Completed Oct 26, 2025)

- **Goal**: Automated post-call RCA with AI-powered diagnosis matching manual analysis quality. ROADMAPv4 P2.1 deliverable.
- **What We Shipped**:
  - `agent troubleshoot` CLI command for instant post-call analysis.
  - RCA-level metrics extraction from Docker logs (provider bytes, drift, underflows, VAD, transport alignment).
  - Golden baseline comparison (OpenAI Realtime, Deepgram, streaming performance).
  - Format/sampling alignment detection (config vs runtime validation; catches AudioSocket format mismatches).
  - AI-powered diagnosis with context-aware prompts (OpenAI/Anthropic); filters benign warnings.
  - Quality scoring: 0-100 with EXCELLENT/FAIR/POOR/CRITICAL verdicts.
  - Greeting segment awareness (excludes timing artifacts from quality scoring).
- **Usage Examples**:

  ```bash
  ./bin/agent troubleshoot --last              # Analyze most recent call
  ./bin/agent troubleshoot --call 1761523231.2199
  ./bin/agent troubleshoot --last --provider anthropic
  ./bin/agent troubleshoot --last --no-llm     # Skip AI diagnosis
  ```

- **Verification (2025-10-26)**:
  - Call 2199 alignment test: Manual RCA "GOOD - SNR 67.3 dB" matched `agent troubleshoot` "EXCELLENT - 100/100" ✅
  - Format detection validated; catches slin vs ulaw mismatches, frame size alignment errors.
- **Acceptance**:
  - Accurate call detection (filters AudioSocket infrastructure channels).
  - RCA-level metrics depth matches manual analysis; AI diagnosis provides actionable fixes.

## Milestone 12 — Setup & Validation Tools (✅ Completed Oct 26, 2025)

- **Goal**: Complete operator workflow from zero to production; minimize time to first call. ROADMAPv4 P2.2 milestone.
- **What We Shipped**:
  - `agent init` — Interactive setup wizard with provider selection, credential management, template support (local|cloud|hybrid|openai-agent|deepgram-agent).
  - `agent doctor` — 11-point environment validation (Docker, ARI, AudioSocket, config, provider keys, logs, network, media directory).
  - `agent demo` — Audio pipeline validation without making real calls; validates before production.
  - `agent troubleshoot` — Post-call RCA (from Milestone 11).
  - Health checks with exit codes for CI/CD integration; JSON output for programmatic use.
- **Verification (2025-10-26)**:
  - `agent doctor` output: `✅ PASS: 9/11 checks — System is healthy and ready for calls!`
  - All tools validated on production server; work without user configuration changes.
- **Acceptance**:
  - `agent init` completes setup in < 5 minutes.
  - `agent doctor` validates environment before first call; clear error messages with remediation.
  - `agent demo` tests pipeline without real calls.
- **Impact**:
  - New operator to first call: **<30 minutes** (vs hours previously).
  - Self-service debugging without developer intervention.

## Milestone 13 — Config Cleanup & Migration (✅ Completed Oct 26, 2025)

- **Goal**: Simplify configuration, reduce operator footguns, and establish clear separation between production and diagnostic settings. ROADMAPv4 P2.3 milestone.
- **What We Shipped**:
  - Migration script: `scripts/migrate_config_v4.py` with dry-run and apply modes.
  - Moved 8 diagnostic settings to environment variables (`DIAG_*` prefix, `LOG_*` settings).
  - Deprecated 9 legacy settings with migration path and warnings.
  - Config version 4 schema validation; backward compatible with deprecation warnings.
  - 21% cleaner config (374 → 294 lines); 49% smaller file (16K → 8.1K).
- **Settings Moved to Environment Variables**:
  - `DIAG_EGRESS_SWAP_MODE`, `DIAG_ENABLE_TAPS`, `DIAG_TAP_PRE_SECS`, `DIAG_TAP_POST_SECS`
  - `DIAG_TAP_OUTPUT_DIR`, `STREAMING_LOG_LEVEL`
  - Safer production defaults (diagnostics opt-in only).
- **Verification (2025-10-26)**:
  - Migration script tested on production config; container rebuilt successfully.
  - Health checks pass (`agent doctor: 9/11 PASS`); no deprecation warnings with env vars.
- **Acceptance**:
  - Deprecated knobs removed from YAML schema; warnings logged if env var override set.
  - Config version field validated on load; migration instructions clear.
- **Impact**:
  - Clearer separation: production vs diagnostic settings in separate files.
  - Easier maintenance; diagnostic settings in one place (`.env`).

---

## Milestone 14 — Tool Calling System (✅ Completed Nov 10, 2025)

- **Goal**: Implement unified, provider-agnostic tool calling architecture enabling AI agents to perform real-world actions. Detailed specifications in `docs/milestones/milestone-16-tool-calling-system.md`.
- **What We Shipped**:
  - **Core Framework** (537 lines): `Tool`, `ToolDefinition`, `ToolRegistry`, `ToolExecutionContext`
  - **Provider Adapters** (417 lines): Deepgram + OpenAI Realtime format translation
  - **Telephony Tools** (736 lines): `transfer_call`, `cancel_transfer`, `hangup_call`
  - **Business Tools** (822 lines): `request_transcript`, `send_email_summary`
  - **Direct SIP Origination**: Eliminated Local channels for perfect audio
  - **Conversation Tracking**: Real-time turn history in both providers
  - **Documentation**: `TOOL_CALLING_GUIDE.md` (600+ lines)
- **Verification (2025-11-10)**:
  - Transfer execution: <150ms for 4-step cleanup sequence
  - Audio quality: Perfect bidirectional (SNR >60 dB), zero Local channel issues
  - Email delivery: 100% success rate with DNS MX validation
  - Calls validated: 1762880919.4536 (Deepgram), 1762734947.4251 (OpenAI)
- **Acceptance**:
  - Tools work with all providers without modification
  - <100ms execution overhead per tool call
  - Production-validated with 50+ successful executions
- **Impact**:
  - ~2,500 lines of new code across 10 files
  - Enabled AI agents to perform complex call workflows
  - Zero audio issues with direct SIP origination

---

## Milestone 16 — Tool Testing & Quality Assurance (✅ Completed Nov 12, 2025)

- **Goal**: Establish comprehensive test coverage for tool calling system with automated CI/CD enforcement to protect critical features and enable confident iteration.
- **What We Shipped**:
  - **Test Suite** (2,400 lines across 5 files):
    - 58 telephony tool tests (transfer, hangup, cancel_transfer)
    - 53 business tool tests (request_transcript, send_email_summary)
    - Total: 111 tool-specific tests
  - **Test Infrastructure**:
    - Shared pytest fixtures in `tests/tools/conftest.py`
    - Mock ARI client, SessionStore, Resend, DNS resolver
    - Async test patterns for background email sending
  - **CI/CD Integration**:
    - GitHub Actions enforcing 27% coverage threshold
    - Automated test runs on every push (~27 seconds)
    - Coverage reports (HTML, XML, JSON) uploaded as artifacts
  - **Bug Discovery**: Found and fixed blind transfer parameter bug during test development
  - **Coverage Impact**: 20% → 28-29% (+8.5% increase)
- **Test Coverage by Tool**:
  - `TransferCallTool`: 23 tests (warm/blind modes, extension resolution, error handling)
  - `HangupCallTool`: 17 tests (farewell messages, session cleanup)
  - `CancelTransferTool`: 18 tests (in-progress cancellation, state validation)
  - `RequestTranscriptTool`: 28 tests (email parsing, DNS validation, async sending)
  - `SendEmailSummaryTool`: 25 tests (auto-triggered, duration formatting, HTML templates)
- **Verification (2025-11-12)**:
  - All 276 tests passing in CI
  - Coverage: 27.18% (meets 27% threshold)
  - Test execution time: ~27 seconds
  - Email async behavior validated and documented
- **Acceptance**:
  - All tool execution paths tested in isolation
  - Error handling validated for edge cases
  - Mock configurations working correctly
  - CI prevents regressions on every commit
- **Impact**:
  - Protected mission-critical features (call control, email delivery)
  - Enabled confident iteration on tool system
  - Established testing patterns for future tools
  - Discovered 1 production bug before deployment

---

## Milestone 15 — AudioSocket + Pipeline Validation & Tool Execution (✅ Completed Nov 19, 2025)

- **Goal**: Validate AudioSocket transport with pipeline mode and enable tool execution for modular pipelines, achieving full transport/mode parity.
- **What We Shipped**:
  - **AudioSocket + Pipeline Validation**: Comprehensive 4-call test suite demonstrating AudioSocket works with both full agents AND pipelines.
  - **Tool Execution for Pipelines** (AAVA-85): Extended tool calling system to modular pipelines via OpenAI Chat Completions API.
    - LLMResponse dataclass with tool_calls support
    - Pipeline orchestrator executes tools via tool_registry
    - Terminal tool handling (hangup, transfer) with proper flow control
    - Conversation history persistence for email summaries
  - **Configuration Validation Improvements**: Tightened warnings to only flag when neither pipelines nor providers are configured.
  - **Documentation Updates**: Updated `Transport-Mode-Compatibility.md` to mark AudioSocket + Pipeline as validated with historical context.
- **Test Suite Results (Nov 19, 2025)**:
  - **Call 1** (1763610697.6282): Google Live monolithic, 36.34s, 186 frames ✅
  - **Call 2** (1763610742.6286): Deepgram monolithic, 34.36s, 176 frames ✅
  - **Call 3** (1763610785.6290): OpenAI Realtime monolithic, 71.29s, 360 frames, tool execution ✅
  - **Call 4** (1763610866.6294): local_hybrid pipeline, 54.57s, 277 frames, tool execution ✅
  - All calls: Continuous audio frame flow, clean two-way conversation, proper hangup
- **Key Fixes Referenced**:
  - `fbbe5b9` (Oct 27): Pipeline audio routing fix - added pipeline mode check BEFORE continuous_input provider routing
  - `181b210` (Oct 28): Pipeline gating enforcement - prevents feedback loop
  - `fbaaf2e` (Oct 28): Fallback safety margin increase for pipelines
- **Tool Execution Validation** (AAVA-85):
  - **Pipeline Tools Working**: transfer, hangup_call, send_email_summary, request_transcript
  - **Call 1763610866.6294**: local_hybrid pipeline executed hangup_call with farewell, email summary sent
  - **Call 1763582071.6214**: transfer to ringgroup 600 (Sales team) successful
  - **Critical Bugs Fixed** (5 iterations):
    - Config AttributeError: Fixed Engine.config vs self.app_config (commit a007241)
    - Hangup method error: Used correct ARI method hangup_channel() (commit cc125fd)
    - Farewell in email: Saved farewell to history before playback (commit 8058dab)
    - Farewell audio cutoff: Accurate duration calculation from audio bytes (commit 8058dab)
    - Conversation history preservation: Initialize from session.conversation_history (commit dd5bc5a)
- **Configuration Matrix Update**:
  - AudioSocket + Full Agent: ✅ VALIDATED
  - AudioSocket + Pipeline: ✅ VALIDATED (v4.0+)
  - ExternalMedia RTP + Pipeline: ✅ VALIDATED
- **Verification**:
  - All 4 transport/mode combinations now production-validated
  - Tool execution functional in both monolithic providers and pipelines
  - No false configuration warnings about valid combinations
- **Acceptance**:
  - AudioSocket + Pipeline produces clean two-way conversations with continuous audio flow
  - Pipelines can execute all telephony and business tools
  - Config validation warns only when neither providers nor pipelines are configured
- **Impact**:
  - Simplified transport selection: Both AudioSocket and ExternalMedia RTP work for all modes
  - Full tool parity: Pipelines have same capabilities as monolithic providers
  - Removed major deployment constraint documented since October 2025

---

## Milestone 17 — Monitoring, Feedback & Guided Setup (Planned)

- **Goal**: Ship an opt-in monitoring + analytics experience that is turnkey, captures per-call transcripts/metrics, and surfaces actionable YAML tuning guidance. Implementation details live in `docs/milestones/milestone-8-monitoring-stack.md`.
- **Dependencies**: Milestones 5–7 in place so streaming telemetry, pipeline metadata, and configuration hot-reload already work.
- **Workstreams & Tasks**:
  1. **Observability Foundation**
     - Add Prometheus & Grafana services to `docker-compose.yml` with persistent volumes and optional compose profile.
     - Expose Make targets (`monitor-up`, `monitor-down`, `monitor-logs`, `monitor-status`) plus SSH-friendly variants in `tools/ide/Makefile.ide` if needed.
     - Ensure `ai-engine` and `local-ai-server` `/metrics` export call/pipeline labels (`session_uuid`, `pipeline_name`, `provider_id`, `model_variant`).
  2. **Call Analytics & Storage**
     - Extend `SessionStore` (or dedicated collector) to emit end-of-call summaries: duration, turn count, fallback/jitter totals, sentiment score placeholder, pipeline + model names.
     - Archive transcripts and the associated config snapshot per call (e.g., `monitoring/call_sessions/<uuid>.jsonl` + `settings.json`).
     - Publish Prometheus metrics for recommendations (`ai_agent_setting_recommendation_total{field="streaming.low_watermark_ms"}`) and sentiment/quality trends.
  3. **Recommendations & Feedback Loop**
     - Implement rule-based analyzer that inspects call summaries and suggests YAML tweaks (buffer warmup, fallback timeouts, pipeline swaps) exposed via Prometheus labels and a lightweight `/feedback/latest` endpoint.
     - Document how to interpret each recommendation and where to edit (`config/ai-agent.yaml`).
  4. **Dashboards & UX**
     - Curate Grafana dashboards: real-time call board, pipeline/model leaderboards, sentiment timeline, recommendation feed, transcript quick links.
     - Keep dashboards auto-provisioned (`monitoring/dashboards/`) so `make monitor-up` renders data without manual import.
  5. **Guided Setup for Non-Linux Users**
     - Deliver a helper script (e.g., `scripts/setup_monitoring.py`) that checks Docker, scaffolds `.env`, snapshots current YAML, enables the monitoring profile, and prints Grafana credentials/URL.
     - Update docs/Architecture, Agents.md, `.cursor/…`, `.windsurf/…`, `Gemini.md` to mention the optional workflow.
- **Acceptance & Fast Verification**:
  - `make monitor-up` (or helper script) starts Prometheus + Grafana; Grafana reachable on documented port with dashboards populated during a smoke call.
  - After a call, a transcript + metrics artifact is created and the recommendation endpoint lists at least one actionable suggestion referencing YAML keys.
  - Disabling the stack (`make monitor-down`) leaves core services unaffected and removes Prometheus/Grafana containers.

Keep this roadmap updated after each milestone to help any collaborator—or future AI assistant—pick up where we left off.

---

## Future Roadmap

### Milestone 18 — Quality, Multi-Provider Demos, and Hi-Fi Audio (Planned)

- **Goal**: Improve resampling quality for hi-fi profiles and demonstrate multi-provider parity. ROADMAPv4 P3 milestone.
- **Dependencies**: Milestones 8-13 complete; golden baselines validated.
- **What We Plan to Ship**:
  - Higher-quality resamplers (speexdsp/soxr) for `hifi_pcm_24k` profile to improve frequency response.
  - Multi-provider demonstration configs showing Deepgram + OpenAI combinations.
  - Extended metrics for cadence reframe efficiency and pacer drift control.
  - Side-by-side audio quality comparisons at 24kHz sampling rate.
- **Timeline**: 2-4 days
- **Acceptance**:
  - Side-by-side playback comparisons show improved frequency response at 24kHz vs audioop resampling.
  - Drift remains ≈ 0%; pacer efficiency metrics stable.
  - Published demo configs for common provider combinations (deepgram-stt + openai-llm + deepgram-tts).

---

### v4.3.0 (November 2025) - Pipeline Tool Execution & Transport Validation

**Release Date**: November 19, 2025  
**Focus**: Full transport/mode parity and tool execution for modular pipelines

> **Note**: This work was completed in Milestone 15 and released as v4.3.0 (not v4.2.x). The v4.2.1 patch release (Nov 18) focused on onboarding improvements (quickstart wizard, enhanced installer).

**AudioSocket + Pipeline Validation**:

- Validated AudioSocket transport with pipeline mode (Milestone 15)
- 4-call comprehensive test suite (Google Live, Deepgram, OpenAI Realtime, local_hybrid)
- All transport/mode combinations now production-ready
- Documentation updated with historical context and v4.0+ validation notes

**Tool Execution for Pipelines** (AAVA-85):

- Extended tool calling to modular pipelines via OpenAI Chat Completions API
- Pipeline orchestrator executes tools via unified tool_registry
- Validated tools: transfer, hangup_call, send_email_summary, request_transcript
- Fixed 5 critical bugs during implementation (config, hangup method, farewell handling)

**Configuration Improvements**:

- Tightened validation warnings (only flag when neither providers nor pipelines configured)
- Updated Transport-Mode-Compatibility.md to reflect AudioSocket + Pipeline as validated
- Removed incorrect warnings about valid configurations

**Production Validation**:

- Call 1763610866.6294: local_hybrid pipeline, 54.57s, tool execution successful
- Call 1763582071.6214: transfer tool, ringgroup resolution working
- Zero false configuration warnings in production

---

### v4.4 Planning (Q1 2026)

**Testing & Quality**:

- ✅ Unit tests for tool adapters and email tools (111 tests, 27-29% coverage - Milestone 16)
- ✅ AudioSocket + Pipeline validation (4-call test suite - Milestone 15)
- ⏳ Integration tests for transfer workflows (unit tests complete, full workflow pending)
- 🎯 Increase CI coverage threshold to 30% then 40% (currently 27%)
- ⏳ Automated regression test suite (foundation in place)

**Additional Tool Categories** (In Progress):

- 🔄 Queue management tools - Transfer to queue, queue status (AAVA-63)
- 🔄 Calendar appointment tool - Book/check availability (AAVA-66)
- ⏳ Voicemail tools (leave message, retrieve messages)
- ⏳ Conference bridge tools (create, manage participants)
- ⏳ SMS/MMS tools (send text messages to caller)

**Additional Providers** (In Progress):

- 🔄 Azure Speech Services for STT/TTS (AAVA-64)
- 🔄 Google Cloud Speech for STT/TTS (AAVA-65)
- ⏳ Anthropic Claude integration for LLM
- ⏳ Google Gemini integration for LLM

**Advanced Features**:

- WebRTC SIP client integration for browser-based calls
- High availability / clustering for multi-server deployments
- Real-time dashboard for active call monitoring
- Call recording with consent management

**Performance**:

- GPU acceleration for local-ai-server
- Streaming latency optimizations (<500ms target)
- Memory usage profiling and optimization
- Parallel tool execution

**Documentation**:

- Video tutorials for setup and configuration
- Architecture deep-dives with diagrams
- Case studies from production deployments
- Tool calling best practices guide

---

## Release History

### v4.1.0 (November 2025) - Tool Calling & Agent Actions

**Release Date**: November 10, 2025  
**Focus**: Unified tool calling architecture enabling AI agents to perform real-world actions

**Tool Calling System**:

- **Unified Architecture**: Write tools once, use with any provider (Deepgram, OpenAI, custom pipelines)
- **Provider Adapters**: Automatic translation between provider formats (202 lines Deepgram, 215 lines OpenAI)
- **Base Framework**: `Tool`, `ToolDefinition`, `ToolRegistry` classes (537 lines total)
- **Execution Context**: Session-aware context with ARI access for real-time call control

**Telephony Tools** (5 tools shipped):

- **`transfer_call`**: Warm/blind transfers with direct SIP origination (504 lines)
  - Department name resolution ("support" → 6000)
  - Perfect bidirectional audio (eliminated Local channel issues)
  - <150ms execution time validated in production
- **`cancel_transfer`**: Cancel in-progress transfer before agent answers
- **`hangup_call`**: Graceful call termination with farewell message
  - Deepgram/OpenAI integration with HangupReady event
  - Prevents race conditions with farewell audio

**Business Tools** (2 tools shipped):

- **`request_transcript`**: Caller-initiated transcript delivery (475 lines)
  - Email parsing from speech ("john dot smith at gmail")
  - DNS MX validation and confirmation flow
  - Deduplication and admin BCC
- **`send_email_summary`**: Auto-send call summaries (347 lines)
  - Full conversation transcript with timestamps
  - Professional HTML formatting

**Agent CLI Tools**:

- **Binary Distribution**: Pre-built binaries for 5 platforms (Linux AMD64/ARM64, macOS Intel/Apple Silicon, Windows)
- **One-Line Installer**: `curl -sSL ... | bash` with platform auto-detection
- **GitHub Actions CI**: Automated builds and releases
- **5 CLI Commands**: `doctor`, `troubleshoot`, `demo`, `init`, `version`

**Conversation Tracking**:

- Real-time turn tracking in both Deepgram and OpenAI providers
- `conversation_history` field in `CallSession` model
- Enables email tools with full transcript context

**Critical Fixes**:

- **Direct SIP Origination**: Eliminated Local channels for perfect audio (AAVA-57, AAVA-58)
- **OpenAI VAD Timing**: Fixed greeting protection with `response.done` event (AAVA-62)
- **Email Tool Race Conditions**: Fixed 5 async bugs (AAVA-52)
- **Deepgram Hangup**: Added HangupReady event emission after farewell audio

**Documentation**:

- **New**: `docs/TOOL_CALLING_GUIDE.md` - Comprehensive 600+ line guide
- **Updated**: FreePBX Integration Guide, CLI Tools Guide, README, Architecture.md

**Production Validation**:

- Call IDs: 1762731796.4233 (Deepgram), 1762734947.4251 (OpenAI)
- Transfer execution: <150ms for 4-step cleanup sequence
- Email delivery: 100% success rate with MX validation

---

### v4.0.0 (October 2025) - Production-Ready GA Release

**Milestones Delivered**: 1-13 (September-October 2025)

**Foundation (Milestones 1-7)**:
- SessionStore-only state management
- Provider switching CLI and hot reload
- Model auto-fetch for local AI
- Conversation coordinator with metrics
- Streaming transport production readiness (AudioSocket)
- OpenAI Realtime voice agent integration
- Configurable pipelines with hot reload

**Production Readiness (Milestones 8-13)**:
- Transport stabilization (fixed AudioSocket format override bug)
- Audio gating & echo prevention (VAD-based, zero self-interruption)
- Transport orchestrator & audio profiles (provider-agnostic architecture)
- Post-call diagnostics (`agent troubleshoot` with AI-powered RCA)
- Setup & validation tools (`agent init/doctor/demo`)
- Config cleanup & migration (49% smaller configs)

**3 Golden Baselines Validated**:
- **Deepgram Voice Agent**: SNR 66.8 dB, telephony_responsive profile
- **OpenAI Realtime**: SNR 64.7 dB, zero self-interruption, natural conversation flow
- **Local Hybrid**: Vosk + OpenAI + Piper (functional with modern hardware)

**Technical Achievements**:
- Zero underflows, perfect pacing (drift ≈0%)
- Per-call context system with channel variables (`AI_PROVIDER`, `AI_AUDIO_PROFILE`, `AI_CONTEXT`)
- Both AudioSocket and ExternalMedia RTP transports validated
- New operator to first call: **<30 minutes** (vs hours previously)

---

### Pre-Release Development

- **v3.0** - Released Sep 16, 2025
- **v2.0** - Internal development (never released)
- **v1.0** - Initial concept (never released)

---

## Technical References

For detailed implementation plans and specifications:

- **docs/milestones/**: Detailed milestone implementation plans and technical specifications
- **OPENAI_REALTIME_GOLDEN_BASELINE.md**: OpenAI Realtime production configuration and validation

---

**Last Updated**: November 19, 2025  
**Roadmap Version**: 2.4 (Corrected v4.3.0 release alignment, updated future planning to v4.4)
