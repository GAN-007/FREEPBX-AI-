"""
Configuration normalization and sanitization.

This module handles:
- Pipeline definition normalization
- Default profile and context injection
- Provider-specific token sanitization (e.g., Bash ${VAR:-default} tokens)
"""

from typing import Any, Dict


def _compose_provider_components(provider: str) -> Dict[str, Any]:
    """
    Compose canonical component names for provider-backed pipelines.
    
    For a given provider name (e.g., "openai_realtime"), generates
    the expected STT/LLM/TTS component names.
    
    Args:
        provider: Provider name (e.g., "openai_realtime", "deepgram", "local")
        
    Returns:
        Dictionary with stt, llm, tts, and options keys
        
    Complexity: 1
    """
    return {
        "stt": f"{provider}_stt",
        "llm": f"{provider}_llm",
        "tts": f"{provider}_tts",
        "options": {}
    }


def _generate_default_pipeline(config_data: Dict[str, Any]) -> None:
    """
    Populate a default pipeline entry when none are provided.
    
    Creates a "default" pipeline based on the default_provider.
    
    Args:
        config_data: Configuration dictionary to modify in-place
        
    Complexity: 4
    """
    default_provider = config_data.get("default_provider", "openai_realtime")
    pipeline_name = "default"
    default_components = _compose_provider_components(default_provider)
    
    pipelines = config_data.setdefault("pipelines", {})
    existing_entry = pipelines.get(pipeline_name)
    
    if existing_entry is None:
        pipelines[pipeline_name] = _compose_provider_components(default_provider)
    elif isinstance(existing_entry, str):
        pipelines[pipeline_name] = _compose_provider_components(existing_entry)
    elif isinstance(existing_entry, dict):
        existing_entry.setdefault("stt", default_components["stt"])
        existing_entry.setdefault("llm", default_components["llm"])
        existing_entry.setdefault("tts", default_components["tts"])
        if not isinstance(existing_entry.get("options"), dict):
            existing_entry["options"] = {}
    else:
        pipelines[pipeline_name] = _compose_provider_components(default_provider)
    
    config_data.setdefault("active_pipeline", pipeline_name)


def normalize_pipelines(config_data: Dict[str, Any]) -> None:
    """
    Normalize pipeline definitions into the PipelineEntry schema.
    
    Handles various pipeline definition formats:
    - None/missing: Use default provider
    - String: Provider name (e.g., "openai_realtime")
    - Dict: Explicit stt/llm/tts + options
    
    Args:
        config_data: Configuration dictionary to modify in-place
        
    Raises:
        TypeError: If pipeline definition format is unsupported
        
    Complexity: 9
    """
    # Set default_provider if not present (for AppConfig validation)
    config_data.setdefault("default_provider", "openai_realtime")
    default_provider = config_data.get("default_provider")
    pipelines_cfg = config_data.get("pipelines")
    
    if not pipelines_cfg:
        _generate_default_pipeline(config_data)
        return
    
    normalized: Dict[str, Dict[str, Any]] = {}
    
    for pipeline_name, raw_entry in pipelines_cfg.items():
        if raw_entry is None:
            normalized[pipeline_name] = _compose_provider_components(default_provider)
            continue
        
        if isinstance(raw_entry, str):
            normalized[pipeline_name] = _compose_provider_components(raw_entry)
            continue
        
        if isinstance(raw_entry, dict):
            provider_hint = raw_entry.get("provider")
            provider_for_defaults = provider_hint or default_provider
            components = _compose_provider_components(provider_for_defaults)
            
            options_block = raw_entry.get("options") or {}
            if not isinstance(options_block, dict):
                raise TypeError(
                    f"Unsupported pipeline options type for '{pipeline_name}': {type(options_block).__name__}"
                )
            
            normalized_entry = {
                "stt": raw_entry.get("stt", components["stt"]),
                "llm": raw_entry.get("llm", components["llm"]),
                "tts": raw_entry.get("tts", components["tts"]),
                "tools": raw_entry.get("tools") or [],
                "options": options_block,
            }
            
            normalized[pipeline_name] = normalized_entry
            continue
        
        raise TypeError(f"Unsupported pipeline definition for '{pipeline_name}': {type(raw_entry).__name__}")
    
    config_data["pipelines"] = normalized
    config_data.setdefault("active_pipeline", next(iter(normalized.keys())))


def normalize_profiles(config_data: Dict[str, Any]) -> None:
    """
    Inject default profiles and contexts with sane defaults.
    
    Ensures a default telephony_ulaw_8k profile exists for basic
    telephony use cases. Also initializes empty contexts block if missing.
    
    Args:
        config_data: Configuration dictionary to modify in-place
        
    Complexity: 5
    """
    # Ensure profiles block exists
    try:
        profiles_block = (config_data.get('profiles') or {}) if isinstance(config_data.get('profiles'), dict) else {}
    except Exception:
        profiles_block = {}
    
    # Inject default telephony profile if missing
    if 'telephony_ulaw_8k' not in profiles_block:
        profiles_block['telephony_ulaw_8k'] = {
            'internal_rate_hz': 8000,
            'transport_out': {'encoding': 'ulaw', 'sample_rate_hz': 8000},
            'provider_pref': {
                'input': {'encoding': 'mulaw', 'sample_rate_hz': 8000},
                'output': {'encoding': 'mulaw', 'sample_rate_hz': 8000},
                'preferred_chunk_ms': 20,
            },
            'idle_cutoff_ms': 1200,
        }
    
    # Provide default selector if not present
    try:
        default_profile_name = profiles_block.get('default')
    except Exception:
        default_profile_name = None
    if not default_profile_name:
        profiles_block['default'] = 'telephony_ulaw_8k'
    
    config_data['profiles'] = profiles_block
    
    # Contexts mapping (optional). Keep empty by default.
    try:
        contexts_block = config_data.get('contexts')
        if not isinstance(contexts_block, dict):
            contexts_block = {}
    except Exception:
        contexts_block = {}
    config_data['contexts'] = contexts_block


def normalize_local_provider_tokens(config_data: Dict[str, Any]) -> None:
    """
    Sanitize local provider configuration for Bash-style ${VAR:-default} tokens.
    
    os.path.expandvars does not support Bash-style ${VAR:-default} or ${VAR:=default}
    and may leave tokens intact or empty. This function extracts and applies the
    default values so Pydantic receives valid scalars.
    
    Args:
        config_data: Configuration dictionary to modify in-place
        
    Complexity: 8
    """
    def _apply_default_token(val, *, default=None):
        """Extract default value from Bash-style ${VAR:-default} token."""
        # If val is a token like "${NAME:-fallback}" or "${NAME:=fallback}", extract fallback
        if isinstance(val, str) and val.strip().startswith('${') and val.strip().endswith('}'):
            inner = val.strip()[2:-1]
            # Split on first ':' to isolate var name vs default part
            parts = inner.split(':', 1)
            if len(parts) == 2:
                default_part = parts[1]
                # Strip any leading '-', '=' used in Bash syntax
                default_part = default_part.lstrip('-=')
                return default_part
            return default
        # If val is empty string after env expansion, use provided default
        if val == '' and default is not None:
            return default
        return val
    
    try:
        providers_block = config_data.get('providers', {}) or {}
        local_block = providers_block.get('local', {}) or {}
        
        if isinstance(local_block, dict):
            # Apply defaults for known local provider keys
            local_block['ws_url'] = _apply_default_token(
                local_block.get('ws_url'), default='ws://127.0.0.1:8765'
            )
            local_block['connect_timeout_sec'] = _apply_default_token(
                local_block.get('connect_timeout_sec'), default='5.0'
            )
            local_block['response_timeout_sec'] = _apply_default_token(
                local_block.get('response_timeout_sec'), default='5.0'
            )
            local_block['chunk_ms'] = _apply_default_token(
                local_block.get('chunk_ms'), default='200'
            )
            
            # Coerce numeric strings to proper types
            try:
                if isinstance(local_block.get('connect_timeout_sec'), str):
                    local_block['connect_timeout_sec'] = float(local_block['connect_timeout_sec'])
            except Exception:
                local_block['connect_timeout_sec'] = 5.0
            
            try:
                if isinstance(local_block.get('response_timeout_sec'), str):
                    local_block['response_timeout_sec'] = float(local_block['response_timeout_sec'])
            except Exception:
                local_block['response_timeout_sec'] = 5.0
            
            try:
                if isinstance(local_block.get('chunk_ms'), str):
                    local_block['chunk_ms'] = int(float(local_block['chunk_ms']))
            except Exception:
                local_block['chunk_ms'] = 200
            
            providers_block['local'] = local_block
            config_data['providers'] = providers_block
    except Exception:
        # Non-fatal; Pydantic may still coerce correctly
        pass
