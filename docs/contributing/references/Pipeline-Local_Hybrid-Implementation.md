# Tool Execution Testing Plan - local_hybrid Pipeline

**Date:** November 19, 2025  
**Pipeline:** local_hybrid (Vosk STT + OpenAI LLM + Piper TTS)  
**Status:** ✅ TESTING COMPLETE - 4/6 Tools Validated, 2/6 Deployed

**Test Results:**

- ✅ transfer - VALIDATED (Call 1763582071.6214)
- ✅ hangup_call - VALIDATED (Call 1763582133.6224)
- ✅ send_email_summary - VALIDATED (Multiple calls)
- ✅ request_transcript - VALIDATED (Call 1763582133.6224)
- 🟡 cancel_transfer - DEPLOYED (Not tested - requires active transfer)
- 🟡 leave_voicemail - DEPLOYED (Not tested - requires voicemail config)

---

## Enabled Tools

### Telephony Tools (4)
1. ✅ **transfer** - UnifiedTransferTool
2. 🆕 **cancel_transfer** - CancelTransferTool
3. ✅ **hangup_call** - HangupCallTool (VALIDATED)
4. 🆕 **leave_voicemail** - VoicemailTool

### Business Tools (2)
5. ✅ **send_email_summary** - SendEmailSummaryTool (VALIDATED)
6. 🆕 **request_transcript** - RequestTranscriptTool

---

## Tool #1: transfer ✅ (Already Working)

**Purpose:** Transfer call to another extension/number

**Test Script:**
```
User: "Can you transfer me to sales?"
Expected: Agent asks for number/extension, then transfers
```

**Validation:**
- [ ] Agent confirms transfer intent
- [ ] Call is transferred successfully
- [ ] Transfer shows in logs

---

## Tool #2: cancel_transfer 🆕

**Purpose:** Cancel an in-progress transfer (while ringing)

**Test Script:**
```
User: "Transfer me to extension 100"
Agent: "Transferring to 100..." (starts ringing)
User: "Actually, cancel that"
Expected: Transfer cancelled, back to conversation
```

**Validation:**
- [ ] Transfer starts (ringing)
- [ ] Cancel command detected
- [ ] Transfer cancelled successfully
- [ ] Conversation resumes

**Edge Cases:**
- Cannot cancel after transfer is answered (tool should return error)
- Cannot cancel if no transfer in progress

---

## Tool #3: hangup_call ✅ VALIDATED

**Purpose:** End call with farewell message

**Test Script:**
```
User: "That's all, goodbye"
Expected: Agent says farewell, call disconnects
```

**Validation:**
- [x] Agent generates farewell message
- [x] Farewell added to conversation history
- [x] Farewell audio plays completely
- [x] Call hangs up automatically
- [x] Email includes farewell

**Status:** ✅ Fully tested and working perfectly

---

## Tool #4: leave_voicemail 🆕

**Purpose:** Transfer caller to voicemail

**Test Script:**
```
User: "Can I leave a voicemail?"
Expected: Agent confirms and transfers to voicemail
```

**Validation:**
- [ ] Agent confirms voicemail intent
- [ ] Call transferred to voicemail application
- [ ] Voicemail recording starts
- [ ] Shows in Asterisk voicemail

**Prerequisites:**
- Voicemail must be configured in Asterisk
- Valid voicemail extension must exist

---

## Tool #5: send_email_summary ✅ VALIDATED

**Purpose:** Send call transcript to configured email

**Test Script:**
```
User: "Can you send me a summary?"
Expected: Email sent with full transcript
```

**Validation:**
- [x] Email sent successfully
- [x] Includes initial greeting
- [x] Includes full conversation
- [x] Includes farewell (if present)
- [x] Email is well-formatted

**Status:** ✅ Fully tested and working perfectly

**Configuration:**
```yaml
tools:
  send_email_summary:
    enabled: true
    default_recipient: haider@jugaar.llc
    from_email: noreply@jugaar.llc
```

---

## Tool #6: request_transcript 🆕

**Purpose:** Email transcript to caller's email address

**Test Script:**
```
User: "Can you email me the transcript?"
Agent: "What's your email address?"
User: "john@example.com"
Expected: Transcript emailed to john@example.com
```

**Validation:**
- [ ] Agent asks for email address
- [ ] LLM extracts email from speech
- [ ] Tool called with correct email parameter
- [ ] Email sent to caller's address
- [ ] Email contains full transcript

**Complexity:**
- Requires email extraction from natural speech
- Must validate email format
- Different from send_email_summary (uses caller's email vs configured email)

**Edge Cases:**
- Invalid email format
- Email not understood by STT
- User doesn't provide email

---

## Test Execution Order

### Phase 1: Individual Tool Testing
Test each tool in isolation to verify basic functionality.

1. ✅ **hangup_call** - Already validated
2. ✅ **send_email_summary** - Already validated
3. 🔲 **transfer** - Test basic transfer
4. 🔲 **leave_voicemail** - Test voicemail transfer
5. 🔲 **request_transcript** - Test email extraction
6. 🔲 **cancel_transfer** - Test transfer cancellation

### Phase 2: Combined Scenarios
Test realistic workflows combining multiple tools.

**Scenario 1: Transfer + Cancel**
```
1. Request transfer to extension
2. Cancel while ringing
3. Continue conversation
4. Hangup
```

**Scenario 2: Full Conversation + Email**
```
1. Have conversation
2. Request transcript to email
3. Provide email address
4. Hangup normally
5. Verify both emails received (summary + transcript)
```

**Scenario 3: Voicemail Flow**
```
1. Ask to leave voicemail
2. Transfer to voicemail
3. Leave message
4. Verify voicemail saved
```

---

## Testing Checklist

### Pre-Test Setup
- [x] All 6 tools registered in tool_registry
- [x] Tools enabled in local_hybrid pipeline config
- [x] Container deployed and running
- [ ] Voicemail configured in Asterisk (for voicemail tool)
- [ ] Test extension configured (for transfer tests)
- [ ] Email server configured (RESEND_API_KEY set)

### Test Environment
- **Server:** voiprnd.nemtclouddispatch.com
- **Pipeline:** local_hybrid
- **Context:** demo_hybrid
- **Tools Config:** config/ai-agent.yaml lines 239-245

### Success Criteria
- [ ] All 6 tools execute without errors
- [ ] Tool detection by LLM works correctly
- [ ] Tool parameters extracted properly
- [ ] Expected side effects occur (emails sent, transfers happen, etc.)
- [ ] Conversation history includes tool actions
- [ ] No crashes or exceptions in logs

---

## Known Limitations

1. **request_transcript** requires accurate email extraction from speech
   - May fail if email address not clearly spoken
   - STT errors on email addresses are common

2. **cancel_transfer** only works during ringing phase
   - Cannot cancel after answer
   - Requires transfer to be in progress

3. **leave_voicemail** requires Asterisk voicemail configuration
   - Must have valid voicemail mailbox
   - Dialplan must route to voicemail app

4. **transfer** requires valid destination
   - Extension must exist
   - Or external number must be permitted by trunk

---

## Logging & Debugging

### Key Log Messages to Watch For

**Tool Detection:**
```
OpenAI chat completion received with tools ... tool_calls=1
DEBUG: LLM Result Type ... is_llm_response=True tool_calls_len=1
```

**Tool Execution:**
```
DEBUG: Processing tool call name=<tool_name>
Executing pipeline tool tool=<tool_name>
Tool execution result tool=<tool_name> result={...}
```

**Tool-Specific:**
```
# Transfer
Initiating transfer to <destination>

# Cancel Transfer  
Transfer cancelled successfully

# Hangup
Executing explicit hangup via ARI
Farewell playback completed

# Voicemail
Transferring to voicemail

# Email Summary
Email summary sent successfully email_id=...

# Request Transcript
Sending transcript to <email>
```

### RCA Collection
After each test, collect logs:
```bash
SERVER_MODE=remote bash scripts/rca_collect.sh
```

---

## Test Results Template

### Tool: [tool_name]
**Date:** [date]  
**Call ID:** [call_id]  
**Result:** ✅ PASS / ❌ FAIL  

**Observations:**
- [What happened]
- [Expected vs actual]
- [Any issues]

**Logs:**
```
[Relevant log excerpts]
```

**Next Steps:**
- [Any follow-up needed]

---

## Deployment Info

**Commit:** 03335e3  
**Deployed:** Nov 19, 2025 19:48:10 UTC  
**Configuration:**
```yaml
pipelines:
  local_hybrid:
    tools:
      - transfer
      - cancel_transfer
      - hangup_call
      - leave_voicemail
      - send_email_summary
      - request_transcript
```

**Tool Registration Confirmed:**
```
✅ Registered tool: transfer (telephony)
✅ Registered tool: cancel_transfer (telephony)
✅ Registered tool: hangup_call (telephony)
✅ Registered tool: leave_voicemail (telephony)
✅ Registered tool: send_email_summary (business)
✅ Registered tool: request_transcript (business)
🛠️  Initialized 6 tools
```

---

## Notes

- **hangup_call** and **send_email_summary** are already validated and working perfectly
- Focus testing on the 4 new tools: transfer, cancel_transfer, leave_voicemail, request_transcript
- Test in order of complexity (simple tools first, then combined scenarios)
- Document any issues or unexpected behavior for future improvements
