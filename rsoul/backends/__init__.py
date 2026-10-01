"""
Backend registry and factory.

Provides functions to discover and instantiate available backends.
"""

from typing import List, Dict, TYPE_CHECKING
import logging

from .base import BackendUnavailable, DownloadBackend, DownloadStatus, SearchResult, DownloadTask, DownloadTarget

if TYPE_CHECKING:
    from ..config import Context

logger = logging.getLogger(__name__)

# Registry of available backend classes
_BACKEND_REGISTRY: Dict[str, type] = {}


def register_backend(name: str):
    """Decorator to register a backend class."""

    def decorator(cls: type):
        _BACKEND_REGISTRY[name] = cls
        return cls

    return decorator


def get_available_backends() -> List[str]:
    """Get list of registered backend names."""
    return list(_BACKEND_REGISTRY.keys())


def create_backend(name: str, ctx: "Context") -> DownloadBackend:
    """Create a backend instance by name.

    Args:
        name: Backend name (e.g., 'slskd')
        ctx: Application context

    Returns:
        Instantiated backend

    Raises:
        ValueError: If backend not found
    """
    if name not in _BACKEND_REGISTRY:
        raise ValueError(f"Unknown backend: {name}. Available: {get_available_backends()}")

    backend_class = _BACKEND_REGISTRY[name]
    return backend_class(ctx)


def create_backends_from_config(ctx: "Context") -> List[DownloadBackend]:
    """Create and return enabled backends in priority order.

    Reads config to determine which backends are enabled and their priority.

    Args:
        ctx: Application context

    Returns:
        List of backend instances, sorted by priority
    """
    backends: List[DownloadBackend] = []

    # Get priority order from config (comma-separated list)
    priority_str = ctx.config.get("Backends", "priority", fallback="slskd")
    priority_order = [b.strip() for b in priority_str.split(",") if b.strip()]

    for i, backend_name in enumerate(priority_order):
        # Check if backend is enabled
        enabled_key = f"{backend_name}_enabled"
        is_enabled = ctx.config.getboolean("Backends", enabled_key, fallback=True)

        if not is_enabled:
            logger.debug(f"Backend {backend_name} is disabled in config")
            continue

        if backend_name not in _BACKEND_REGISTRY:
            logger.warning(f"Backend {backend_name} not found in registry, skipping")
            continue

        try:
            backend = create_backend(backend_name, ctx)
            # Override priority based on config order
            backend._config_priority = i  # type: ignore
            backends.append(backend)
            logger.info(f"Loaded backend: {backend_name} (priority {i})")
        except Exception as e:
            logger.error(f"Failed to create backend {backend_name}: {e}")

    # Sort by config priority (order in config list)
    backends.sort(key=lambda b: getattr(b, "_config_priority", b.priority))

    return backends


# Export base classes
__all__ = [
    "BackendUnavailable",
    "DownloadBackend",
    "DownloadStatus",
    "SearchResult",
    "DownloadTask",
    "DownloadTarget",
    "register_backend",
    "get_available_backends",
    "create_backend",
    "create_backends_from_config",
]

# Import backends to trigger registration
# Add new backends here as they are created
try:
    from . import slskd_backend  # noqa: E402, F401
except ImportError:
    pass  # slskd_backend not yet created

try:
    from . import stacks_backend  # noqa: E402, F401
except ImportError:
    pass  # stacks_backend dependencies not available
