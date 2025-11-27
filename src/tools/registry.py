"""
Tool registry - central repository for all available tools.

Singleton pattern ensures only one registry exists across the application.
"""

from typing import Dict, List, Type, Optional
from src.tools.base import Tool, ToolDefinition, ToolCategory
import logging

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Singleton registry for all available tools.
    
    Manages tool registration, lookup, and schema generation for different providers.
    """
    
    _instance = None
    
    def __new__(cls):
        """Singleton pattern - only one instance exists."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, Tool] = {}
            cls._instance._initialized = False
        return cls._instance
    
    def register(self, tool_class: Type[Tool]) -> None:
        """
        Register a tool class.
        
        Args:
            tool_class: Tool class (not instance) to register
        
        Example:
            registry.register(TransferCallTool)
        """
        tool = tool_class()
        tool_name = tool.definition.name
        
        if tool_name in self._tools:
            logger.warning(f"Tool {tool_name} already registered, overwriting")
        
        self._tools[tool_name] = tool
        logger.info(f"✅ Registered tool: {tool_name} ({tool.definition.category.value})")
    
    def get(self, name: str) -> Optional[Tool]:
        """
        Get tool by name.
        
        Args:
            name: Tool name (e.g., "transfer_call")
        
        Returns:
            Tool instance or None if not found
        """
        return self._tools.get(name)
    
    def get_all(self) -> List[Tool]:
        """
        Get all registered tools.
        
        Returns:
            List of all tool instances
        """
        return list(self._tools.values())
    
    def get_by_category(self, category: ToolCategory) -> List[Tool]:
        """
        Get tools by category.
        
        Args:
            category: ToolCategory enum value
        
        Returns:
            List of tools in that category
        """
        return [
            tool for tool in self._tools.values()
            if tool.definition.category == category
        ]
    
    def get_definitions(self) -> List[ToolDefinition]:
        """
        Get all tool definitions.
        
        Returns:
            List of ToolDefinition objects
        """
        return [tool.definition for tool in self._tools.values()]
    
    def to_deepgram_schema(self) -> List[Dict]:
        """
        Export all tools in Deepgram Voice Agent format.
        
        Returns:
            List of tool schemas for Deepgram
        """
        return [
            tool.definition.to_deepgram_schema()
            for tool in self._tools.values()
        ]
    
    def to_openai_schema(self) -> List[Dict]:
        """
        Export all tools in OpenAI Chat Completions API format.
        
        Returns:
            List of tool schemas for OpenAI Chat Completions (nested format)
        """
        return [
            tool.definition.to_openai_schema()
            for tool in self._tools.values()
        ]
    
    def to_openai_realtime_schema(self) -> List[Dict]:
        """
        Export all tools in OpenAI Realtime API format.
        
        Returns:
            List of tool schemas for OpenAI Realtime API (flat format)
        """
        return [
            tool.definition.to_openai_realtime_schema()
            for tool in self._tools.values()
        ]
    
    def to_prompt_text(self) -> str:
        """
        Export all tools as text for custom pipeline system prompts.
        
        Returns:
            Formatted text description of all tools
        """
        if not self._tools:
            return ""
        
        lines = ["Available tools:\n"]
        for tool in self._tools.values():
            lines.append(tool.definition.to_prompt_text())
            lines.append("")  # Blank line between tools
        
        return "\n".join(lines)
    
    def initialize_default_tools(self) -> None:
        """
        Register all built-in tools.
        
        Called once during engine startup to register all available tools.
        """
        if self._initialized:
            logger.info("Tools already initialized, skipping")
            return
        
        logger.info("Initializing default tools...")
        
        # Import and register telephony tools
        try:
            from src.tools.telephony.unified_transfer import UnifiedTransferTool
            self.register(UnifiedTransferTool)
        except ImportError as e:
            logger.warning(f"Could not import UnifiedTransferTool: {e}")
        
        try:
            from src.tools.telephony.cancel_transfer import CancelTransferTool
            self.register(CancelTransferTool)
        except ImportError as e:
            logger.warning(f"Could not import CancelTransferTool: {e}")
        
        try:
            from src.tools.telephony.hangup import HangupCallTool
            self.register(HangupCallTool)
        except ImportError as e:
            logger.warning(f"Could not import HangupCallTool: {e}")
        
        try:
            from src.tools.telephony.voicemail import VoicemailTool
            self.register(VoicemailTool)
        except ImportError as e:
            logger.warning(f"Could not import VoicemailTool: {e}")
        
        # Business tools
        try:
            from src.tools.business.email_summary import SendEmailSummaryTool
            self.register(SendEmailSummaryTool)
        except ImportError as e:
            logger.warning(f"Could not import SendEmailSummaryTool: {e}")
        
        try:
            from src.tools.business.request_transcript import RequestTranscriptTool
            self.register(RequestTranscriptTool)
        except ImportError as e:
            logger.warning(f"Could not import RequestTranscriptTool: {e}")
        
        # Future tools will be registered here:
        # from src.tools.telephony.voicemail import SendToVoicemailTool
        # self.register(SendToVoicemailTool)
        
        self._initialized = True
        logger.info(f"🛠️  Initialized {len(self._tools)} tools")
    
    def list_tools(self) -> List[str]:
        """
        Get list of all tool names.
        
        Returns:
            List of tool names
        """
        return list(self._tools.keys())
    
    def clear(self) -> None:
        """
        Clear all registered tools.
        
        Mainly for testing purposes.
        """
        self._tools.clear()
        self._initialized = False
        logger.info("Cleared all registered tools")


# Global singleton instance
tool_registry = ToolRegistry()
