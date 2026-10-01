#!/usr/bin/env python

import sys
import time
import argparse
import os
import configparser
import logging
from rich.console import Console

# Import from rsoul package
from readarr_api import ReadarrAPI
import slskd_api

from rsoul.config import Context, setup_logging, validate_config, apply_env_overrides, ENV_PREFIX
from rsoul.display import print_startup_banner, console
from rsoul.utils import is_docker
from rsoul.workflow import run_workflow
from rsoul.search import get_books, SearchError
from rsoul.media import get_media_mode
from rsoul.net import apply_timeout
from rsoul.locking import AlreadyRunning, InstanceLock
from rsoul import health
from rsoul.history import HistoryManager
from rsoul.state import StateManager
from rsoul.backends import create_backends_from_config
from rsoul.orchestrator import DownloadOrchestrator

logger = logging.getLogger("readarr_soul")


# Exit codes (run.sh logs anything other than 0)
EXIT_OK = 0  # run finished; books not found on Soulseek don't count as errors
EXIT_ERROR = 1  # configuration/startup problem or a crash: nothing (more) was done
EXIT_PARTIAL = 2  # saved downloads were handled, but the search for new books failed

# Outcome of the last run, for the health status (filled in by main)
LAST_RUN: dict = {}


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="""Readarr Soul: Connect Readarr with Soulseek""")

    default_data_directory = os.getcwd()
    if is_docker():
        default_data_directory = "/data"

    parser.add_argument(
        "-c",
        "--config-dir",
        default=default_data_directory,
        const=default_data_directory,
        nargs="?",
        type=str,
        help="Config directory (default: %(default)s)",
    )

    args = parser.parse_args()
    config_dir = args.config_dir

    # Path setup
    config_file_path = os.path.join(config_dir, "config.ini")

    # One R:soul per data folder (an OS lock: released automatically, even after a crash)
    instance_lock = InstanceLock(config_dir)
    try:
        instance_lock.acquire()
    except AlreadyRunning as e:
        console.print(str(e), style="bold red")
        return EXIT_ERROR

    exit_code = EXIT_OK
    health.configure(config_dir)
    health.write_status("running", run_started_at=time.time())
    try:
        # Print banner
        print_startup_banner()

        # Load Config
        # Disable interpolation to make storing logging formats in the config file much easier
        config = configparser.ConfigParser(interpolation=None)

        # The sample config shipped with R:soul supplies the defaults when there is no
        # config.ini but settings come from RSOUL__<SECTION>__<OPTION> environment variables
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
        env_configured = any(key.upper().startswith(ENV_PREFIX) for key in os.environ)

        if os.path.exists(config_file_path):
            config.read(config_file_path)
            config_source = config_file_path
        elif env_configured and os.path.exists(template_path):
            config.read(template_path)
            config_source = "environment variables (defaults from the bundled sample config)"
        else:
            if is_docker():
                console.print(
                    'Config file does not exist! Mount "/data" and place your "config.ini" there, '
                    f"or set options with {ENV_PREFIX}<SECTION>__<OPTION> environment variables.",
                    style="bold red",
                )
            else:
                console.print("Config file does not exist! Please place it in the working directory.", style="bold red")

            return EXIT_ERROR

        # Environment variables override config.ini
        env_overrides = apply_env_overrides(config)

        # Setup Logging
        setup_logging(config)
        logger.info(f"Configuration from: {config_source}")
        for line in env_overrides:
            logger.info(f"Environment override: {line}")

        # Validate Config
        validate_config(config)

        # Extract Config Values
        slskd_enabled = config.getboolean("Backends", "slskd_enabled", fallback=True)

        readarr_api_key = config["Readarr"]["api_key"]
        readarr_host_url = config["Readarr"]["host_url"]

        # Search Settings
        search_type = config.get("Search Settings", "search_type", fallback="first_page").lower().strip()
        search_source = config.get("Search Settings", "search_source", fallback="missing").lower().strip()
        search_sources = [search_source]
        if search_sources[0] == "all":
            search_sources = ["missing", "cutoff_unmet"]

        page_size = config.getint("Search Settings", "number_of_books_to_grab", fallback=10)

        media_mode = get_media_mode(config)
        logger.info(f"Media mode: {media_mode}")

        # Initialize Clients
        slskd = None
        if slskd_enabled:
            slskd_api_key = config["Slskd"]["api_key"]
            slskd_host_url = config["Slskd"]["host_url"]
            slskd_url_base = config.get("Slskd", "url_base", fallback="/")
            # A finite timeout so a hung slskd can't freeze the run (0 = no timeout)
            slskd_timeout = config.getfloat("Slskd", "request_timeout", fallback=30) or None
            slskd = slskd_api.SlskdClient(host=slskd_host_url, api_key=slskd_api_key, url_base=slskd_url_base, timeout=slskd_timeout)
        readarr = ReadarrAPI(readarr_host_url, readarr_api_key)
        apply_timeout(readarr.session, config.getfloat("Readarr", "request_timeout", fallback=60))

        # Initialize History Manager
        history_manager = HistoryManager(config_dir)

        # Initialize State Manager for resume functionality
        state_manager = StateManager(config_dir)

        # Initialize Context
        ctx = Context(config=config, slskd=slskd, readarr=readarr, config_dir=config_dir, history=history_manager, state=state_manager)

        # Initialize backends and orchestrator
        backends = create_backends_from_config(ctx)
        if backends:
            orchestrator = DownloadOrchestrator(backends, ctx)
            ctx.orchestrator = orchestrator
            logger.info(f"Initialized {len(backends)} backend(s): {[b.name for b in backends]}")
        else:
            logger.warning("No backends available - downloads will fail")

        # Check if we have saved state to resume (downloads, or imports awaiting confirmation)
        has_saved_state = state_manager.has_pending_state()
        saved_downloads = [i for i in state_manager.get_items() if not i.get("extra", {}).get("import_pending")]
        if has_saved_state:
            console.print(f"\nFound saved state with {len(state_manager.get_items())} pending item(s)", style="bold yellow")

        # Fetch Wanted Books. Without a monitor window, a run with saved state only resumes;
        # with one, it resumes and searches for new books in the same run.
        monitor_window = config.getint("Download Settings", "monitor_window", fallback=0)
        wanted_books = []
        download_targets = []
        if not saved_downloads or monitor_window > 0:
            try:
                for source in search_sources:
                    logger.debug(f"Getting records from {source}")
                    wanted_books.extend(get_books(ctx, source, search_type, page_size))
            except (ValueError, SearchError) as ex:
                # Neither a bad search setting nor an unreachable Readarr/Chaptarr may strand
                # downloads that are already running
                logger.error(f"Searching for new books failed: {ex}")
                if not has_saved_state:
                    return EXIT_ERROR if isinstance(ex, ValueError) else EXIT_PARTIAL
                logger.error("Continuing with the saved downloads only")
                wanted_books = []
                exit_code = EXIT_PARTIAL

            # Construct Download Targets. Books still downloading from an earlier run (hand-off)
            # are skipped here already, so their authors aren't fetched for nothing.
            in_flight = {item.get("book_id") for item in state_manager.get_tasks_for_orchestrator()}
            wanted_books = [b for b in wanted_books if b.get("id") not in in_flight]
            if len(wanted_books) > 0:
                console.print(f"\nFound {len(wanted_books)} wanted books to process", style="bold green")

                for book in wanted_books:
                    try:
                        authorID = book["authorId"]
                        author = ctx.readarr.get_author(authorID)
                        download_targets.append({"book": book, "author": author})
                    except Exception:
                        logger.exception(f"Error processing book {book.get('title', 'unknown')}")
                        continue

        # Run Workflow
        # Run if we have download targets OR if we have saved state to resume
        if len(download_targets) > 0 or has_saved_state:
            try:
                LAST_RUN.update(run_workflow(ctx, download_targets) or {})
            except Exception:
                logger.exception("Fatal error encountered during workflow execution")
                return EXIT_ERROR
        else:
            console.print("No releases wanted. Nothing to do!", style="blue")
            logger.info("No releases wanted. Exiting...")

    except KeyboardInterrupt:
        console.print("\nOperation cancelled by user", style="bold yellow")
        return 130
    except ValueError as e:
        logger.error(f"{e}")
        return EXIT_ERROR
    except Exception:
        logger.exception("An unexpected error occurred")
        return EXIT_ERROR
    finally:
        instance_lock.release()

    return exit_code


def _main_with_status() -> int:
    LAST_RUN.clear()
    code = main()
    # Docker's health check only looks at how recent this file is (liveness). The details
    # below say how the last run went, for anyone reading health.json.
    details = {
        "last_exit": code,
        "last_run_ended_at": time.time(),
        "last_run": {
            "search_failed": code == EXIT_PARTIAL,
            "grabbed": LAST_RUN.get("grabbed_count", 0),
            "failed": LAST_RUN.get("failed_download", 0),
            "still_downloading": LAST_RUN.get("still_running", 0),
            "pending_imports": LAST_RUN.get("pending_imports", 0),
            "unchecked_downloads": LAST_RUN.get("unchecked_downloads", 0),
        },
    }
    if code == EXIT_OK:
        details["last_success_at"] = details["last_run_ended_at"]
    health.write_status("idle", **details)
    return code


if __name__ == "__main__":
    sys.exit(_main_with_status())
