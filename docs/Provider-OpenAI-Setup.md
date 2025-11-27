# OpenAI Realtime Provider Setup Guide

## Overview

OpenAI Realtime API provides low-latency bidirectional streaming conversational AI powered by GPT-4o-realtime. Ideal for natural voice interactions with built-in speech-to-speech capabilities and native tool execution.

**Performance**: 0.5-1.5 second response latency | Full duplex | Server-side echo cancellation

## Quick Start

### 1. Get OpenAI API Key

1. Sign up at [OpenAI Platform](https://platform.openai.com/)
2. Navigate to **API Keys**
3. Create a new API key
4. Copy your API key

**Note**: OpenAI Realtime API requires a paid account with Realtime API access enabled.

### 2. Configure API Key

Add your OpenAI API key to `.env`:

```bash
# OpenAI (required for openai_realtime provider and local_hybrid pipeline)
OPENAI_API_KEY=your_api_key_here
```

**Test API Key**:
```bash
curl -X GET "https://api.openai.com/v1/models" \
  -H "Authorization: Bearer ${OPENAI_API_KEY}"
```

### 3. Configure Provider

The OpenAI Realtime provider is configured in `config/ai-agent.yaml`:

```yaml
providers:
  openai_realtime:
    api_key: ${OPENAI_API_KEY}
    enabled: true
    greeting: "Hi {caller_name}, I'm your AI assistant. How can I help you today?"
    
    # Model Configuration
    model: gpt-4o-realtime-preview-2024-12-17  # Latest Realtime model
    temperature: 0.8                            # Creativity (0.0-1.0)
    max_response_output_tokens: 4096            # Max output length
    
    # Voice Configuration
    voice: shimmer                              # Options: alloy, echo, shimmer, coral, sage, ash, verse
    
    # Audio Configuration
    input_audio_format: pcm16                   # Raw PCM for telephony
    output_audio_format: pcm16
    input_audio_sample_rate: 24000              # 24kHz for best quality
    output_audio_sample_rate: 24000
    
    # Modalities
    modalities: ["text", "audio"]               # REQUIRED: Both text and audio
    
    # Turn Detection (VAD)
    turn_detection_enabled: true
    turn_detection_type: server_vad             # Use OpenAI's server-side VAD
```

**Key Settings**:
- `model`: Use latest `gpt-4o-realtime-preview` model
- `voice`: Choose from 7 available voices (shimmer recommended for female, alloy for male)
- `modalities`: **MUST include both "text" and "audio"** for Realtime to work
- `turn_detection_type`: Use `server_vad` for best results

### 4. Critical VAD Configuration ⚠️

**REQUIRED FOR PRODUCTION**: Set VAD aggressiveness to prevent echo detection issues.

In `config/ai-agent.yaml`:

```yaml
vad:
  webrtc_aggressiveness: 1  # CRITICAL: Must be 1 for OpenAI Realtime
  enhanced_enabled: true
  confidence_threshold: 0.6
  energy_threshold: 1500
```

**Why This Matters**:
- `webrtc_aggressiveness: 0` is too sensitive and detects echo as speech
- This causes audio gate to flutter, letting echo leak through
- OpenAI's server-side echo cancellation works best when local VAD is level 1
- Level 1 ignores echo but detects real speech

**Symptoms of Wrong VAD Setting**:
- AI interrupts itself mid-sentence
- Echo heard during conversation
- Log shows 50+ gate closures per call (should be 1-2)
- Self-interruption loop

**See**: `docs/case-studies/OpenAI-Realtime-Golden-Baseline.md` for validated configuration

### 5. Configure Asterisk Dialplan

Add to `/etc/asterisk/extensions_custom.conf`:

```ini
[from-ai-agent-openai]
exten => s,1,NoOp(AI Voice Agent - OpenAI Realtime)
exten => s,n,Set(AI_CONTEXT=demo_openai)
exten => s,n,Set(AI_PROVIDER=openai_realtime)
exten => s,n,Stasis(asterisk-ai-voice-agent)
exten => s,n,Hangup()
```

**CRITICAL**: Both `AI_CONTEXT` and `AI_PROVIDER` must be set:
- `AI_CONTEXT` - Selects the context (greeting, prompt, profile)
- `AI_PROVIDER` - Must be `openai_realtime`

### 6. Reload Asterisk

```bash
asterisk -rx "dialplan reload"
```

### 7. Create FreePBX Custom Destination

1. Navigate to **Admin → Custom Destinations**
2. Click **Add Custom Destination**
3. Set:
   - **Target**: `from-ai-agent-openai,s,1`
   - **Description**: `OpenAI Realtime AI Agent`
4. Save and Apply Config

### 8. Test Call

Route a test call to the custom destination and verify:
- ✅ Greeting plays within 1 second
- ✅ AI responds naturally to questions
- ✅ Can interrupt AI mid-sentence (barge-in)
- ✅ No echo or self-interruption
- ✅ Tools execute if configured

## Context Configuration

Define your AI's behavior in `config/ai-agent.yaml`:

```yaml
contexts:
  demo_openai:
    greeting: "Hi {caller_name}, I'm your AI assistant. How can I help you today?"
    profile: telephony_pcm16_24k
    prompt: |
      You are a helpful AI assistant for {company_name}.
      
      Your role is to assist callers professionally and efficiently.
      
      CONVERSATION STYLE:
      - Be warm, professional, and concise
      - Use natural language without robotic phrases
      - Answer questions directly and clearly
      - Confirm important actions before executing
      
      CALL ENDING PROTOCOL:
      1. When user says goodbye → ask "Is there anything else I can help with?"
      2. If user confirms done → give brief farewell + IMMEDIATELY call hangup_call tool
      3. NEVER leave silence - always explicitly end the call
      
      TOOL USAGE:
      - Use transfer tool to route calls to appropriate departments
      - Use email tools when caller requests information sent to them
      - Always confirm before executing tools that affect the call
```

**Template Variables**:
- `{caller_name}` - Caller ID name
- `{caller_number}` - Caller phone number
- `{company_name}` - Your company name (set in config)

## Tool Configuration

Enable tools for OpenAI Realtime in `config/ai-agent.yaml`:

```yaml
providers:
  openai_realtime:
    tools:
      - transfer              # Transfer calls to extensions/queues
      - cancel_transfer       # Cancel an active transfer
      - hangup_call           # End call with farewell
      - leave_voicemail       # Send caller to voicemail
      - send_email_summary    # Auto-send call summary
      - request_transcript    # Email transcript on request
```

**Tool Execution**: OpenAI Realtime natively supports function calling. Tools are executed automatically when the AI decides to use them based on conversation context.

## Troubleshooting

### Issue: "Echo / Self-Interruption"

**Cause**: VAD aggressiveness set too low

**Fix**:
```yaml
vad:
  webrtc_aggressiveness: 1  # MUST be 1, not 0
```

**Verification**: Check logs for gate closures - should be 1-2 per call, not 50+

### Issue: "Tools Not Working"

**Cause**: Schema format mismatch (post-AAVA-85 regression)

**Fix**: Verify you're on latest version. Tool registry now uses `to_openai_realtime_schema()` (flat format), not `to_openai_schema()` (nested format).

**Logs to Check**:
- ✅ "OpenAI session configured with N tools"
- ❌ "Missing required parameter: 'session.tools[0].name'"

**See**: `docs/contributing/COMMON_PITFALLS.md#tool-execution-issues`

### Issue: "No Audio" or "Silence"

**Cause**: Modalities not set correctly

**Fix**:
```yaml
providers:
  openai_realtime:
    modalities: ["text", "audio"]  # BOTH required
```

### Issue: "High Latency" (>2 seconds)

**Cause**: Network latency or model selection

**Fix**:
1. Check network: `ping api.openai.com`
2. Verify using latest realtime model
3. Check OpenAI status: https://status.openai.com/

### Issue: "AI Doesn't Respond"

**Cause**: VAD not detecting speech or turn detection issues

**Fix**:
```yaml
providers:
  openai_realtime:
    turn_detection_enabled: true
    turn_detection_type: server_vad  # Use server-side detection
```

## Production Considerations

### API Key Management
- Rotate keys every 90 days
- Use separate keys for dev/staging/production
- Monitor usage in OpenAI Dashboard
- Set spending limits to prevent overages

### Cost Optimization
- OpenAI Realtime charges per audio minute + token usage
- Monitor concurrent calls to manage costs
- Consider usage limits for high-volume scenarios
- Audio: ~$0.06/minute input, ~$0.24/minute output
- Tokens: Additional LLM costs for text processing

### Monitoring
- Track response latency in logs
- Monitor OpenAI API status: https://status.openai.com/
- Set up alerts for API errors or high latency
- Watch for rate limiting (500 requests/day default)

### Rate Limits
- Realtime API has lower rate limits than standard API
- Default: 500 requests/day, 100 concurrent sessions
- Request increase through OpenAI if needed
- Implement queuing for high-volume deployments

## See Also

- **Implementation Details**: `docs/contributing/references/Provider-OpenAI-Implementation.md`
- **Golden Baseline**: `docs/case-studies/OpenAI-Realtime-Golden-Baseline.md`
- **Common Pitfalls**: `docs/contributing/COMMON_PITFALLS.md`
- **Tool Calling Guide**: `docs/TOOL_CALLING_GUIDE.md`
- **VAD Configuration**: Critical setting documented in golden baseline

---

**OpenAI Realtime Provider Setup - Complete** ✅

For questions or issues, see the [GitHub repository](https://github.com/hkjarral/Asterisk-AI-Voice-Agent).
