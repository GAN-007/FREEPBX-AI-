"""
Configuration file loaders and path resolution.

This module handles:
- Path resolution (relative to absolute)
- YAML file loading
- Environment variable expansion in YAML
"""

import os
import yaml
from pathlib import Path


# Project root directory (parent of src/)
_PROJ_DIR = Path(__file__).parent.parent.parent.resolve()


def resolve_config_path(path: str) -> str:
    """
    Resolve configuration file path to absolute path.
    
    If the provided path is not absolute, it is resolved relative to the project root.
    
    Args:
        path: Configuration file path (absolute or relative)
        
    Returns:
        Absolute path to configuration file
        
    Complexity: 2
    """
    if not os.path.isabs(path):
        return os.path.join(_PROJ_DIR, path)
    return path


def load_yaml_with_env_expansion(path: str) -> dict:
    """
    Load YAML file with environment variable expansion.
    
    Reads the YAML file, expands ${VAR} and $VAR environment variable references,
    then parses the YAML content.
    
    Args:
        path: Absolute path to YAML configuration file
        
    Returns:
        Parsed configuration dictionary
        
    Raises:
        FileNotFoundError: If configuration file doesn't exist
        yaml.YAMLError: If YAML parsing fails
        
    Complexity: 3
    """
    try:
        with open(path, 'r') as f:
            config_str = f.read()
        
        # Substitute environment variables (supports ${VAR} and $VAR)
        config_str_expanded = os.path.expandvars(config_str)
        
        # Parse YAML
        config_data = yaml.safe_load(config_str_expanded)
        
        return config_data if config_data is not None else {}
        
    except FileNotFoundError:
        raise FileNotFoundError(f"Configuration file not found at: {path}")
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Error parsing YAML configuration: {e}")
