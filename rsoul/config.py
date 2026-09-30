import logging
import sys
import configparser
from dataclasses import dataclass, field
from typing import Any, Optional, Dict

from .display import console
from .media import get_media_mode, unsupported_audiobook_formats, EBOOK

logger = logging.getLogger(__name__)

if False:  # TYPE_CHECKING hack to avoid circular imports at runtime if needed, though simple import is usually fine
    from .history import HistoryManager
    from .state import StateManager
    from .orchestrator import DownloadOrchestrator


# ANSI color codes for terminal output
class LogColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"

    # Level colors
    DEBUG = "\033[36m"  # Cyan
    INFO = "\033[32m"  # Green
    WARNING = "\033[33m"  # Yellow
    ERROR = "\033[31m"  # Red
    CRITICAL = "\033[35m"  # Magenta

    # Component colors
    TIMESTAMP = "\033[90m"  # Gray
    NAME = "\033[34m"  # Blue


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log levels."""

    LEVEL_COLORS = {
        logging.DEBUG: LogColors.DEBUG,
        logging.INFO: LogColors.INFO,
        logging.WARNING: LogColors.WARNING,
        logging.ERROR: LogColors.ERROR,
        logging.CRITICAL: LogColors.CRITICAL,
    }

    def format(self, record):
        # Get the color for this level
        level_color = self.LEVEL_COLORS.get(record.levelno, LogColors.RESET)

        # Format the timestamp
        timestamp = self.formatTime(record, self.datefmt)

        # Build the colored log line with tab separator
        # Format: [LEVEL|module|Lline] timestamp:\t message
        prefix = f"{level_color}[{record.levelname}|{record.module}|L{record.lineno}]{LogColors.RESET}"
        time_part = f"{LogColors.TIMESTAMP}{timestamp}{LogColors.RESET}"
        message = record.getMessage()

        # Color the message based on level for warnings/errors
        if record.levelno >= logging.WARNING:
            message = f"{level_color}{message}{LogColors.RESET}"

        return f"{prefix} {time_part}:\t{message}"


DEFAULT_LOGGING_CONF = {
    "level": "INFO",
    "format": "[%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s:\t%(message)s",
    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
}


def setup_logging(config):
    """
    Configure the logging system with colored output.
    """
    if "Logging" in config:
        log_config = config["Logging"]
    else:
        log_config = DEFAULT_LOGGING_CONF

    level = getattr(logging, log_config.get("level", "INFO").upper())
    datefmt = log_config.get("datefmt", DEFAULT_LOGGING_CONF["datefmt"])

    # Check if colors should be enabled (default: True if stdout is a TTY)
    use_colors = sys.stdout.isatty()

    # Create handler
    handler = logging.StreamHandler(sys.stdout)

    if use_colors:
        # Use colored formatter
        handler.setFormatter(ColoredFormatter(datefmt=datefmt))
    else:
        # Use plain formatter for non-TTY (e.g., file output, Docker logs)
        plain_format = log_config.get("format", DEFAULT_LOGGING_CONF["format"])
        handler.setFormatter(logging.Formatter(fmt=plain_format, datefmt=datefmt))

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def validate_config(config: configparser.ConfigParser) -> None:
    """
    Validate that the configuration has all required sections and keys.

    Conditionally validates backend-specific sections when those backends
    are enabled in [Backends].
    """
    KNOWN_BACKENDS = {"slskd", "stacks"}

    required = {
        "Readarr": ["api_key", "host_url"],
    }

    for section, keys in required.items():
        if section not in config:
            raise ValueError(f"Configuration Error: Missing required section '[{section}]'")
        for key in keys:
            if key not in config[section]:
                raise ValueError(f"Configuration Error: Missing required key '{key}' in section '{section}'")

    # Validate backend-specific sections when enabled
    slskd_enabled = config.getboolean("Backends", "slskd_enabled", fallback=True)
    stacks_enabled = config.getboolean("Backends", "stacks_enabled", fallback=False)

    if slskd_enabled:
        slskd_required = ["api_key", "host_url"]
        if "Slskd" not in config:
            raise ValueError("Configuration Error: Slskd backend is enabled but '[Slskd]' section is missing")
        for key in slskd_required:
            if key not in config["Slskd"]:
                raise ValueError(f"Configuration Error: Missing required key '{key}' in section 'Slskd'")

    if stacks_enabled:
        stacks_required = ["api_key", "host_url", "download_dir"]
        if "Stacks" not in config:
            raise ValueError("Configuration Error: Stacks backend is enabled but '[Stacks]' section is missing")
        for key in stacks_required:
            if key not in config["Stacks"]:
                raise ValueError(f"Configuration Error: Missing required key '{key}' in section 'Stacks'")

    # Validate media mode early so a typo fails at startup, not mid-run
    if get_media_mode(config) != EBOOK:
        unsupported = unsupported_audiobook_formats(config)
        if unsupported:
            logger.warning(f"audiobook_formats contains formats Chaptarr can't import: {', '.join(unsupported)}")

    # Validate priority list references only known backends
    if "Backends" in config:
        priority_str = config.get("Backends", "priority", fallback="slskd")
        priority_backends = [b.strip().lower() for b in priority_str.split(",") if b.strip()]
        unknown = set(priority_backends) - KNOWN_BACKENDS
        if unknown:
            logger.warning(f"Unknown backend(s) in priority list: {unknown}. Known backends: {KNOWN_BACKENDS}")


@dataclass
class Context:
    """
    Application context to hold shared state across the application.
    """

    config: Any  # dict or ConfigParser
    slskd: Any
    readarr: Any
    config_dir: str = "."
    stats: Optional[Dict[str, Any]] = field(default_factory=dict)
    history: Any = None
    state: Any = None  # StateManager for resume functionality
    orchestrator: Any = None  # DownloadOrchestrator for backend management
