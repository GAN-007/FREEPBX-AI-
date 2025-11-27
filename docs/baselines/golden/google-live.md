# Google Live — Golden Baseline

Use these references for a known-good Google Live flow:

- Google Live Golden Baseline: `docs/case-studies/Google-Live-Golden-Baseline.md`
- Milestone Documentation: `docs/milestone/milestone-17-google-live.md`
- Provider Setup Guide: `docs/GOOGLE_PROVIDER_SETUP.md`

Quick checks to match the baseline:
- Response latency < 1 second (fastest available)
- Full bidirectional streaming with native barge-in
- Complete transcription (user and AI speech captured separately)
- Clean call termination with hangup_call tool
- No manual hangup required
- Tool execution in streaming mode

