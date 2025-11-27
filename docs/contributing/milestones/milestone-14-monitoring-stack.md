# Milestone 14 — Monitoring, Feedback & Guided Setup

Deliver an opt-in monitoring and analytics experience that works out of the box, captures per-call transcripts, and recommends YAML tuning changes so operators can improve call quality quickly.

---

## 1. Objectives & Success Criteria

- **One-command enablement**: `make monitor-up` (or helper script) launches Prometheus + Grafana with provisioning; stack is opt-in and dormant by default.
- **Per-call insight**: Every completed call produces a metrics + transcript artifact with pipeline/provider/model metadata and a summary in Prometheus.
- **Actionable feedback**: Recommendation engine suggests concrete `config/ai-agent.yaml` tweaks based on measured jitter, fallback frequency, latency, and sentiment.
- **Friendly onboarding**: A guided setup script handles Docker checks, `.env` scaffolding, monitoring bootstrap, and links to Grafana for non-Linux users.
- **Documentation parity**: Architecture, Roadmap, and IDE rule files instruct collaborators on enabling, interpreting, and extending the monitoring stack.

Verification quick check: run the helper script, place a single call, confirm dashboards populate live, view the stored transcript artifact, and retrieve at least one recommendation referencing YAML keys.

---

## 2. Prerequisites & Reference Docs

- Milestones 5–7 complete (streaming telemetry, pipeline metadata, hot reload).
- Prometheus metrics already exposed at `/metrics` on `ai-engine` and `local-ai-server` (labels may need extension).
- Follow-on documentation updates required in:
  - `docs/Architecture.md`
  - `docs/ROADMAP.md`
  - `Agents.md`, `.cursor/rules/asterisk_ai_voice_agent.mdc`, `.windsurf/rules/asterisk_ai_voice_agent.md`, `Gemini.md`
- Coordinate secrets handling: `.env` must include Grafana admin password when stack is enabled (document defaults and overrides).

---

## 3. Implementation Phases

### Phase 1 — Telemetry & Data Model

1. **Metric schema**
   - Define structured labels: `call_uuid`, `pipeline_name`, `provider_id`, `model_variant`, `recommendation_key`.
   - Add counters/histograms:
     - `ai_agent_call_duration_seconds`
     - `ai_agent_call_turns_total`
     - `ai_agent_call_jitter_low_watermark_hits_total`
     - `ai_agent_call_fallback_total`
     - `ai_agent_call_sentiment_score` (gauge updated at hangup)
     - `ai_agent_setting_recommendation_total{field="streaming.low_watermark_ms"}`
   - Extend `/metrics` exporters in `ai-engine` & `local-ai-server` to emit summaries on call end (SessionStore hook or event bus).
2. **Transcript capture**
   - Persist call artifacts under `monitoring/call_sessions/<call_uuid>/`:
     - `transcript.jsonl` (turn-by-turn text with sentiment and timestamps)
     - `metrics.json` (call summary, pipeline/provider info, engine version)
     - `config_snapshot.yaml` (subset of `config/ai-agent.yaml` relevant to recommendations)
   - Ensure rotation/cleanup policy (configurable retention; document defaults).

### Phase 2 — Recommendation Engine

1. **Rule definitions**
   - Map metrics to YAML adjustments, e.g.:
     - Jitter events > threshold ⇒ raise `streaming.low_watermark_ms`.
     - Fallback count > 0 ⇒ increase `streaming.fallback_timeout_ms`.
     - High latency + negative sentiment ⇒ suggest switching pipeline or provider.
2. **Execution**
   - Run rules at call teardown;
   - Store results in memory (for `/feedback/latest` endpoint) and export as Prometheus counters with `field` and `severity` labels.
3. **API & CLI**
   - Add `/feedback/latest` (JSON) including call metadata, top recommendations, and direct YAML key references.
   - Optional `scripts/monitoring/report_latest.py` to print recommendations in terminal.

### Phase 3 — Monitoring Stack Infrastructure

1. **docker-compose updates**
   - Add `prometheus` and `grafana` services under a `monitoring` profile.
   - Configure scrape configs for engine + local server (use environment variables for endpoints).
   - Mount volumes:
     - `./monitoring/prometheus/:/etc/prometheus`
     - `./monitoring/grafana/provisioning/:/etc/grafana/provisioning`
     - `./monitoring/grafana/dashboards/:/var/lib/grafana/dashboards`
2. **Make targets**
   - `monitor-up` (with optional `profile=monitoring` override)
   - `monitor-down`
   - `monitor-logs`
   - `monitor-status` (wrapper around `docker-compose ps` filtered for monitoring services)
   - SSH variants in `tools/ide/Makefile.ide` mirroring deployment workflow.
3. **Grafana provisioning**
   - Data source config auto-provisions Prometheus at startup.
   - Dashboard provisioning loads JSON from `monitoring/dashboards/`.
   - Document default admin credentials (pull from `.env`).

### Phase 4 — Dashboards & UX

1. **Dashboards**
   - `Call Overview`: active calls, last 10 calls table with sentiment, duration, recommendations count.
   - `Streaming Health`: jitter buffer depth, fallback rate, transport restarts, AudioSocket vs fallback usage.
   - `Provider/Pipeline Insights`: call volume per pipeline, latency distribution, sentiment by model.
   - `Recommendation Feed`: panel reading `ai_agent_setting_recommendation_total` with links to YAML keys.
2. **Annotations & Links**
   - Link dashboard panels to transcript artifacts (using Grafana data links to `monitoring/call_sessions`).
   - Provide panel descriptions instructing operators how to act on metrics.
3. **Usability polish**
   - Hide Prometheus internals, expose only curated dashboards via Grafana home.
   - Include call-out text for “Optional stack — safe to disable when not needed”.

### Phase 5 — Guided Setup & Documentation

1. **Helper script (`scripts/setup_monitoring.py`)**
   - Detect Docker/Compose availability, prompt for Grafana password, ensure `.env` entries exist.
   - Snapshot `config/ai-agent.yaml` into `monitoring/config_snapshots/`.
   - Run `make monitor-up` and open/print Grafana URL.
   - If requested, run smoke check (`curl http://localhost:15000/health`) before completion.
2. **Documentation updates**
   - Expand `docs/Architecture.md` monitoring section (ports, expectations, data retention).
   - Add quick-start section describing helper script workflow + manual commands.
   - Update `docs/ROADMAP.md` (done as part of this milestone).
   - Sync IDE rule files (`Agents.md`, `.cursor`, `.windsurf`, `Gemini.md`).
3. **Training content**
   - Provide short “interpreting recommendations” table mapping Grafana panels → YAML fields → effect on call quality.
   - Document transcript access path and retention guidelines.

---

## 4. Testing & Verification Checklist

- **Local smoke**: helper script completes; Grafana accessible; dashboards populate during `make call-smoke` (or manual test call).
- **Metrics validation**: Prometheus metrics show labels for call UUID, pipeline, provider, model; new histograms/counters scrape without errors.
- **Recommendation engine**: Induce a fallback or jitter event (via config tweak), confirm `/feedback/latest` surfaces matching YAML suggestions.
- **Transcript artifacts**: Inspect `monitoring/call_sessions/<uuid>/transcript.jsonl` and ensure timestamps, speaker labels, sentiment entries exist.
- **Disable flow**: `make monitor-down` leaves only AI engine containers running; rerun helper script to ensure idempotency.
- **Docs parity**: Architecture and Roadmap reflect new workflow; IDE rule files mention helper script and optional monitoring profile.

---

## 5. Security, Data & Maintenance Considerations

- Restrict Grafana access (bind to localhost by default, expose reverse proxy guidance for remote access).
- Sanitize transcripts before storage if sensitive data expected; document retention settings and cleanup schedule.
- Keep Prometheus/Grafana images pinned to specific tags; include `make monitor-update` task if upgrading frequently.
- Ensure recommendation logic fails safe (no advice if metrics missing); log warnings for incomplete data.
- Provide mechanism to purge old artifacts (`make monitor-clean` optional task) without removing dashboards.

---

## 6. Deliverables Summary

- Updated code/config: metrics instrumentation, recommendation module, transcript persistence.
- `docker-compose.yml` monitoring profile + new Make/IDE targets.
- Grafana provisioning + dashboard JSON files.
- Helper script for onboarding non-Linux users.
- Documentation updates across Architecture, Roadmap, and IDE rules.
- Verification artifacts (screenshots or metrics captures) recorded under `docs/regressions/` as proof of completion.

Complete these phases iteratively, landing Prometheus/Grafana scaffolding first, then layering analytics, recommendations, and onboarding tooling. Coordinate with stakeholders to calibrate recommendation thresholds before release.
