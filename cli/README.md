# Agent CLI Tools

Go-based command-line interface for Asterisk AI Voice Agent operations.

## Overview

The `agent` CLI provides a comprehensive set of tools for setup, diagnostics, and troubleshooting. All commands are built as a single Go binary for easy distribution.

**Current Status**: ✅ Binary builds available for v4.1+

## Available Commands

- **`agent init`** - Interactive setup wizard
- **`agent doctor`** - System health check and diagnostics
- **`agent demo`** - Audio pipeline validation
- **`agent troubleshoot`** - Post-call analysis and RCA
- **`agent version`** - Show version information

## Installation

### Quick Install (Recommended)

**Linux/macOS:**
```bash
curl -sSL https://raw.githubusercontent.com/hkjarral/Asterisk-AI-Voice-Agent/main/scripts/install-cli.sh | bash
```

This will:
- Detect your platform automatically
- Download the latest binary
- Verify checksums
- Install to `/usr/local/bin`
- Test the installation

### Manual Download

Download pre-built binaries from [GitHub Releases](https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases):

**Linux:**
```bash
# AMD64 (most Linux servers)
curl -L -o agent https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases/latest/download/agent-linux-amd64
chmod +x agent
sudo mv agent /usr/local/bin/

# ARM64 (Raspberry Pi, AWS Graviton)
curl -L -o agent https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases/latest/download/agent-linux-arm64
chmod +x agent
sudo mv agent /usr/local/bin/
```

**macOS:**
```bash
# Intel Macs
curl -L -o agent https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases/latest/download/agent-darwin-amd64
chmod +x agent
sudo mv agent /usr/local/bin/

# Apple Silicon (M1/M2/M3)
curl -L -o agent https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases/latest/download/agent-darwin-arm64
chmod +x agent
sudo mv agent /usr/local/bin/
```

**Windows:**
Download `agent-windows-amd64.exe` from releases and add to your PATH.

### Verify Installation

```bash
agent version
```

## Building from Source

### Prerequisites

- Go 1.21 or newer
- Linux/macOS/Windows

### Build Instructions

```bash
# From project root
make cli-build

# Or build manually
cd cli
go build -o ../bin/agent ./cmd/agent
```

### Build for All Platforms

```bash
# Creates binaries for Linux, macOS, Windows (AMD64 & ARM64)
make cli-build-all

# Generate checksums
make cli-checksums

# Complete release build
make cli-release
```

## Command Reference

### `agent init` - Interactive Setup Wizard

Guided setup wizard to configure Asterisk AI Voice Agent from scratch.

**Usage:**
```bash
agent init
```

**Steps:**
1. **Asterisk ARI Connection** - Host, username, password validation
2. **Audio Transport** - AudioSocket (modern) or ExternalMedia RTP (legacy)
3. **AI Provider** - OpenAI, Deepgram, Google, or Local Hybrid
4. **Configuration Review** - Saves to `.env` and restarts services

**Example:**
```bash
$ agent init

Step 1/4: Asterisk ARI Connection
Enter Asterisk host [127.0.0.1]: 
Enter ARI username: AIAgent
Enter ARI password: ******
✓ Testing ARI connection... Success!

Step 2/4: Audio Transport Selection
  1) AudioSocket (Modern, for full agents) [RECOMMENDED]
  2) ExternalMedia RTP (Legacy, for hybrid pipelines)
Your choice [1]: 1

Step 3/4: AI Provider Selection
  1) OpenAI Realtime (0.5-1.5s response time)
  2) Deepgram Voice Agent (1-2s response time)
  3) Local Hybrid (3-7s, privacy-focused)
Your choice [1]: 1

Enter OpenAI API Key: sk-...
✓ API key validated
✓ Configuration saved to .env
✓ Docker services restarted

Setup complete! 🎉
```

---

### `agent doctor` - System Health Check

Comprehensive health check and diagnostics tool.

**Usage:**
```bash
agent doctor [flags]
```

**Flags:**
- `--fix` - Attempt to auto-fix issues (future)
- `--json` - Output as JSON
- `--verbose` - Show detailed check output

**Exit Codes:**
- `0` - All checks passed ✅
- `1` - Warnings detected (non-critical) ⚠️
- `2` - Failures detected (critical) ❌

**Checks Performed:**
- Docker daemon and containers running
- Asterisk ARI connectivity
- AudioSocket/RTP ports available
- Configuration file validity
- API keys present
- Provider API connectivity
- Recent call history
- Disk space availability

**Example:**
```bash
$ agent doctor

[1/11] Docker Daemon...     ✅ Docker running (v26.1.4)
[2/11] Containers...        ✅ ai-engine running (healthy)
[3/11] Asterisk ARI...      ✅ Connected to 127.0.0.1:8088
[4/11] AudioSocket Port...  ✅ Port 8090 listening
[5/11] Configuration...     ✅ YAML valid
[6/11] API Keys...          ✅ OPENAI_API_KEY present
[7/11] Provider Connectivity... ✅ OpenAI API reachable (134ms)

Summary: 10 passed, 0 warnings, 0 failures
✅ System is healthy - ready for calls!
```

**Use in Scripts:**
```bash
if ! agent doctor; then
    echo "Health check failed"
    exit 1
fi
```

---

### `agent demo` - Audio Pipeline Validation

Tests audio pipeline without making real calls.

**Usage:**
```bash
agent demo [flags]
```

**Flags:**
- `--provider <name>` - Test specific provider
- `--duration <seconds>` - Test duration (default: 10)
- `--verbose` - Show detailed test output

**What It Tests:**
1. Audio capture and VAD
2. Provider STT/LLM/TTS integration
3. Audio quality and latency
4. Playback path

**Example:**
```bash
$ agent demo

Testing Audio Pipeline (OpenAI Realtime)
✓ Audio capture initialized
✓ Provider connection established
✓ Test audio processed (latency: 245ms)
✓ Playback successful

Pipeline validated successfully!
```

---

### `agent troubleshoot` - Post-Call Analysis

Analyze call issues with root cause analysis.

**Usage:**
```bash
# Analyze most recent call
agent troubleshoot

# Analyze specific call
agent troubleshoot <call_id>

# With verbose output
agent troubleshoot --verbose <call_id>
```

**Analysis Includes:**
- Call duration and timeline
- Audio transport issues
- Provider errors and latency
- Tool execution problems
- Configuration mismatches
- Suggested fixes

**Example:**
```bash
$ agent troubleshoot 1763582071.6214

Analyzing call 1763582071.6214...

Call Summary:
  Duration: 43.2s
  Provider: local_hybrid
  Transport: ExternalMedia RTP
  Tools Used: transfer

Issues Found:
  ✅ No critical issues
  ⚠️  High latency detected (3.2s avg)

Recommendations:
  - Consider OpenAI Realtime for lower latency
  - Check network connectivity to cloud LLM

Detailed logs: /var/log/ai-engine/call-1763582071.6214.log
```

---

### `agent dialplan` - Generate Dialplan Snippets

Generate Asterisk dialplan configuration for a provider.

**Usage:**
```bash
agent dialplan [--provider <name>]
```

**Flags:**
- `--provider` - Provider name (openai_realtime, deepgram, local_hybrid, google_live)

**Example:**
```bash
$ agent dialplan --provider openai_realtime

Add this snippet to: /etc/asterisk/extensions_custom.conf

[from-ai-agent-openai]
exten => s,1,NoOp(AI Agent - OpenAI Realtime)
 same => n,Set(AI_PROVIDER=openai_realtime)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()

FreePBX Setup:
  1. Admin → Config Edit → extensions_custom.conf
  2. Paste snippet above
  3. Save and Apply Config
  4. Create Custom Destination: from-ai-agent-openai,s,1
```

---

### `agent config validate` - Configuration Validation

Validate `config/ai-agent.yaml` for errors.

**Usage:**
```bash
agent config validate [--file <path>] [--fix] [--strict]
```

**Flags:**
- `--file` - Path to config file (default: config/ai-agent.yaml)
- `--fix` - Attempt to auto-fix issues
- `--strict` - Treat warnings as errors

**Checks:**
- YAML syntax
- Required fields present
- Provider configurations valid
- Sample rate alignment
- Transport compatibility

**Example:**
```bash
$ agent config validate

Validating config/ai-agent.yaml...
✓ YAML syntax valid
✓ Required fields present
✓ Provider 'openai_realtime' enabled
✓ Sample rates aligned (24000 Hz)

Summary: 4 passed, 0 warnings, 0 errors
✅ Configuration valid
```

---

### `agent version` - Show Version

**Usage:**
```bash
agent version
```

**Output:**
```
Asterisk AI Voice Agent CLI
Version: 4.1.0
Build: 2025-11-19
Go: 1.21.0
```

---

## Common Workflows

### First-Time Setup
```bash
# 1. Run interactive setup
agent init

# 2. Validate installation
agent doctor

# 3. Test audio pipeline
agent demo

# 4. Generate dialplan snippet
agent dialplan

# 5. Make a test call
```

### Troubleshooting Issues
```bash
# 1. Run health check
agent doctor

# 2. Analyze recent call
agent troubleshoot

# 3. Validate configuration
agent config validate
```

### CI/CD Integration
```bash
#!/bin/bash
# Pre-deployment validation

agent config validate --strict || exit 1
agent doctor || exit 1

echo "✅ Validation passed - deploying..."
```

## Additional Resources

- **[TROUBLESHOOTING_GUIDE.md](../docs/TROUBLESHOOTING_GUIDE.md)** - General troubleshooting
- **[CHANGELOG.md](../CHANGELOG.md)** - CLI tools features and updates
- **[INSTALLATION.md](../docs/INSTALLATION.md)** - Full installation guide

## Development

### Project Structure

```
cli/
├── cmd/agent/           # Main CLI commands
│   ├── main.go          # Root command and app entry
│   ├── init.go          # Setup wizard
│   ├── doctor.go        # Health checks
│   ├── demo.go          # Audio validation
│   ├── troubleshoot.go  # Post-call analysis
│   └── version.go       # Version command
└── internal/            # Internal packages
    ├── wizard/          # Interactive setup wizard
    ├── health/          # Health check system
    ├── audio/           # Audio test utilities
    └── rca/             # Root cause analysis
```

### Dependencies

```bash
# Install dependencies
go mod download

# Update dependencies
go get -u ./...
go mod tidy
```

### Testing

```bash
# Run tests
go test ./...

# Run with coverage
go test -cover ./...
```

## Planned Features (v4.1)

- [ ] Automated binary builds (Makefile target)
- [ ] `agent config validate` - Pre-flight config validation
- [ ] `agent test` - Automated test call execution
- [ ] Windows support
- [ ] Shell completion (bash, zsh, fish)
- [ ] Package managers (apt, yum, brew)

## Exit Codes

Commands follow standard Unix exit code conventions:

- **0** - Success
- **1** - Warning (non-critical issues detected)
- **2** - Failure (critical issues detected)

Use in scripts:

```bash
#!/bin/bash
if ! ./bin/agent doctor; then
    echo "Health check failed - see output above"
    exit 1
fi
```

## Support

- **Documentation**: [docs/CLI_TOOLS_GUIDE.md](../docs/CLI_TOOLS_GUIDE.md)
- **Issues**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/issues
- **Discussions**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/discussions

## License

Same as parent project - see [LICENSE](../LICENSE)
