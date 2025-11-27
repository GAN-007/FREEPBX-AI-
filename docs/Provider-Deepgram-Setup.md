# Deepgram Provider Setup Guide

## Overview

Deepgram Voice Agent is a monolithic real-time conversational AI provider that combines speech-to-text, LLM reasoning, and text-to-speech in a single streaming API. Ideal for low-latency telephony applications with built-in function calling support.

**Performance**: 1-2 second response latency | Full duplex | Native tool execution

## Quick Start

### 1. Get Deepgram API Key

1. Sign up at [Deepgram Console](https://console.deepgram.com/)
2. Navigate to **API Keys**
3. Create a new API key with Voice Agent access
4. Copy your API key

### 2. Configure API Key

Add your Deepgram API key to `.env`:

```bash
# Deepgram Voice Agent (required for deepgram provider)
DEEPGRAM_API_KEY=your_api_key_here
```

**Test API Key**:
```bash
curl -X GET "https://api.deepgram.com/v1/projects" \
  -H "Authorization: Token ${DEEPGRAM_API_KEY}"
```

### 3. Configure Provider

The Deepgram provider is configured in `config/ai-agent.yaml`:

```yaml
providers:
  deepgram:
    api_key: ${DEEPGRAM_API_KEY}
    enabled: true
    greeting: "Hi {caller_name}, I'm Ava. How can I help you today?"
    
    # LLM Configuration
    llm_model: nova-2-conversationalai  # Recommended for telephony
    llm_temperature: 1.0                # Natural conversation (0.0-1.0)
    
    # Voice Configuration
    tts_voice: aura-asteria-en          # Natural female voice
    # Options: aura-asteria-en, aura-luna-en, aura-stella-en, aura-athena-en
    
    # Audio Configuration
    encoding: mulaw                     # Telephony standard
    sample_rate: 8000                   # 8kHz for telephony
    enable_endpoint: true               # Enable turn detection
    
    # Conversation Settings
    context_handling: extended          # Maintain full conversation context
    interim_results: true               # Show real-time transcription
```

**Key Settings**:
- `llm_model`: `nova-2-conversationalai` (best for telephony) or `nova-2` (general purpose)
- `tts_voice`: Choose from Aura voices (asteria, luna, stella, athena)
- `encoding`: `mulaw` for telephony, `linear16` for higher quality
- `sample_rate`: `8000` for telephony, `16000` for higher quality

### 4. Configure Asterisk Dialplan

Add to `/etc/asterisk/extensions_custom.conf`:

```ini
[from-ai-agent-deepgram]
exten => s,1,NoOp(AI Voice Agent - Deepgram)
exten => s,n,Set(AI_CONTEXT=demo_deepgram)
exten => s,n,Set(AI_PROVIDER=deepgram)
exten => s,n,Stasis(asterisk-ai-voice-agent)
exten => s,n,Hangup()
```

**CRITICAL**: Both `AI_CONTEXT` and `AI_PROVIDER` must be set:
- `AI_CONTEXT` - Selects the context (greeting, prompt, profile)
- `AI_PROVIDER` - Must be `deepgram`

### 5. Reload Asterisk

```bash
asterisk -rx "dialplan reload"
```

### 6. Create FreePBX Custom Destination

1. Navigate to **Admin → Custom Destinations**
2. Click **Add Custom Destination**
3. Set:
   - **Target**: `from-ai-agent-deepgram,s,1`
   - **Description**: `Deepgram AI Agent`
4. Save and Apply Config

### 7. Test Call

Route a test call to the custom destination and verify:
- ✅ Greeting plays within 1-2 seconds
- ✅ AI responds to your questions naturally
- ✅ Duplex communication (can interrupt AI)
- ✅ Tools execute if configured (transfer, email, etc.)

## Context Configuration

Define your AI's behavior in `config/ai-agent.yaml`:

```yaml
contexts:
  demo_deepgram:
    greeting: "Hi {caller_name}, I'm Ava. How can I help you today?"
    profile: telephony_ulaw_8k
    prompt: |
      You are Ava, a helpful AI assistant for {company_name}.
      
      Your role is to assist callers with inquiries and route calls as needed.
      
      CONVERSATION STYLE:
      - Be friendly, professional, and concise
      - Speak naturally without filler words
      - Answer questions directly and clearly
      - Confirm user requests before executing tools
      
      CALL ENDING PROTOCOL:
      1. When user indicates they're done → ask "Is there anything else?"
      2. If user confirms done → say brief farewell + IMMEDIATELY call hangup_call
      3. NEVER leave call hanging in silence
      
      TOOL USAGE:
      - Use transfer tool to send callers to appropriate departments
      - Use email tools when caller requests transcript or summary
      - Always confirm actions with user before executing
```

**Template Variables**:
- `{caller_name}` - Caller ID name
- `{caller_number}` - Caller phone number
- `{company_name}` - Your company name (set in config)

## Tool Configuration

Enable tools for Deepgram in `config/ai-agent.yaml`:

```yaml
providers:
  deepgram:
    tools:
      - transfer              # Transfer calls to extensions/queues
      - cancel_transfer       # Cancel an active transfer
      - hangup_call           # End call with farewell
      - leave_voicemail       # Send caller to voicemail
      - send_email_summary    # Auto-send call summary
      - request_transcript    # Email transcript on request
```

**Tool Execution**: Deepgram natively supports function calling. Tools are executed automatically when the AI decides to use them based on conversation context.

## Troubleshooting

### Issue: "No Audio" or "Silence"

**Cause**: Sample rate or encoding mismatch

**Fix**:
```yaml
providers:
  deepgram:
    encoding: mulaw        # Must match Asterisk
    sample_rate: 8000      # Must match Asterisk
```

### Issue: "High Latency" (>3 seconds)

**Cause**: Network latency or model selection

**Fix**:
1. Check network: `ping api.deepgram.com`
2. Use faster model: `llm_model: nova-2-conversationalai`
3. Verify API key not rate-limited

### Issue: "Tools Not Working"

**Cause**: Incorrect function calling format

**Fix**: Verify tools are in provider config (not pipeline-level). Deepgram uses its own function calling format - check logs for `FunctionCallRequest` events.

**See**: `docs/contributing/COMMON_PITFALLS.md#deepgram-function-calling`

### Issue: "AI Cuts Off Mid-Sentence"

**Cause**: Endpoint detection too aggressive

**Fix**:
```yaml
providers:
  deepgram:
    enable_endpoint: true    # Keep enabled
    interim_results: true    # Helps with turn detection
```

## Production Considerations

### API Key Management
- Rotate keys every 90 days
- Use separate keys for dev/staging/production
- Monitor usage in Deepgram Console

### Cost Optimization
- Deepgram charges per minute of audio processed
- Monitor concurrent calls to manage costs
- Consider usage-based pricing tier for high volume

### Monitoring
- Track response latency in logs
- Monitor Deepgram API status: https://status.deepgram.com/
- Set up alerts for API errors or high latency

## See Also

- **Implementation & API Reference**: `docs/contributing/references/Provider-Deepgram-Implementation.md`
- **Golden Baseline**: `docs/case-studies/Deepgram-Agent-Golden-Baseline.md`
- **Common Pitfalls**: `docs/contributing/COMMON_PITFALLS.md`
- **Tool Calling Guide**: `docs/TOOL_CALLING_GUIDE.md`

---

**Deepgram Provider Setup - Complete** ✅

For questions or issues, see the [GitHub repository](https://github.com/hkjarral/Asterisk-AI-Voice-Agent).
