#!/usr/bin/env python

import sys
import argparse
import os
import configparser
import logging
from rich.console import Console

# Import from rsoul package
from readarr_api import ReadarrAPI
import slskd_api

from rsoul.config import Context, setup_logging, validate_config
from rsoul.display import print_startup_banner, console
from rsoul.utils import is_docker
from rsoul.workflow import run_workflow
from rsoul.search import get_books
from rsoul.media import get_media_mode
from rsoul.history import HistoryManager
from rsoul.state import StateManager
from rsoul.backends import create_backends_from_config
from rsoul.orchestrator import DownloadOrchestrator

logger = logging.getLogger("readarr_soul")


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
    lock_file_path = os.path.join(config_dir, ".rsoul.lock")
    config_file_path = os.path.join(config_dir, "config.ini")

    # Lock check
    if not is_docker() and os.path.exists(lock_file_path):
        console.print(f"readarr_soul instance is already running.", style="bold red")
        sys.exit(1)

    try:
        # Print banner
        print_startup_banner()

        if not is_docker():
            with open(lock_file_path, "w") as lock_file:
                lock_file.write("locked")

        # Load Config
        # Disable interpolation to make storing logging formats in the config file much easier
        config = configparser.ConfigParser(interpolation=None)

        if os.path.exists(config_file_path):
            config.read(config_file_path)
        else:
            if is_docker():
                console.print('Config file does not exist! Please mount "/data" and place your "config.ini" file there.', style="bold red")
            else:
                console.print("Config file does not exist! Please place it in the working directory.", style="bold red")

            if os.path.exists(lock_file_path) and not is_docker():
                os.remove(lock_file_path)
            sys.exit(0)

        # Setup Logging
        setup_logging(config)

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
            slskd = slskd_api.SlskdClient(host=slskd_host_url, api_key=slskd_api_key, url_base=slskd_url_base)
        readarr = ReadarrAPI(readarr_host_url, readarr_api_key)

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

        # Check if we have saved state to resume
        has_saved_state = state_manager.has_pending_state()
        if has_saved_state:
            console.print(f"\nFound saved state with {len(state_manager.get_items())} pending downloads", style="bold yellow")

        # Fetch Wanted Books. Without a monitor window, a run with saved state only resumes;
        # with one, it resumes and searches for new books in the same run.
        monitor_window = config.getint("Download Settings", "monitor_window", fallback=0)
        wanted_books = []
        download_targets = []
        if not has_saved_state or monitor_window > 0:
            try:
                for source in search_sources:
                    logger.debug(f"Getting records from {source}")
                    wanted_books.extend(get_books(ctx, source, search_type, page_size))
            except ValueError as ex:
                logger.error(f"An error occurred: {ex}")
                logger.error("Exiting...")
                sys.exit(0)

            # Construct Download Targets
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
                run_workflow(ctx, download_targets)
            except Exception:
                logger.exception("Fatal error encountered during workflow execution")
                sys.exit(1)
        else:
            console.print("No releases wanted. Nothing to do!", style="blue")
            logger.info("No releases wanted. Exiting...")

    except KeyboardInterrupt:
        console.print("\nOperation cancelled by user", style="bold yellow")
    except ValueError as e:
        logger.error(f"{e}")
        sys.exit(1)
    except Exception as e:
        logger.exception("An unexpected error occurred")
    finally:
        # cleanup lock
        if os.path.exists(lock_file_path) and not is_docker():
            try:
                os.remove(lock_file_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
