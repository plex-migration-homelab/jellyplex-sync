import os
import pathlib
from typing import Dict, Optional
import yaml
import logging

log = logging.getLogger(__name__)

def load_config(config_path: Optional[str] = None) -> dict:
    """Load config from explicit path or env var."""
    path = None

    if config_path:
        path = pathlib.Path(config_path)
    elif os.environ.get("JELLYPLEX_CONFIG"):
        path = pathlib.Path(os.environ["JELLYPLEX_CONFIG"])

    if path and path.exists():
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            log.warning(f"Failed to load config file {path}: {e}")
            return {}

    return {}

def get_path_mappings(config_path: Optional[str] = None) -> Dict[str, str]:
    """Get path mappings from config file."""
    config = load_config(config_path)
    mappings = config.get("path_mappings", {})
    if mappings:
        if not isinstance(mappings, dict):
            log.warning(f"path_mappings should be a dictionary, got {type(mappings)}")
            return {}
        log.debug(f"Loaded {len(mappings)} path mapping(s)")
    return mappings
