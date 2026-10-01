import logging
import math
import os
import sys
import configparser
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List, Mapping

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


ENV_PREFIX = "RSOUL__"

# Settings that must be finite numbers >= 0 (0 disables a limit where documented)
NON_NEGATIVE_SETTINGS = [
    ("Slskd", "request_timeout"),
    ("Readarr", "request_timeout"),
    ("Readarr", "import_poll_timeout"),
    ("Download Settings", "stall_timeout"),
    ("Download Settings", "queue_timeout"),
    ("Download Settings", "max_download_time"),
]
# Settings read as whole numbers, which must be >= 0
NON_NEGATIVE_INT_SETTINGS = [
    ("Download Settings", "monitor_window"),
    ("Download Settings", "max_active_downloads"),
]
_SENSITIVE = ("api_key", "password", "token", "secret")
# Sections the code reads that the sample config.ini doesn't contain
_EXTRA_SECTIONS = {"general": "General"}


def _section_key(name: str) -> str:
    """Normalise a section name for matching: "Search Settings" == "SEARCH_SETTINGS"."""
    return name.strip().lower().replace(" ", "_")


def apply_env_overrides(config: configparser.ConfigParser, environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """Override config values from environment variables.

    RSOUL__<SECTION>__<OPTION>=value sets <option> in [<Section>]; spaces in section names
    become underscores and case doesn't matter, e.g.

        RSOUL__READARR__API_KEY=abc          -> [Readarr] api_key
        RSOUL__SEARCH_SETTINGS__MEDIA_MODE=audiobook -> [Search Settings] media_mode

    Environment values win over config.ini. Only sections that exist in the config (or in
    the bundled template it was based on) are accepted, so a typo is reported instead of
    silently creating a new section.

    Returns:
        Log lines describing what was set (sensitive values are not shown).
    """
    environ = os.environ if environ is None else environ
    sections = {_section_key(name): name for name in config.sections()}
    applied: List[str] = []

    for key in sorted(environ):
        if not key.upper().startswith(ENV_PREFIX):
            continue
        rest = key[len(ENV_PREFIX):]
        if "__" not in rest:
            applied.append(f"Ignored {key}: expected {ENV_PREFIX}<SECTION>__<OPTION>")
            continue
        section_token, option = rest.split("__", 1)
        section = sections.get(_section_key(section_token))
        if section is None and _section_key(section_token) in _EXTRA_SECTIONS:
            # Read by the code but absent from the sample config
            section = _EXTRA_SECTIONS[_section_key(section_token)]
            config.add_section(section)
            sections[_section_key(section)] = section
        option = option.strip().lower()
        if not section or not option:
            applied.append(f"Ignored {key}: unknown section '{section_token}' (known: {', '.join(config.sections())})")
            continue

        config.set(section, option, environ[key])
        shown = "(hidden)" if any(word in option for word in _SENSITIVE) else environ[key]
        applied.append(f"[{section}] {option} = {shown} (from {key})")

    return applied


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

    # Placeholders from the sample config (e.g. YOUR_READARR_API_KEY) were never replaced
    enabled_sections = ["Readarr"]
    if config.getboolean("Backends", "slskd_enabled", fallback=True):
        enabled_sections.append("Slskd")
    if config.getboolean("Backends", "stacks_enabled", fallback=False):
        enabled_sections.append("Stacks")
    for section in enabled_sections:
        value = config.get(section, "api_key", fallback="")
        if value.startswith("YOUR_"):
            env = f"{ENV_PREFIX}{section.upper().replace(' ', '_')}__API_KEY"
            raise ValueError(f"Configuration Error: [{section}] api_key is still the placeholder '{value}'. Set it in config.ini or with the environment variable {env}.")

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

    # Numeric settings: a typo such as "-100" or "nan" must not silently disable a limit
    for section, option in NON_NEGATIVE_SETTINGS:
        if config.has_option(section, option):
            raw = config.get(section, option).strip()
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"Configuration Error: [{section}] {option} = '{raw}' is not a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Configuration Error: [{section}] {option} = '{raw}' must be 0 or a positive number")
    for section, option in NON_NEGATIVE_INT_SETTINGS:
        if config.has_option(section, option):
            raw = config.get(section, option).strip()
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"Configuration Error: [{section}] {option} = '{raw}' must be a whole number")
            if value < 0:
                raise ValueError(f"Configuration Error: [{section}] {option} = '{raw}' must be 0 or a positive whole number")

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
