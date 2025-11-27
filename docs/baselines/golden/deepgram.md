# Deepgram — Golden Baseline

Use these references for a known-good Deepgram flow:

- Working Baseline Documentation: `logs/remote/rca-working-baseline-20251023-022434/WORKING_BASELINE_DOCUMENTATION.md`
- Golden Baseline Analysis: `logs/remote/rca-20251026-033115/GOLDEN_BASELINE_ANALYSIS.md`
- Regression Playbook (Deepgram): `docs/regressions/deepgram-call-framework.md`

Quick checks to match the baseline:
- AudioSocket upstream active; downstream streaming with automatic file fallback
- No underflows; drift ~0%; correct μ-law/PCM alignment by design
- Latency P95 ≲ 2s; clear, natural audio

