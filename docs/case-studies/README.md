# Production Case Studies

Comprehensive documentation of production-validated configurations with complete implementation details, metrics, and lessons learned.

## Available Golden Baselines

### **Full Agent Providers**

- **[Google Live Golden Baseline](Google-Live-Golden-Baseline.md)** ⚡ **FASTEST**
  - Response Latency: <1 second
  - Status: Production Ready (v4.2.0)
  - Best For: Interactive voice applications requiring natural conversation

- **[OpenAI Realtime Golden Baseline](OpenAI-Realtime-Golden-Baseline.md)**
  - Response Latency: <2 seconds
  - Status: Production Ready
  - Best For: Full agent mode with streaming capabilities

### **Pipeline Providers**

- **[Hybrid Pipeline Golden Baseline](Local-Hybrid-Golden-Baseline.md)**
  - Configuration: Local STT/TTS + Cloud LLM
  - Status: Production Ready
  - Best For: Cost optimization with local processing

### **Legacy References**

For quick reference links and pointer files, see: `docs/baselines/golden/`

## Document Format

Each case study includes:
- ✅ Validated production configuration
- 📊 Performance metrics and validation results
- 🎯 Critical settings and why they matter
- 🔧 Complete YAML configuration
- 📝 Lessons learned and troubleshooting
- 🚀 Deployment recommendations

## Related Documentation

- **[Milestones](../milestones/)** - Project milestone tracking
- **[Provider Setup Guides](../)** - Initial configuration guides
- **[Monitoring Guide](../MONITORING_GUIDE.md)** - Production monitoring
- **[Troubleshooting Guide](../TROUBLESHOOTING_GUIDE.md)** - Common issues and fixes
