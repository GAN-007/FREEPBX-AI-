"""
Google Gemini Live API adapter for tool calling.

Handles translation between unified tool format and Google's function calling format.
"""

from typing import Dict, Any, List
from src.tools.registry import ToolRegistry
from src.tools.context import ToolExecutionContext
import structlog
import json

logger = structlog.get_logger(__name__)


class GoogleToolAdapter:
    """
    Adapter for Google Gemini Live API tool calling.
    
    Translates between unified tool format and Google's function declaration format.
    """
    
    def __init__(self, registry: ToolRegistry):
        """
        Initialize adapter with tool registry.
        
        Args:
            registry: ToolRegistry instance with registered tools
        """
        self.registry = registry
    
    def get_tools_config(self) -> List[Dict[str, Any]]:
        """
        Get ALL tools configuration in Google Live API format.
        
        This method follows the same pattern as Deepgram and OpenAI adapters,
        returning all registered tools automatically.
        
        Google format:
        [{
            "functionDeclarations": [
                {
                    "name": "transfer_call",
                    "description": "Transfer the call to another extension",
                    "parameters": {
                        "type": "object",
                        "properties": {...},
                        "required": [...]
                    }
                }
            ]
        }]
        
        Returns:
            List of tool declarations in Google format (all registered tools)
        """
        function_declarations = []
        
        # Iterate through all registered tools
        for tool_name, tool in self.registry._tools.items():
            try:
                definition = tool.definition
                declaration = {
                    "name": tool_name,
                    "description": definition.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            p.name: p.to_dict()
                            for p in definition.parameters
                        },
                        "required": [p.name for p in definition.parameters if p.required]
                    }
                }
                function_declarations.append(declaration)
            except Exception as e:
                logger.warning(f"Failed to format tool {tool_name}: {e}")
                continue
        
        logger.debug(f"Formatted {len(function_declarations)} tools for Google Live")
        
        return [{
            "functionDeclarations": function_declarations  # camelCase per official API
        }] if function_declarations else []
    
    def format_tools(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        """
        Format SPECIFIC tools for Google Gemini Live API.
        
        DEPRECATED: Use get_tools_config() instead for consistency with other providers.
        This method is kept for backwards compatibility.
        
        Args:
            tool_names: List of tool names to include
            
        Returns:
            List of tool declarations in Google format
        """
        function_declarations = []
        
        for tool_name in tool_names:
            tool = self.registry.get(tool_name)
            if not tool:
                logger.warning(f"Tool not found in registry: {tool_name}")
                continue
            
            # Convert tool schema to Google format
            # Use the tool's definition to get description and parameters
            definition = tool.definition
            declaration = {
                "name": tool_name,
                "description": definition.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p.name: p.to_dict()
                        for p in definition.parameters
                    },
                    "required": [p.name for p in definition.parameters if p.required]
                }
            }
            function_declarations.append(declaration)
        
        logger.debug(f"Formatted {len(function_declarations)} tools for Google Live")
        
        return [{
            "functionDeclarations": function_declarations  # camelCase per official API
        }] if function_declarations else []
    
    async def execute_tool(
        self,
        function_name: str,
        arguments: Dict[str, Any],
        context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """
        Execute a tool with given arguments.
        
        Args:
            function_name: Name of the tool to execute
            arguments: Tool parameters
            context: Execution context
            
        Returns:
            Tool execution result
        """
        logger.info(
            f"🔧 Google tool call: {function_name}({arguments})",
            call_id=context.call_id,
        )
        
        # Get tool from registry
        tool = self.registry.get(function_name)
        if not tool:
            error_msg = f"Unknown tool: {function_name}"
            logger.error(error_msg)
            return {
                "status": "error",
                "message": error_msg
            }
        
        # Execute tool
        try:
            result = await tool.execute(arguments, context)
            logger.info(
                f"✅ Tool {function_name} executed: {result.get('status')}",
                call_id=context.call_id,
            )
            return result
        except Exception as e:
            error_msg = f"Tool execution failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {
                "status": "error",
                "message": error_msg,
                "error": str(e)
            }
