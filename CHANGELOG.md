# Changelog

All notable changes to the Asterisk AI Voice Agent project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Additional provider integrations
- Enhanced monitoring features

## [4.3.0] - 2025-11-19

### Added
- **Holistic Tool Support for Pipelines (AAVA-85)**: Complete tool execution system across all pipeline types
  - Enabled all 6 tools (hangup, transfer, email, transcript, voicemail) for `local_hybrid` pipeline
  - Session history persistence for tool context
  - Explicit ARI hangup implementation
- **Comprehensive Documentation Structure**: Complete reorganization of project documentation
  - New `docs/contributing/` structure for developer documentation
  - Provider setup guides: `Provider-Deepgram-Setup.md`, `Provider-OpenAI-Setup.md`, `Provider-Google-Setup.md`
  - Developer guides: quickstart, architecture overview, architecture deep dive, common pitfalls
  - Technical references for all provider implementations
- **Community Integration**: Discord server integration (https://discord.gg/CAVACtaY)
- **Milestone 18**: Hybrid Pipelines Tool Implementation documentation

### Fixed
- **OpenAI Realtime Tool Schema Regression**: Corrected tool schema format for chat completions
- **Tool Execution Flow**: Resolved AttributeError and execution blocking issues
- **Playback Race Conditions**: Fixed audio cutoff during tool execution
- **Hangup Method**: Corrected method name (`hangup_channel()` vs `delete_channel()`)
- **Pydantic Compatibility**: Fixed v1/v2 compatibility (`model_dump` → `dict`)
- **Milestone Numbering**: Corrected duplicate milestone-8, renumbered monitoring stack to milestone-14

### Changed
- **Documentation Structure**: Reorganized docs into User, Provider, Operations, Developer, and Project sections
- **Merged Documentation**: Combined Deepgram API reference into implementation guide (single comprehensive doc)
- **Consolidated Guides**: CLI tools → `cli/README.md`, Queue setup → FreePBX Integration Guide
- **Renamed Files**: Clearer naming for pipeline implementations and architecture docs
- **Link Format**: All documentation links now use relative paths (GitHub-clickable)

### Removed
- **Obsolete Documentation**: 8 outdated docs removed (2,763 lines)
  - `call-framework.md`, `AudioSocket-Provider-Alignment.md`, `CLI_TOOLS_GUIDE.md`
  - `LOCAL_AI_SERVER_LOGGING_OPTIMIZATION.md`, `ASTERISK_QUEUE_SETUP.md`
  - `ExternalMedia_Deployment_Guide.md`, `deepgram-agent-api.md`
- **Broken References**: Replaced `linear-issues-community-features.md` with Discord server

## [4.2.1] - 2025-11-18

### Added

#### Streamlined Onboarding Experience
- **🚀 Interactive Setup Wizard**: New `agent quickstart` command guides first-time users through complete setup
  - Step-by-step provider selection (OpenAI, Deepgram, Google, Local Hybrid)
  - Real-time API key validation before saving
  - Asterisk ARI connection testing
  - Automatic dialplan snippet generation
  - Clear next steps and FreePBX integration instructions
- **🔧 Enhanced install.sh**: Improved installer with CLI integration
  - ARI connection validation after credentials input
  - Shows Asterisk version on successful connection
  - Offers CLI tool installation with platform auto-detection
  - Launches `agent dialplan` helper if CLI installed
  - Graceful fallbacks for unsupported platforms or download failures
- **📝 Dialplan Generation Helper**: New `agent dialplan` command
  - Generates provider-specific dialplan snippets
  - Supports all providers: OpenAI Realtime, Deepgram, Google Live, Local Hybrid
  - Shows FreePBX Custom Destination setup steps
  - Includes context override examples (AI_PROVIDER, AI_CONTEXT variables)
  - Print-only approach (no auto-write to files)
- **✅ Configuration Validation**: New `agent config validate` command
  - Validates YAML syntax and structure
  - Checks required fields and provider configurations
  - Verifies sample rate alignment across providers
  - Validates transport compatibility
  - Checks barge-in configuration
  - `--fix` flag for interactive auto-fix
  - `--strict` mode for CI/CD (treats warnings as errors)
  - Exit codes: 0 (valid), 1 (warnings), 2 (errors)
- **🩺 Doctor Auto-Fix**: Enhanced `agent doctor` with `--fix` flag
  - Focuses on YAML config validation issues
  - Guides users to `agent config validate --fix` for detailed repairs
  - Re-runs health checks after fixes applied

#### API and Connection Validation
- **API Key Validation**: Real-time validation before saving credentials
  - OpenAI: Validates against `/v1/models` endpoint, checks for GPT models
  - Deepgram: Validates against `/v1/projects` endpoint
  - Google: Format validation (length check)
  - Clear error messages with troubleshooting guidance
  - Network timeout handling (10 second limit)
- **ARI Connection Testing**: Validates Asterisk connectivity during setup
  - Tests connection to `/ari/asterisk/info`
  - Extracts and displays Asterisk version
  - Shows troubleshooting steps on failure
  - Continues with warning if validation fails

#### Documentation
- **CLI Tools Guide**: Updated with all new v4.2 commands
  - Comprehensive `agent quickstart` reference with example session
  - `agent dialplan` usage and output examples
  - `agent config validate` with validation checks and flags
  - Version bumped to v4.2
- **README.md**: Updated Quick Start section
  - Two-path approach: Interactive Quickstart vs Manual Setup
  - Highlights new `agent quickstart` wizard
  - Shows new CLI commands (`dialplan`, `config validate --fix`)
  - Updated version references to v4.2
- **Developer Experience**: Enhanced setup documentation
  - Clear separation between first-time and advanced user paths
  - Better CLI tool discovery and installation guidance

### Fixed

#### OpenAI Realtime Provider
- **Hangup Tool Reliability**: Fixed issue where calls wouldn't hang up when OpenAI failed to generate farewell audio
  - Now emits `HangupReady` immediately when `response.done` arrives without audio
  - Eliminated reliance on timeout-only fallback mechanism
  - Ensures consistent call termination regardless of OpenAI audio generation
- **Self-Interruption Prevention**: Resolved agent overhearing itself and interrupting mid-response
  - Increased `post_tts_end_protection_ms` from 100ms to 800ms (8x longer guard window)
  - Tuned `turn_detection.threshold` from 0.5 to 0.6 (less sensitive to agent's own voice)
  - Increased `turn_detection.silence_duration_ms` from 600ms to 1000ms (more patient turn-taking)
  - Result: Clean, natural conversation flow without choppy interruptions
- **Greeting Timing**: Attempted optimization of `session.updated` ACK timeout (reverted due to audio issues)

#### Local Hybrid Pipeline
- **Critical Sample Rate Fix**: Resolved Vosk STT recognition failure
  - Changed `external_media.format` from `slin` to `slin16` and `sample_rate` from 8000 to 16000
  - Enabled RTP server resampling to match Vosk's native 16kHz requirement
  - Audio now correctly resampled: 8kHz μ-law → decode → 16kHz PCM16 → Vosk
  - Result: Clear two-way conversation with accurate transcription
- **Audio Flow Debugging**: Added comprehensive debug logging for troubleshooting
  - Traces audio bytes, RMS levels, sample counts through full pipeline
  - Helps diagnose future audio routing or quality issues

#### Logging Optimization
- **Production Log Volume**: Reduced local-ai-server log noise
  - Implemented `LOCAL_DEBUG` environment flag to gate verbose audio flow logs
  - Moved detailed audio processing logs (`FEEDING VOSK`, RMS calculation) behind debug flag
  - Preserved essential logs (STT finals, LLM results, TTS output, connection events)
  - Result: ~90% log volume reduction in production with `LOCAL_DEBUG=0`
- **Configuration Clarity**: Improved `.env.example` documentation
  - Clear section headers distinguishing ai-engine vs local-ai-server settings
  - Explicit warnings about log volume impact of debug flags
  - Better guidance on production vs development logging levels

### Added

#### Documentation
- **Local Hybrid Golden Baseline**: Complete production-validated configuration reference
  - Performance metrics, architecture, sample rate fix details
  - Call quality assessment and tuning recommendations
  - See `docs/case-studies/Local-Hybrid-Golden-Baseline.md`
- **Logging Optimization Guide**: Comprehensive logging strategy documentation
  - Debug flag usage, log volume comparison, configuration examples
  - See `docs/LOCAL_AI_SERVER_LOGGING_OPTIMIZATION.md`

#### Unified Transfer Tool (AAVA-63, AAVA-74)
- **Unified Transfer System**: Single `transfer` tool replaces separate `transfer_call` and `transfer_to_queue` tools
  - **Extension Transfers**: Direct dial to specific agents (ARI `redirect`, channel stays in Stasis)
  - **Queue Transfers**: Transfer to ACD queues for next available agent (ARI `continue` to `ext-queues`)
  - **Ring Group Transfers**: Transfer to ring groups that ring multiple agents simultaneously (ARI `continue` to `ext-group`)
- **Smart Routing**: Automatic routing based on destination type configuration
- **Proper Cleanup Handling**: `transfer_active` flag prevents premature caller hangup for queue/ring group transfers
- **Production Verified**: All three transfer types validated on live production server
- **Configuration**: Unified `tools.transfer.destinations` structure with type-based routing

#### Voicemail Tool (AAVA-51)
- **Voicemail Routing**: New `leave_voicemail` tool sends callers to voicemail
  - Routes to FreePBX voicemail via `ext-local,vmu{extension},1` dialplan pattern
  - Uses ARI `continue()` pattern consistent with queue/ring group transfers
  - `transfer_active` flag prevents premature caller hangup
  - Configurable voicemail box extension number
- **Interactive Prompt Strategy**: Tool asks "Are you ready to leave a message now?" to work around FreePBX VoiceMail app behavior
  - VoiceMail app requires bidirectional RTP and voice activity before playing greeting
  - Without caller interaction, 5-8 second delay occurs before greeting plays
  - Caller response establishes RTP path and triggers greeting immediately
- **Comprehensive Documentation**: Detailed behavioral analysis and timeline evidence in module docstring
- **Production Verified**: Tested and deployed on live production server

### Changed
- **Breaking**: Removed `transfer_call` and `transfer_to_queue` tools in favor of unified `transfer` tool
- **Configuration Migration**: Update from separate tool configs to unified `transfer.destinations` structure

## [4.2.0] - 2025-11-14

### 🚀 Major Feature: Google Live Provider (Real-Time Agent)

Version 4.2 introduces the **Google Live provider** - a real-time bidirectional streaming agent powered by Gemini 2.5 Flash with native audio capabilities. This provider delivers ultra-low latency (<1 second) and true duplex communication, making it the fastest option in the Asterisk AI Voice Agent.

### Added

#### Google Live Provider (AAVA-75)
- **Real-Time Bidirectional Streaming**: Full-duplex communication with Gemini 2.5 Flash
  - Native audio processing (no separate STT/TTS pipeline)
  - Ultra-low latency: <1 second response time
  - True duplex: Natural interruptions and barge-in
  - WebSocket-based streaming communication
- **Provider Implementation**: `src/providers/google_live.py`
  - WebSocket connection to Gemini Live API
  - Bidirectional audio streaming with automatic resampling
  - Native tool execution via Google function declarations
  - Session management with context retention
- **Tool Adapter**: `src/tools/adapters/google.py`
  - Converts tools to Google function declaration format
  - Handles async tool execution in streaming mode
  - Sends tool responses back to Gemini
- **Audio Processing**: Automatic resampling for telephony compatibility
  - Input: 8kHz μ-law → 16kHz PCM16 → Gemini
  - Output: 24kHz PCM16 from Gemini → 8kHz μ-law → Asterisk
- **Configurable Parameters**: Full YAML configuration support
  - LLM generation parameters (temperature, max_output_tokens, top_p, top_k)
  - Response modalities (audio, text, audio_text)
  - Transcription toggles (enable_input_transcription, enable_output_transcription)
  - Voice selection (Aoede, Kore, Leda, Puck, Charon, etc.)
- **Golden Baseline**: Validated production-ready configuration
  - See `docs/GOOGLE_LIVE_GOLDEN_BASELINE.md` for complete reference
  - Call quality: Excellent, clean two-way conversation
  - Response latency: <1 second (fastest available)
  - All features validated: duplex, barge-in, tools, transcriptions

#### Transcription System (AAVA-75)
- **Dual Transcription Support**: User and AI speech transcription
  - `inputTranscription`: Captures user speech
  - `outputTranscription`: Captures AI speech
  - Turn-complete based: Saves only final utterances
  - Incremental fragment concatenation for complete transcripts
- **Email Summary Integration**: Complete conversation history in emails
  - Auto-triggered email summaries at call end
  - Manual transcript requests via `request_transcript` tool
  - Transcripts include both user and AI speech
- **Conversation History**: Full conversation tracking
  - Stored in session for context retention
  - Available for email summaries and transcript requests
  - Proper turn management with `turnComplete` flag

### Fixed

#### Transcript Email Timing (CRITICAL)
- **Issue**: `request_transcript` tool sent email immediately (mid-call), missing final conversation
- **Fix**: Defer transcript sending until call end
  - Store email address in session during call
  - Send complete transcript at call cleanup with full conversation history
  - Prevents incomplete transcripts missing final exchanges
- **Impact**: Transcripts now include complete conversation including goodbye

#### Call Ending Protocol
- **Issue**: AI didn't hang up calls after completing tasks, leaving silence
- **Fix**: Explicit call ending protocol in system prompts
  - Step-by-step protocol for detecting conversation end
  - "Is there anything else?" prompt after completing tasks
  - Immediate `hangup_call` tool execution on confirmation
  - Never leave calls hanging in silence
- **Impact**: Professional call termination, no manual hangup needed

#### Greeting Implementation
- **Issue**: Cannot pre-fill model responses in Gemini Live API
- **Fix**: Send user turn requesting AI to speak greeting
  - Changed from pre-filled model response to user request
  - AI generates and speaks personalized greeting naturally
  - Properly uses caller name in greeting
- **Impact**: Greetings now work correctly with caller personalization

#### Incremental Transcription Handling
- **Issue**: API sends word-by-word fragments, not cumulative text
- **Fix**: Concatenate fragments instead of replacing buffer
  - Buffer accumulates fragments until `turnComplete`
  - Prevents fragmented/incomplete transcriptions
  - Matches actual API behavior (differs from documentation)
- **Impact**: Complete, clean transcriptions of all speech

### Changed
- **Documentation**: Renamed `docs/GOOGLE_CLOUD_SETUP.md` → `docs/GOOGLE_PROVIDER_SETUP.md`
  - Updated to cover both Google Live and Cloud Pipeline modes
  - Added comprehensive setup instructions for both
  - Separate dialplan examples for each mode
- **Configuration Examples**: Updated `config/ai-agent.yaml`
  - Added `demo_google_live` context with full configuration
  - Includes all new configurable parameters with inline docs
  - Clear call ending protocol in system prompts

### Performance
- **Latency**: <1 second response time (fastest provider)
- **Audio Quality**: Excellent, natural conversation flow
- **Duplex Communication**: True full-duplex with seamless interruptions
- **Reliability**: Production-tested with clean call termination

### Lessons Learned
- Trust API turn completion signals over custom heuristics
- API behavior may differ from documentation - always validate with testing
- Defer email sending until call end for complete transcripts
- Be explicit about call ending protocols in system prompts
- Provide maximum user flexibility via YAML configuration

## [4.0.0] - 2025-10-29

### 🎉 Major Release: Modular Pipeline Architecture

Version 4.0 introduces a **production-ready modular pipeline architecture** that enables flexible combinations of Speech-to-Text (STT), Large Language Models (LLM), and Text-to-Speech (TTS) providers. This release represents a complete architectural evolution while maintaining backward compatibility with existing deployments.

### Added

#### Core Architecture
- **Modular Pipeline System**: Mix and match STT, LLM, and TTS providers
  - Local STT (Vosk) + Cloud LLM (OpenAI) + Local TTS (Piper)
  - Cloud STT (Deepgram) + Cloud LLM (OpenAI) + Cloud TTS (Deepgram)
  - Fully local pipeline (Vosk + Phi-3/Llama + Piper)
- **Unified Configuration Format**: Single YAML file for all pipeline and provider settings
- **Golden Baseline Configurations**: Three validated, production-ready configurations:
  - **OpenAI Realtime**: Cloud monolithic agent (fastest, <2s response)
  - **Deepgram Voice Agent**: Enterprise cloud agent with Think stage
  - **Local Hybrid**: Privacy-focused with local STT/TTS + cloud LLM

#### Audio Transport
- **Dual Transport Support**: AudioSocket (TCP) and ExternalMedia RTP (UDP)
- **Automatic Transport Selection**: Optimal transport chosen per configuration
- **Enhanced Audio Processing**: Improved resampling, echo cancellation, and codec handling
- **Pipeline Audio Routing**: Fixed audio path for pipeline configurations
- **Transport Compatibility Matrix**: Documented all configuration + transport combinations

#### Monitoring & Observability
- **Production Monitoring Stack**: Prometheus + Grafana with 5 pre-built dashboards
  - System Overview: Active calls, provider distribution
  - Call Quality: Turn latency (p50/p95/p99), processing time
  - Audio Quality: RMS levels, underflows, jitter buffer depth
  - Provider Performance: Provider-specific metrics and health
  - Barge-In Analysis: Interrupt behavior and timing
- **50+ Metrics**: Comprehensive call quality, audio quality, and system health metrics
- **Alert Rules**: Critical and warning alerts for production monitoring
- **Health Endpoint**: `/metrics` endpoint on port 15000 for Prometheus scraping

#### Installation & Setup
- **Interactive Installer**: `install.sh` with guided pipeline selection
  - Choose from 3 golden baseline configurations
  - Automatic dependency setup per configuration
  - Model downloads for local pipelines
  - Environment validation and configuration
- **Two-File Configuration Model**: 
  - `.env` for secrets and credentials (gitignored)
  - `config/ai-agent.yaml` for pipeline definitions (committed)
- **Streamlined User Journey**: From clone to first call in <15 minutes

#### Documentation
- **FreePBX Integration Guide**: Complete v4.0 guide with channel variables
  - `AI_CONTEXT`: Department/call-type specific routing
  - `AI_GREETING`: Per-call greeting customization
  - `AI_PERSONA`: Dynamic persona switching
  - Remote deployment configurations (NFS, Docker, Kubernetes)
  - Network and shared storage setup for distributed deployments
- **Configuration Reference**: Comprehensive YAML parameter documentation
- **Transport Compatibility Guide**: Validated configuration + transport combinations
- **Golden Baseline Case Studies**: Detailed performance analysis and tuning guides
- **Inline YAML Documentation**: Comprehensive comments with ranges and impacts

#### Developer Experience
- **CLI Tools**: Go-based `agent` command with 5 subcommands
  - `agent init`: Interactive setup wizard
  - `agent doctor`: Health diagnostics and validation
  - `agent demo`: Demo call functionality
  - `agent troubleshoot`: Interactive troubleshooting assistant
  - `agent version`: Version and build information
- **Enhanced Logging**: Structured logging with context and call tracking
- **RCA Tools**: Root cause analysis scripts for audio quality debugging
- **Test Infrastructure**: Baseline validation and regression testing
- **IDE Integration**: Full development context preserved in develop branch

### Changed

#### Configuration
- **YAML Structure**: Streamlined provider configuration format
- **Settings Consolidation**: Removed unused/duplicate settings (`llm.model`, `external_media.jitter_buffer_ms`)
- **downstream_mode Enforcement**: Now properly gates streaming vs file playback
- **Security Model**: Credentials **ONLY** in `.env`, never in YAML files

#### Audio Processing
- **VAD Configuration**: Optimized Voice Activity Detection for each provider
  - OpenAI Realtime: `webrtc_aggressiveness: 1` (balanced mode)
  - Server-side VAD support for providers that offer it
- **Barge-In System**: Enhanced interrupt detection with configurable thresholds
- **Audio Routing**: Fixed pipeline audio routing for AudioSocket and RTP transports

#### Performance
- **Response Times**: Validated response times for all golden baselines:
  - OpenAI Realtime: 0.5-1.5s typical
  - Deepgram Hybrid: <3s typical
  - Local Hybrid: 3-7s depending on hardware
- **Echo Cancellation**: Improved echo filtering with SSRC-based detection
- **Jitter Buffer**: Optimized buffer management for streaming playback

### Fixed

- **AudioSocket Pipeline Audio**: Fixed audio routing to STT adapters in pipeline mode
- **RTP Echo Loop**: Added SSRC-based filtering to prevent echo feedback
- **Provider Bytes Tracking**: Corrected audio chunk accounting for accurate pacing
- **Normalizer Consistency**: Fixed audio normalization for consistent output
- **Configuration Loading**: Ensured all config values properly honored at runtime
- **Sample Rate Handling**: Fixed provider-specific sample rate overrides

### Deprecated

- **Legacy YAML Templates**: Replaced with 3 golden baseline configurations
  - `ai-agent.openai-agent.yaml` → `ai-agent.golden-openai.yaml`
  - `ai-agent.deepgram-agent.yaml` → `ai-agent.golden-deepgram.yaml`
  - `ai-agent.hybrid.yaml` → `ai-agent.golden-local-hybrid.yaml`
- **Development Artifacts**: Moved to `archived/` folder (not tracked in git)

### Technical Details

#### System Requirements
- **Minimum**: 4 CPU cores, 8GB RAM (cloud configurations)
- **Recommended**: 8+ CPU cores, 16GB RAM (local pipelines)
- **GPU**: Optional for local-ai-server (improves LLM performance)

#### Compatibility
- **Asterisk**: 18+ required (for AudioSocket support)
- **FreePBX**: 15+ recommended
- **Python**: 3.10+
- **Docker**: 20.10+
- **Docker Compose**: 2.0+

#### Breaking Changes
**None** - This release maintains backward compatibility with existing deployments. Users can continue using existing configurations while adopting new features incrementally.

### Migration Guide

**No migration needed** - This is the first production release. There are no users on v3.0 requiring migration.

For new deployments:
1. Clone repository
2. Run `./install.sh` and select a golden baseline
3. Configure `.env` with your credentials
4. Deploy with `docker compose up -d`
5. Follow the FreePBX Integration Guide to configure Asterisk

### Contributors

- Haider Jarral (@hkjarral) - Architecture, implementation, documentation

### Links

- **Repository**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent
- **Documentation**: [docs/README.md](docs/README.md)
- **FreePBX Guide**: [docs/FreePBX-Integration-Guide.md](docs/FreePBX-Integration-Guide.md)
- **Monitoring**: [monitoring/README.md](monitoring/README.md)

---

## [4.1.0] - 2025-11-10

### 🎉 Tool Calling & Agent CLI Release

Version 4.1 introduces **unified tool calling architecture** enabling AI agents to perform actions like call transfers and email management, plus production-ready **Agent CLI tools** for operations.

### Added

#### Tool Calling System
- **Unified Tool Architecture**: Write tools once, use with any provider
  - Base classes: `Tool`, `ToolDefinition`, `ToolRegistry` (`src/tools/base.py`, 231 lines)
  - Execution context with session and ARI access (`src/tools/context.py`, 108 lines)
  - Singleton registry for tool management (`src/tools/registry.py`, 198 lines)
  - Provider adapters for Deepgram (202 lines) and OpenAI Realtime (215 lines)

#### Telephony Tools
- **Transfer Call Tool**: Warm and blind transfers with direct SIP origination
  - `src/tools/telephony/transfer.py` (504 lines)
  - Department name resolution (e.g., "support" → extension 6000)
  - Warm transfer: AI stays on line until agent answers
  - Blind transfer: Immediate redirect
  - Production validated: <150ms execution time
  - Call IDs: `1762731796.4233` (Deepgram), `1762734947.4251` (OpenAI)
- **Cancel Transfer Tool**: Cancel in-progress transfer before agent answers
  - `src/tools/telephony/cancel_transfer.py`
  - Allows caller to change mind during ring
- **Hangup Call Tool**: Graceful call termination with farewell message
  - `src/tools/telephony/hangup.py`
  - Customizable farewell message
  - Works with all providers

#### Email Tools
- **Request Transcript Tool**: Caller-initiated transcript delivery
  - `src/tools/business/request_transcript.py` (475 lines)
  - Email parsing from speech ("john dot smith at gmail dot com")
  - Domain validation via DNS MX record lookup
  - Confirmation flow (AI reads back email for verification)
  - Deduplication (prevents sending same email multiple times)
  - Admin receives BCC on all transcript requests
  - Resend API integration
- **Send Email Summary Tool**: Auto-send call summaries to admin
  - `src/tools/business/email_summary.py` (347 lines)
  - Triggered automatically after every call
  - Full conversation transcript with timestamps
  - Call metadata (duration, caller ID, date/time)
  - Professional HTML formatting
  - Admin email configuration in YAML

#### Agent CLI Tools
- **Binary Distribution System**:
  - Makefile build system for 5 platforms (Linux, macOS, Windows)
  - GitHub Actions CI/CD for automated releases (`.github/workflows/release-cli.yml`)
  - One-line installer: `curl -sSL ... | bash` (`scripts/install-cli.sh`, 223 lines)
  - SHA256 checksums for security verification
  - Automated binary uploads to GitHub releases
- **CLI Commands**:
  - `agent doctor`: System health checks and validation
  - `agent troubleshoot`: Call analysis and debugging
  - `agent demo`: Feature demonstrations
  - `agent init`: Interactive setup wizard
  - `agent version`: Build and version information
- **Platform Support**:
  - Linux AMD64/ARM64 (servers, Raspberry Pi, AWS Graviton)
  - macOS AMD64/ARM64 (Intel Macs and Apple Silicon M1/M2/M3)
  - Windows AMD64
  - Pre-built binaries with automatic platform detection

#### Conversation Tracking
- **Real-time Tracking**: Both Deepgram and OpenAI Realtime track conversation turns
  - `conversation_history` field in `CallSession` model
  - Tracks role (user/assistant), content, and timestamps
  - Enables email tools to include full transcripts
  - Pattern identical across providers (46-51 lines each)

### Improved

#### Warm Transfer Implementation
- **Direct SIP Endpoint Origination**: Eliminates Local channel complexity
  - Previous: Used `Local/{ext}@{context}/n` → caused unidirectional audio
  - Current: Direct SIP origination (e.g., `SIP/6000`)
  - Result: Perfect bidirectional audio confirmed
  - No Local channels created (verified in production)
- **4-Step Cleanup Sequence**:
  1. Remove AI channel from bridge (<50ms)
  2. Stop provider session gracefully (<30ms)
  3. Add agent SIP channel to bridge (<20ms)
  4. Update session metadata (<10ms)
  - Total: <150ms for complete transfer execution
- **Production Validation**:
  - Call duration: 38+ seconds after transfer (stable)
  - Bridge type: simple_bridge (optimal, 2 channels only)
  - Audio path: Direct (1 hop, minimal latency)
  - Files: `src/tools/telephony/transfer.py`, `src/engine.py` (transfer handler)

#### OpenAI Realtime Stability
- **VAD Re-enable Timing Fix**: Correct event for greeting protection
  - Issue: Used `response.audio.done` (fires per segment) → VAD enabled too early
  - Fix: Changed to `response.done` (fires when complete response generated)
  - Impact: Greeting now plays completely before accepting interruptions
  - Lines: `src/providers/openai_realtime.py` (1206-1219)
- **API Modality Constraints**: Documented OpenAI requirements
  - Supported: `["text"]` or `["audio", "text"]`
  - Not supported: `["audio"]` alone (API rejects)
  - Known limitation: May occasionally generate text-only responses
  - Mitigation: System handles gracefully with keepalive messages
- **Race Condition Handling**: Handles variable event arrival order
  - Sometimes: `response.done` arrives before audio deltas
  - Other times: Audio deltas arrive first
  - Solution: Check `had_audio_burst` flag, re-enable VAD regardless

### Fixed

- **AAVA-57**: Direct SIP endpoint origination for warm transfers
  - Root cause: Local channels caused audio direction mismatch
  - Solution: Direct `SIP/extension` origination
  - Evidence: Call logs show no Local channels, perfect audio
- **AAVA-58**: Local channel audio direction issue (RCA documented)
  - Symptom: Caller heard agent, but agent couldn't hear caller
  - Root cause: Audio path `caller → Local;2 → Local;1 → nowhere`
  - Solution: Eliminated Local channels entirely
- **AAVA-59**: AI provider cleanup during transfer
  - Issue: AI stayed in bridge after agent answered
  - Solution: Remove external media channel before adding agent
  - Result: Clean 2-channel bridge (caller + agent only)
- **AAVA-62**: OpenAI Realtime audio generation analysis and constraints
  - Issue #1: Greeting interrupted (VAD enabled too early)
  - Issue #2: 45-second silence (OpenAI generated text-only)
  - Solution: Correct VAD timing + documented API limitation
  - Commits: `85c4235`, `80efdcd`, `6dbd51e`
- **AAVA-52**: Email tools race conditions and missing await
  - Bug #1: `context.get_session()` called without `await`
  - Bug #2: Auto-summary triggered async, session removed first
  - Bug #3: Undefined `caller_id` variable
  - Bug #4: No email confirmation flow
  - Bug #5: Duplicate emails when caller corrected address
  - Commits: `1deed05`, `700993f`, `5579ddd`, `a2d9409`, `835ac05`

### Documentation

- **New Guides**:
  - `docs/TOOL_CALLING_GUIDE.md` - Comprehensive tool calling documentation
    - Overview and supported providers
    - All 5 tools with example conversations
    - Configuration details with option explanations
    - Dialplan setup requirements
    - Testing procedures
    - Production examples with evidence
    - Troubleshooting section
    - Architecture diagrams
  - Tool sections added to `docs/FreePBX-Integration-Guide.md`
  - Enhanced `docs/CLI_TOOLS_GUIDE.md` with binary installation
- **Updated**:
  - README with v4.1 features and tool examples
  - Architecture.md with tool calling section
  - Configuration comments in `config/ai-agent.yaml`
  - SECURITY.md with v4.1 support

### Known Limitations

- **OpenAI Realtime**: May occasionally generate text-only responses
  - Root cause: API limitation with `["audio", "text"]` modalities
  - Frequency: Varies (test call had 2/4 responses without audio)
  - Impact: Caller experiences brief silence
  - Mitigation: System handles gracefully with keepalive messages
  - Not fixable: Cannot force OpenAI to always generate audio
- **Tool Calling**: Currently Deepgram and OpenAI Realtime only
  - Custom Pipeline support planned for v4.3 (AAVA-56)
  - Other providers: Anthropic Claude, Google Gemini (v4.3+)

### Architecture Validation

**Provider-Agnostic Design Confirmed**:
- Same tool code (504 lines for transfer) works with both providers
- Only adapters differ (202 lines Deepgram, 215 lines OpenAI)
- Zero code duplication in tool logic
- Adding new providers requires <250 lines of adapter code

**Line Counts**:
- Tool calling framework: 537 lines (base + context + registry)
- Transfer call tool: 504 lines (shared by all providers)
- Email summary tool: 347 lines (shared)
- Request transcript tool: 475 lines (shared)
- Deepgram adapter: 202 lines
- OpenAI adapter: 215 lines
- **Total duplication**: 0 lines ✅

### Performance Metrics

**Transfer Tool**:
- Transfer execution: <150ms
- AI cleanup time: <100ms
- Bridge technology: simple_bridge (optimal)
- Audio path: Direct (1 hop)
- Call stability: 38+ seconds validated

**Email Tools**:
- Email validation: <100ms (DNS MX lookup)
- Email delivery: ~200ms (Resend API)
- Conversation tracking: Real-time (no performance impact)

### Contributors

- Haider Jarral (@hkjarral) - Tool architecture, transfers, email tools, CLI tools, documentation

### Links

- **Repository**: https://github.com/hkjarral/Asterisk-AI-Voice-Agent
- **Tool Calling Guide**: [docs/TOOL_CALLING_GUIDE.md](docs/TOOL_CALLING_GUIDE.md)
- **FreePBX Guide**: [docs/FreePBX-Integration-Guide.md](docs/FreePBX-Integration-Guide.md)
- **CLI Tools Guide**: [docs/CLI_TOOLS_GUIDE.md](docs/CLI_TOOLS_GUIDE.md)

---

## [Unreleased]

### Planned for v4.2
- **Additional Provider Integrations**: Anthropic Claude, Google Gemini
- **Custom Pipeline Tool Support**: Tool calling for local pipelines (AAVA-56)
- **WebRTC Support**: SIP client integration
- **High Availability**: Clustering and load balancing

---

## Version History

- **v4.0.0** (2025-10-29) - Modular pipeline architecture, production monitoring, golden baselines
- **v3.0.0** (2025-09-16) - Modular pipeline architecture, file based playback
- **v2.0.0** - Internal development version (never released)
- **v1.0.0** - Initial concept (never released)

[4.0.0]: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/releases/tag/v4.0.0
[Unreleased]: https://github.com/hkjarral/Asterisk-AI-Voice-Agent/compare/v4.0.0...HEAD
