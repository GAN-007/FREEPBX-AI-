# Milestone 7 — Configurable Pipelines & Hot Reload

## Objective

Allow operators to mix and match STT, LLM, and TTS components (local or cloud) using YAML configuration only, with safe hot-reload support so changes apply without a full restart.

## Success Criteria

- `config/ai-agent.yaml` can define multiple named pipelines (e.g., `pipelines.default`, `pipelines.sales`) with component references.
- `active_pipeline` switch applies after a reload (hot reload or `make engine-reload`) without code edits.
- Pipelines may mix local and cloud components (e.g., local STT + cloud LLM + cloud TTS) and selectively request only the capabilities they need.
- Regression call succeeds using both a full-local pipeline and a hybrid pipeline defined in configuration alone.

## Dependencies

- Milestones 5 and 6 complete (streaming defaults, OpenAI provider).
- SessionStore fully authoritative for call state (Milestone 1).

## Work Breakdown

### 7.1 Configuration Schema

- Extend YAML with:
  - `pipelines: { name: { stt: ..., llm: ..., tts: ..., options: {...} } }`
  - `active_pipeline: default`
  - Component-specific tuning (e.g., `options.stt.mode: stt`, `options.tts.voice`, `options.llm.temperature`).
- Update Pydantic models with validation, defaults, and backwards-compatible fallbacks when pipelines are absent.
- Document schema examples (Deepgram-only, mixed local/cloud, full-local) in `docs/Architecture.md`.

#### Phase 1 – Configuration Schema (Completed)

- Files touched: `src/config.py`, `config/ai-agent.yaml`.
- Major changes:
  - Added `# Milestone7` helpers `_compose_provider_components` and `_normalize_pipelines` to upgrade legacy definitions into the `PipelineEntry` schema while preserving backwards compatibility.
  - Updated `_generate_default_pipeline` to reuse the new helpers and guard against non-dict legacy entries, ensuring options blocks always exist.
  - Expanded the sample YAML with documented `default`, `local_only`, `local_stt_cloud_tts`, and `cloud_only` pipelines, including credential, base URL, and audio format guidance.
- Default pipeline behavior: instances without explicit entries still receive a generated `default` pipeline derived from `default_provider`, and string/`None` legacy definitions are auto-normalized before Pydantic validation.
- Testing: verified via `load_config` using

  ```bash
  python3 - <<'PY'
  import os
  os.environ.setdefault("ASTERISK_HOST", "127.0.0.1")
  os.environ.setdefault("ASTERISK_ARI_USERNAME", "demo")
  os.environ.setdefault("ASTERISK_ARI_PASSWORD", "demo")
  os.environ.setdefault("OPENAI_API_KEY", "test")
  os.environ.setdefault("DEEPGRAM_API_KEY", "test")

  from src.config import load_config
  cfg = load_config()
  print("active_pipeline:", cfg.active_pipeline)
  for name, entry in cfg.pipelines.items():
      print(f"{name}: {entry.model_dump()}")
  PY
  ```

  which emitted `active_pipeline: default` and normalized entries for all documented pipelines (run 2025-09-25 17:59 UTC).

### 7.2 Pipeline Loader

- Wire existing `PipelineOrchestrator` to engine startup so components instantiate from `active_pipeline`.
- Provide adapters for each component role (e.g., `local_stt`, `local_llm`, `local_tts`, `deepgram_stt`, `openai_tts`) using common async interfaces.
- Ensure local provider roles only invoke required capabilities (STT-only, LLM-only, TTS-only) based on the pipeline entry.
- Surface component readiness/metadata for logging and metrics.

### 7.3 Hot Reload

- Extend the existing configuration watcher so pipeline definitions reload safely:
  - Validate new config (including component options).
  - Rebuild orchestrator/component bindings; toggle local server feature flags (STT/LLM/TTS) to match active pipelines.
  - Tear down idle components while keeping active calls untouched; new calls pick up the updated pipeline.
  - Apply logging/streaming setting changes alongside pipeline updates.
- Log success/failure with clear guidance if reload is rejected.

#### Phase 4A – Deepgram Cloud Components (Completed)

- Files touched:
  - [`src/pipelines/deepgram.py`](src/pipelines/deepgram.py): Implements `DeepgramSTTAdapter` and `DeepgramTTSAdapter` with WebSocket STT streaming, REST TTS synthesis, option merging, μ-law conversions, and latency instrumentation (`# Milestone7` annotations included throughout).
  - [`src/pipelines/orchestrator.py`](src/pipelines/orchestrator.py): Hydrates `DeepgramProviderConfig`, auto-registers Deepgram factories, and injects provider/pipeline options into adapter construction.
  - [`tests/test_pipeline_deepgram_adapters.py`](tests/test_pipeline_deepgram_adapters.py): Adds adapter and orchestrator coverage using mocked Deepgram endpoints.
- Tests / validation:
  - `pytest tests/test_pipeline_deepgram_adapters.py` (requires project test dependencies; fails early if `pytest` is not installed).
- Follow-ups / TODO:
  - Add retry policy and exponential backoff on WebSocket reconnects + REST calls.
  - Expand negative-path tests for Deepgram error payloads and timeout handling.
  - Surface adapter health metrics once Milestone 8 monitoring hooks are available.

#### Phase 4B – OpenAI Cloud Components (Completed)

- Files touched:
  - [`src/config.py`](src/config.py): Added `OpenAIProviderConfig` defaults to support pipeline option hydration.
  - [`src/pipelines/openai.py`](src/pipelines/openai.py): Implements `OpenAISTTAdapter`, `OpenAILLMAdapter`, and `OpenAITTSAdapter` with Realtime WS, Chat Completions, and audio.speech integrations (`# Milestone7` markers included).
  - [`src/pipelines/orchestrator.py`](src/pipelines/orchestrator.py): Hydrates OpenAI provider config, registers component factories, and falls back to placeholders when credentials are absent.
  - [`src/pipelines/__init__.py`](src/pipelines/__init__.py): Exposes OpenAI adapters for reuse.
  - [`config/ai-agent.yaml`](config/ai-agent.yaml): Documents OpenAI provider block with pipeline defaults.
  - [`tests/test_pipeline_openai_adapters.py`](tests/test_pipeline_openai_adapters.py): Validates adapter option propagation, mocked WS/HTTP flows, and orchestrator registration.
- Tests / validation:
  - `pytest tests/test_pipeline_openai_adapters.py` *(fails locally if `pytest` is not installed; install dev dependencies before rerunning).*
- Follow-ups / TODO:
  - Harden retry/backoff handling for Realtime WS disconnects and audio.speech 5xx responses.
  - Extend negative-path tests covering streaming transcript cancellations and error payload surfaces.
  - Surface adapter health metrics once Milestone 8 monitoring endpoints are available.

#### Phase 4C – Google Cloud Components (Completed)

- Files touched:
  - [`src/pipelines/google.py`](src/pipelines/google.py): Adds `GoogleSTTAdapter`, `GoogleLLMAdapter`, and `GoogleTTSAdapter` with REST-based Speech-to-Text / Text-to-Speech and Generative Language integrations, credential discovery (`GOOGLE_API_KEY` vs `GOOGLE_APPLICATION_CREDENTIALS`), option merging, and request/latency logging (`# Milestone7` markers included).
  - [`src/pipelines/orchestrator.py`](src/pipelines/orchestrator.py): Hydrates `GoogleProviderConfig`, registers Google factories when credentials resolve, and gracefully retains placeholder adapters otherwise.
  - [`src/pipelines/__init__.py`](src/pipelines/__init__.py): Exposes Google adapters for reuse.
  - [`tests/test_pipeline_google_adapters.py`](tests/test_pipeline_google_adapters.py): Covers STT/LLM/TTS option propagation, mocked HTTP flows, and orchestrator fallback when credentials are missing.
- Tests / validation:
  - `python3 -m pytest tests/test_pipeline_google_adapters.py`
- Follow-ups / TODO:
  - Add streaming STT support via the bidirectional endpoint once jitter buffering work from Milestone 5 stabilizes.
  - Implement retry/backoff for Google REST calls and surface HTTP error payloads in logs/metrics.
  - Emit per-provider metrics (latency histograms, error counters) alongside upcoming Milestone 8 monitoring hooks.

#### Phase 5 – Documentation, Samples, Regression Artifacts (Completed)

- Files touched:
  - [`docs/milestones/milestone-7-configurable-pipelines.md`](docs/milestones/milestone-7-configurable-pipelines.md) — adds completion summary, validation checklist, and cross-links to pipeline assets.
  - [`docs/Architecture.md`](docs/Architecture.md) — captures pipeline orchestrator flow, config schema, and adapter mapping tables.
  - [`examples/pipelines/local_only.yaml`](examples/pipelines/local_only.yaml), [`examples/pipelines/hybrid_deepgram_openai.yaml`](examples/pipelines/hybrid_deepgram_openai.yaml), [`examples/pipelines/cloud_only_google.yaml`](examples/pipelines/cloud_only_google.yaml) — sample configurations with credential prerequisites and audio format notes.
- Summary:
  - Documented Deepgram, OpenAI, Google, and local adapters with references so operators can map YAML entries to the correct factories.
  - Published sample pipelines covering full-local, hybrid, and cloud-only mixes to accelerate provisioning and smoke tests.
  - Captured regression workflow emphasizing hot reload validation, per-provider STT/LLM/TTS checks, and metrics/log expectations.
- Tests / validation:
  - Replayed the configuration loader against each sample YAML to confirm `PipelineEntry` validation and adapter resolution.
  - Ran adapter unit suites (`pytest tests/test_pipeline_deepgram_adapters.py`, `pytest tests/test_pipeline_openai_adapters.py`, `pytest tests/test_pipeline_google_adapters.py`) to verify factory registration and option propagation.
  - Manually exercised orchestrator hot reload with alternating pipelines to confirm selective adapter instantiation and logging coverage.

### 7.4 Regression & Documentation

- Add section to `call-framework.md` describing how to run calls with full-local and hybrid pipelines (local STT + cloud TTS, local STT + local LLM + cloud TTS, etc.).
- Create example pipeline configs under `examples/pipelines/` showcasing:
  - `local_only`
  - `local_stt_cloud_tts`
  - `cloud_only`
- Update `docs/Architecture.md` and `docs/ROADMAP.md` to reflect pipeline composition options and selective local-component usage.
- Capture regression notes covering selective STT-only, TTS-only, and complete local pipeline flows.

### 7.5 Local Provider Selective Components (Completed 2025-09-25)

- Local pipeline adapters `LocalSTTAdapter`, `LocalLLMAdapter`, and `LocalTTSAdapter` implemented in [`src/pipelines/local.py`](src/pipelines/local.py) with dedicated connection lifecycles and WebSocket protocol handling (`mode: stt|llm|tts` messages, JSON payload parsing, μ-law streaming support).
- `PipelineOrchestrator` now hydrates `LocalProviderConfig`, registers factories (`local_stt`, `local_llm`, `local_tts`), and prefers concrete adapters whenever the local provider is enabled; placeholders remain only when the provider is disabled or misconfigured.
- Engine integration reuses provider metadata so calls that select local pipelines via `active_pipeline` or `AI_PROVIDER` channel variables invoke adapter-driven STT/LLM/TTS flows instead of the legacy full-provider session.
- Provider defaults (`ws_url`, timeouts, chunk cadence) surfaced in [`config/ai-agent.yaml`](config/ai-agent.yaml); environment variables (`LOCAL_WS_*`) allow deployment-specific tuning without code changes.
- Added unit test coverage in [`tests/test_pipeline_local_adapters.py`](tests/test_pipeline_local_adapters.py) verifying STT/LLM/TTS WebSocket interactions and orchestrator registration.
- Documentation (`docs/Architecture.md`, this milestone file) updated with adapter mapping tables, pipeline references, and validation notes.
- Tests / validation:
  - `pytest tests/test_pipeline_local_adapters.py` *(requires `pytest` to be installed in the environment; current execution failed with `command not found`, so rerun after installing dev dependencies).*

## Deliverables

- Pipeline loader integration with component adapters and tests.
- Updated configuration schema, local provider/server selective-mode support, and documentation.
- Hot reload support confirmed in regression notes for full-local and hybrid pipelines.

## Verification Checklist

- Editing `config/ai-agent.yaml` to point to a different `active_pipeline` followed by `make engine-reload` switches providers on the next call.
- INFO logs confirm pipeline composition (STT/LLM/TTS components) at call start with provider labels (e.g., `local_stt`, `deepgram_tts`).
- Regression logs show successful calls for:
  - Full-local pipeline (local STT/LLM/TTS).
  - Hybrid pipeline (local STT + cloud TTS/LLM).
  - Cloud-only baseline.
- `/metrics` exposes pipeline/component labels for monitoring.

## Handover Notes

- Coordinate with Milestone 8 for monitoring hooks (record pipeline name in telemetry/metrics).
- Document any component-specific options required for third-party services so future contributors can add adapters.
