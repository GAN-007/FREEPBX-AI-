"""
Tool execution context - provides access to system resources during tool execution.
"""

from dataclasses import dataclass
from typing import Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class ToolExecutionContext:
    """
    Context provided to tools during execution.
    
    Contains all information and system access needed for a tool to execute,
    including call metadata, session state, and system clients (ARI, etc.).
    """
    
    # Call information
    call_id: str
    caller_channel_id: Optional[str] = None
    bridge_id: Optional[str] = None
    
    # System access (injected by provider)
    session_store: Any = None  # SessionStore instance
    ari_client: Any = None      # ARIClient instance
    config: Any = None           # Config dict
    
    # Provider information
    provider_name: str = None  # "deepgram", "openai_realtime", "custom_pipeline"
    provider_session: Any = None
    
    # Request metadata
    user_input: Optional[str] = None  # Original user utterance
    detected_intent: Optional[str] = None
    confidence: Optional[float] = None
    
    async def get_session(self):
        """
        Get current call session from session store.
        
        Returns:
            Session object with call state
        
        Raises:
            RuntimeError: If session not found
        """
        if not self.session_store:
            raise RuntimeError("SessionStore not available in context")
        
        session = await self.session_store.get_by_call_id(self.call_id)
        if not session:
            raise RuntimeError(f"Session not found for call_id: {self.call_id}")
        
        return session
    
    async def update_session(self, **kwargs):
        """
        Update call session with new attributes.
        
        Args:
            **kwargs: Attributes to update on session
        
        Example:
            await context.update_session(
                transfer_active=True,
                transfer_target="2765"
            )
        """
        if not self.session_store:
            raise RuntimeError("SessionStore not available in context")
        
        session = await self.get_session()
        
        for key, value in kwargs.items():
            setattr(session, key, value)
        
        await self.session_store.upsert_call(session)
        logger.debug(f"Updated session {self.call_id}: {kwargs}")
    
    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value.
        
        Args:
            key: Config key (supports dot notation, e.g., "tools.transfer_call.mode")
            default: Default value if key not found
        
        Returns:
            Config value or default
        """
        if not self.config:
            return default
        
        # Support dot notation for nested keys
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
