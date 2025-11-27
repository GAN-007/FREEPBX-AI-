"""
Base classes for unified tool calling system.

This module defines the core abstractions that all tools must implement,
regardless of which AI provider they're used with.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ToolCategory(Enum):
    """Category of tool for execution routing."""
    TELEPHONY = "telephony"  # Executes via ARI (transfers, voicemail, etc.)
    BUSINESS = "business"     # Executes via provider-native or external APIs
    HYBRID = "hybrid"         # May use both telephony and business logic


@dataclass
class ToolParameter:
    """Definition of a tool parameter."""
    name: str
    type: str  # "string", "integer", "boolean", "number", "array", "object"
    description: str
    required: bool = False
    enum: Optional[List[str]] = None
    default: Optional[Any] = None
    
    def to_dict(self, include_default: bool = True) -> Dict[str, Any]:
        """
        Convert to dictionary for schema generation.
        
        Args:
            include_default: Whether to include the default field (some providers don't support it)
        """
        result = {
            "type": self.type,
            "description": self.description
        }
        if self.enum:
            result["enum"] = self.enum
        # Only include default if requested (Deepgram doesn't support it)
        if include_default and self.default is not None:
            result["default"] = self.default
        return result


@dataclass
class ToolDefinition:
    """
    Provider-agnostic tool definition.
    
    Contains all metadata needed to expose a tool to any AI provider.
    """
    name: str
    description: str
    category: ToolCategory
    parameters: List[ToolParameter] = field(default_factory=list)
    requires_channel: bool = False  # Needs active call channel
    max_execution_time: int = 30    # Timeout in seconds
    
    def to_deepgram_schema(self) -> Dict[str, Any]:
        """
        Convert to Deepgram Voice Agent function calling format.
        
        Deepgram format:
        {
            "name": "tool_name",
            "description": "Tool description",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        
        Note: Deepgram doesn't support 'default' field in parameters,
        so we exclude it with include_default=False.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    p.name: p.to_dict(include_default=False)
                    for p in self.parameters
                },
                "required": [p.name for p in self.parameters if p.required]
            }
        }
    
    def to_openai_schema(self) -> Dict[str, Any]:
        """
        Convert to OpenAI API function calling format (Chat Completions).
        
        OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "tool_name",
                "description": "Tool description",
                "parameters": {
                    "type": "object",
                    "properties": {...},
                    "required": [...]
                }
            }
        }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p.name: p.to_dict()
                        for p in self.parameters
                    },
                    "required": [p.name for p in self.parameters if p.required]
                }
            }
        }
    
    def to_openai_realtime_schema(self) -> Dict[str, Any]:
        """
        Convert to OpenAI Realtime API function calling format.
        
        OpenAI Realtime format (flatter structure, different from Chat Completions):
        {
            "type": "function",
            "name": "tool_name",
            "description": "Tool description",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        
        Note: Realtime API has name/description at top level, not nested under "function"
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    p.name: p.to_dict()
                    for p in self.parameters
                },
                "required": [p.name for p in self.parameters if p.required]
            }
        }
    
    def to_prompt_text(self) -> str:
        """
        Convert to text format for custom pipeline system prompts.
        
        Used when provider doesn't have native function calling (custom pipelines).
        LLM learns to output structured text that we can parse.
        """
        params_desc = []
        for p in self.parameters:
            req_str = "required" if p.required else "optional"
            param_str = f"{p.name} ({p.type}, {req_str}): {p.description}"
            if p.enum:
                param_str += f" [options: {', '.join(p.enum)}]"
            params_desc.append(param_str)
        
        params_text = "\n  ".join(params_desc) if params_desc else "  (no parameters)"
        
        return f"{self.name}: {self.description}\n  Parameters:\n  {params_text}"


class Tool(ABC):
    """
    Abstract base class for all tools.
    
    All tools must inherit from this class and implement:
    - definition property: Returns ToolDefinition with metadata
    - execute method: Performs the actual tool action
    """
    
    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return tool definition with metadata."""
        pass
    
    @abstractmethod
    async def execute(
        self,
        parameters: Dict[str, Any],
        context: 'ToolExecutionContext'
    ) -> Dict[str, Any]:
        """
        Execute the tool with given parameters and context.
        
        Args:
            parameters: Tool parameters from AI provider
            context: Execution context with call info and system access
        
        Returns:
            Result dictionary with:
            - status: "success" | "failed" | "error"
            - message: Human-readable message for AI to speak
            - Additional tool-specific fields
        
        Raises:
            ValueError: If parameters are invalid
            RuntimeError: If execution fails
        """
        pass
    
    async def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate parameters before execution.
        
        Args:
            parameters: Parameters to validate
        
        Returns:
            True if valid
        
        Raises:
            ValueError: If validation fails with specific error message
        """
        # Check required parameters
        for param in self.definition.parameters:
            if param.required and param.name not in parameters:
                raise ValueError(f"Missing required parameter: {param.name}")
            
            # Check enum values
            if param.enum and param.name in parameters:
                if parameters[param.name] not in param.enum:
                    raise ValueError(
                        f"Invalid value for {param.name}. "
                        f"Must be one of: {', '.join(param.enum)}"
                    )
        
        return True
    
    def _load_config(self) -> Dict[str, Any]:
        """
        Load tool-specific configuration from ai-agent.yaml.
        
        Returns empty dict if no config found.
        """
        from src.config import load_config
        
        try:
            config = load_config()
            tools_config = config.get('tools', {})
            return tools_config.get(self.definition.name, {})
        except Exception as e:
            logger.warning(f"Failed to load config for {self.definition.name}: {e}")
            return {}
