"""
OpenAI Realtime API adapter for tool calling.

Handles translation between unified tool format and OpenAI's function calling format.
"""

from typing import Dict, Any, List
from src.tools.registry import ToolRegistry
from src.tools.context import ToolExecutionContext
import structlog
import json

logger = structlog.get_logger(__name__)


class OpenAIToolAdapter:
    """
    Adapter for OpenAI Realtime API tool calling.
    
    Translates between unified tool format and OpenAI's specific event format.
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
        Get tools configuration in OpenAI Realtime format.
        
        Returns:
            List of tool schemas for OpenAI session.update
        
        Example:
            [
                {
                    "type": "function",
                    "name": "transfer_call",
                    "description": "Transfer caller to extension",
                    "parameters": {
                        "type": "object",
                        "properties": {...},
                        "required": [...]
                    }
                }
            ]
        """
        schemas = self.registry.to_openai_realtime_schema()
        logger.debug(f"Generated OpenAI Realtime schemas for {len(schemas)} tools")
        return schemas
    
    async def handle_tool_call_event(
        self,
        event: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Handle function_call event from OpenAI Realtime API.
        
        OpenAI format (from response.output_item.done event):
        {
            "type": "response.output_item.done",
            "response_id": "resp_123",
            "output_index": 0,
            "item": {
                "id": "item_123",
                "type": "function_call",
                "call_id": "call_456",
                "name": "transfer_call",
                "arguments": "{\"target\": \"6000\"}"  // JSON string
            }
        }
        
        Args:
            event: Function call event from OpenAI
            context: Execution context dict with:
                - call_id
                - caller_channel_id
                - bridge_id
                - session_store
                - ari_client
                - config
        
        Returns:
            Dict with call_id and result for sending back to OpenAI
        """
        # Extract function call details from OpenAI format
        item = event.get('item', {})
        
        if item.get('type') != 'function_call':
            logger.error("Item is not a function_call", item_type=item.get('type'))
            return {"status": "error", "message": "Not a function call"}
        
        function_call_id = item.get('call_id')  # OpenAI uses 'call_id' field
        function_name = item.get('name')
        
        # Parse arguments from JSON string to dict
        arguments_str = item.get('arguments', '{}')
        try:
            parameters = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse function arguments: {e}", arguments=arguments_str)
            parameters = {}
        
        logger.info(f"🔧 OpenAI tool call: {function_name}({parameters})", call_id=function_call_id)
        
        # Get tool from registry
        tool = self.registry.get(function_name)
        if not tool:
            error_msg = f"Unknown tool: {function_name}"
            logger.error(error_msg)
            return {
                "call_id": function_call_id,
                "status": "error",
                "message": error_msg
            }
        
        # Build execution context
        exec_context = ToolExecutionContext(
            call_id=context['call_id'],
            caller_channel_id=context.get('caller_channel_id'),
            bridge_id=context.get('bridge_id'),
            session_store=context['session_store'],
            ari_client=context['ari_client'],
            config=context.get('config'),
            provider_name="openai_realtime",
            user_input=context.get('user_input')
        )
        
        # Execute tool
        try:
            result = await tool.execute(parameters, exec_context)
            logger.info(f"✅ Tool {function_name} executed: {result.get('status')}", 
                       call_id=function_call_id)
            result['call_id'] = function_call_id
            result['function_name'] = function_name
            return result
        except Exception as e:
            error_msg = f"Tool execution failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {
                "call_id": function_call_id,
                "function_name": function_name,
                "status": "error",
                "message": error_msg,
                "error": str(e)
            }
    
    async def send_tool_result(
        self,
        result: Dict[str, Any],
        context: Dict[str, Any]
    ) -> None:
        """
        Send tool execution result back to OpenAI Realtime API.
        
        OpenAI format (conversation.item.create event):
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "call_456",
                "output": "{\"status\": \"success\"}"  // Stringified JSON
            }
        }
        
        Then send response.create to have the model respond:
        {
            "type": "response.create"
        }
        
        Args:
            result: Tool execution result (must include call_id and function_name)
            context: Context dict with websocket connection
        """
        websocket = context.get('websocket')
        if not websocket:
            logger.error("No websocket in context, cannot send tool result")
            return
        
        # Extract call_id and function_name from result
        call_id = result.pop('call_id', None)
        function_name = result.pop('function_name', None)
        
        if not call_id:
            logger.error("No call_id in result, cannot send response")
            return
        
        try:
            # Step 1: Send function_call_output
            output_event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result)  # Stringify the result JSON
                }
            }
            await websocket.send(json.dumps(output_event))
            logger.info(f"✅ Sent function output to OpenAI: {result.get('status')}", 
                       call_id=call_id)
            
            # Step 2: Trigger response generation
            response_event = {
                "type": "response.create"
            }
            await websocket.send(json.dumps(response_event))
            logger.info(f"✅ Triggered OpenAI response generation", call_id=call_id)
            
        except Exception as e:
            logger.error(f"Failed to send tool result to OpenAI: {e}", exc_info=True)
