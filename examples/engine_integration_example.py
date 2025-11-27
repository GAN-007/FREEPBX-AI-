"""
Example showing how the Engine would integrate with the new core components.

This demonstrates how the current dict soup in engine.py would be replaced
with the SessionStore and PlaybackManager.
"""

import asyncio
from src.core import SessionStore, PlaybackManager, CallSession
from src.ari_client import ARIClient
from src.providers.local import LocalProvider


class EngineIntegrationExample:
    """
    Example of how Engine would use the new core components.
    
    This replaces the current dict soup approach with clean, typed interfaces.
    """
    
    def __init__(self, config, ari_client: ARIClient):
        # Initialize core components
        self.session_store = SessionStore()
        self.playback_manager = PlaybackManager(self.session_store, ari_client)
        self.ari_client = ari_client
        self.config = config
        
        # Provider management (simplified)
        self.provider = LocalProvider(config)
    
    async def handle_stasis_start(self, event):
        """Handle StasisStart event - replaces current _on_stasis_start."""
        caller_channel_id = event['channel']['id']
        
        # Create call session with proper initialization
        session = CallSession(
            call_id=caller_channel_id,  # Canonical call_id
            caller_channel_id=caller_channel_id,
            provider_name=self.config.default_provider,
            conversation_state="greeting"
        )
        
        # Store session atomically
        await self.session_store.upsert_call(session)
        
        # Answer call and start provider session
        await self.ari_client.answer_channel(caller_channel_id)
        await self.provider.start_session(caller_channel_id)
        
        # Play greeting using PlaybackManager
        greeting_text = self.config.llm.initial_greeting
        greeting_audio = await self.provider.text_to_speech(greeting_text)
        
        if greeting_audio:
            playback_id = await self.playback_manager.play_audio(
                caller_channel_id,  # Use call_id
                greeting_audio,
                "greeting"
            )
            
            if playback_id:
                logger.info("Greeting playback started",
                           call_id=caller_channel_id,
                           playback_id=playback_id)
    
    async def handle_playback_finished(self, event):
        """Handle PlaybackFinished event - replaces current _on_playback_finished."""
        playback_id = event['playback']['id']
        
        # Delegate to PlaybackManager - it handles all the gating logic
        success = await self.playback_manager.on_playback_finished(playback_id)
        
        if success:
            logger.info("PlaybackFinished handled successfully",
                       playback_id=playback_id)
        else:
            logger.warning("PlaybackFinished for unknown playback",
                         playback_id=playback_id)
    
    async def handle_agent_audio(self, event):
        """Handle AgentAudio event - replaces current _on_agent_audio."""
        audio_data = event['audio_data']
        call_id = event.get('call_id')  # This would come from the event
        
        if not call_id:
            logger.warning("AgentAudio event missing call_id")
            return
        
        # Get session to check if we should process this audio
        session = await self.session_store.get_by_call_id(call_id)
        if not session:
            logger.warning("AgentAudio for unknown call", call_id=call_id)
            return
        
        # Check if audio capture is enabled (TTS gating handled by PlaybackManager)
        if not session.audio_capture_enabled:
            logger.debug("Audio capture disabled, skipping processing",
                        call_id=call_id)
            return
        
        # Process audio through provider
        try:
            # Send audio to provider for STT
            await self.provider.send_audio(audio_data)
            
            # Get STT result (this would be async in real implementation)
            transcript = await self.provider.get_transcript()
            
            if transcript:
                # Get LLM response
                response = await self.provider.get_llm_response(transcript)
                
                if response:
                    # Generate TTS audio
                    tts_audio = await self.provider.text_to_speech(response)
                    
                    if tts_audio:
                        # Play response using PlaybackManager
                        playback_id = await self.playback_manager.play_audio(
                            call_id,
                            tts_audio,
                            "response"
                        )
                        
                        if playback_id:
                            logger.info("Response playback started",
                                       call_id=call_id,
                                       playback_id=playback_id)
        
        except Exception as e:
            logger.error("Error processing AgentAudio",
                        call_id=call_id,
                        error=str(e),
                        exc_info=True)
    
    async def handle_channel_destroyed(self, event):
        """Handle ChannelDestroyed event - cleanup."""
        channel_id = event['channel']['id']
        
        # Get session by channel_id (could be caller or local)
        session = await self.session_store.get_by_channel_id(channel_id)
        if session:
            # Clean up session
            await self.session_store.remove_call(session.call_id)
            
            # Stop provider session
            await self.provider.stop_session(session.call_id)
            
            logger.info("Call session cleaned up",
                       call_id=session.call_id,
                       channel_id=channel_id)


# Example of how the current Engine would be refactored
class CurrentEngineProblems:
    """
    Examples of current problems that the new architecture solves.
    """
    
    def current_problems(self):
        """Current problems in engine.py that we're solving."""
        
        # PROBLEM 1: Dict soup with missing fields
        # Current code:
        # call_data = {
        #     "provider": provider,
        #     "conversation_state": "greeting",
        #     "audio_capture_enabled": False
        #     # Missing: tts_tokens, tts_active_count, tts_playing
        # }
        
        # SOLUTION: CallSession dataclass ensures all fields present
        session = CallSession(
            call_id="test_call",
            caller_channel_id="test_call",
            provider_name="local"
            # All fields have defaults, no missing fields possible
        )
        
        # PROBLEM 2: PlaybackFinished "unknown ID" errors
        # Current code:
        # self.active_playbacks[playback_id] = {...}  # In one place
        # playback_ref = self.active_playbacks.get(playback_id)  # In another place
        
        # SOLUTION: PlaybackManager owns all playback logic
        # playback_manager.play_audio() -> generates ID and tracks it
        # playback_manager.on_playback_finished() -> handles cleanup
        
        # PROBLEM 3: Fallback system ignoring TTS gating
        # Current code:
        # async def _fallback_audio_processing(self, ...):
        #     # No TTS gating check!
        #     await provider.send_audio(buffer)
        
        # SOLUTION: PlaybackManager handles all gating centrally
        # session.audio_capture_enabled is managed atomically
        # All audio processing checks this flag
        
        # PROBLEM 4: Race conditions and inconsistent state
        # Current code:
        # call_data["tts_playing"] = True  # In one thread
        # call_data["audio_capture_enabled"] = False  # In another thread
        
        # SOLUTION: SessionStore with asyncio.Lock ensures atomic updates
        # await session_store.set_gating_token(call_id, playback_id)  # Atomic


if __name__ == "__main__":
    # Example usage
    print("This example shows how the Engine would integrate with the new core components.")
    print("The new architecture eliminates:")
    print("- Dict soup with missing fields")
    print("- PlaybackFinished 'unknown ID' errors") 
    print("- Fallback system ignoring TTS gating")
    print("- Race conditions and inconsistent state")
    print("\nAll operations are now atomic and type-safe!")
