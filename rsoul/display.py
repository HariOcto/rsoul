import os
import logging
from rich.console import Console
from .version import __version__, BUILD_ID


# Safe terminal width detection with proper error handling
def get_terminal_width():
    """Get terminal width with fallback for environments without a terminal"""
    try:
        # Try to get actual terminal size
        if hasattr(os, "get_terminal_size"):
            return os.get_terminal_size().columns
        else:
            return 120  # Fallback for older Python versions
    except (OSError, ValueError):
        # Handle cases where there's no terminal (Docker, CI/CD, etc.)
        # Try environment variables first
        try:
            width = os.environ.get("COLUMNS")
            if width:
                return int(width)
        except (ValueError, TypeError):
            pass

        # Final fallback
        return 120


# Initialize rich console with safe width detection
terminal_width = get_terminal_width()
console = Console(width=terminal_width)

logger = logging.getLogger("rsoul.display")


def print_startup_banner():
    """Print a simple startup banner"""
    banner = """
================================================================
                         READARR SOUL                         
                    Enhanced Book Downloader                  
                     Powered by Soulseek                     
================================================================
    """
    print(banner)
    print(f"R:soul {__version__} — {BUILD_ID}")


def print_search_summary(query, results_count, search_type="main", status="completed"):
    """Print a simple search summary"""
    prefix = "Fallback Search" if search_type == "fallback" else "Main Search"
    if status == "searching":
        logger.info(f"{prefix}: {query} - Status: Searching...")
    else:
        logger.info(f"{prefix}: {query} - Results: {results_count} files found")


def print_directory_summary(username, directory_data):
    """Print a clean summary of directory contents"""
    if isinstance(directory_data, list) and len(directory_data) > 0:
        dir_info = directory_data[0]
        file_count = dir_info.get("fileCount", 0)
        dir_name = dir_info.get("name", "Unknown")
    elif isinstance(directory_data, dict):
        file_count = len(directory_data.get("files", []))
        dir_name = directory_data.get("name", "Unknown")
    else:
        file_count = 0
        dir_name = "Unknown"

    short_dir = dir_name.split("\\")[-1]
    logger.info(f"User: {username} | Directory: {short_dir} | Files: {file_count}")


def print_download_summary(downloads):
    """Print a simple list of downloads"""
    if not downloads:
        logger.info("No downloads to process")
        return

    logger.info("--- Download Queue ---")
    for download in downloads:
        username = download["username"]
        for dir_info in download["directories"]:
            logger.info(f"User: {username} | Directory: {dir_info['directory']}")
    logger.info("----------------------")


def print_import_summary(commands):
    """Print a simple list of import operations"""
    if not commands:
        return

    logger.info("--- Import Operations ---")
    for command in commands:
        author_name = command.get("_rsoul_folder") or "Unknown"
        if command.get("body", {}).get("path"):
            author_name = os.path.basename(command["body"]["path"])

        logger.info(f"Import folder: {author_name} | Command ID: {command['id']} | Status: Queued")
    logger.info("-------------------------")


def print_match_details(filename, ratio, username, filetype):
    """Print simple match details - toned down version"""
    short_filename = filename.split("\\")[-1] if "\\" in filename else filename
    logger.debug(f"Match candidate: {short_filename} (ratio: {ratio:.3f}, user: {username}, type: {filetype})")


def print_section_header(title, style=None):
    """Print a section header with a simple separator line"""
    logger.info("=" * 40 + f" {title} " + "=" * 40)


def print_run_summary(grabbed_count, failed_count, failed_books=None, failed_imports=None, failed_downloads=None):
    """Print a final run summary with counts, unfound books, failed downloads and import failures."""
    logger.info("=" * 40 + " RUN SUMMARY " + "=" * 40)
    logger.info(f"Books grabbed: {grabbed_count}")
    logger.info(f"Books failed:  {failed_count}")
    if failed_books:
        logger.info(f"Not found ({len(failed_books)}):")
        for author, title in failed_books:
            logger.info(f"  - {author} — {title}")
    if failed_downloads:
        logger.info(f"Download failed ({len(failed_downloads)}):")
        for author, title, reason in failed_downloads:
            logger.info(f"  - {author} — {title}" + (f" ({reason})" if reason else ""))
    if failed_imports:
        logger.info(f"Import failed ({len(failed_imports)}):")
        for author, title in failed_imports:
            logger.info(f"  - {author} — {title}")
    logger.info("=" * 93)


__all__ = [
    "console",
    "get_terminal_width",
    "print_startup_banner",
    "print_search_summary",
    "print_directory_summary",
    "print_download_summary",
    "print_import_summary",
    "print_match_details",
    "print_section_header",
    "print_run_summary",
]
