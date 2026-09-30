<p align="center">
<img src="https://raw.githubusercontent.com/insanemal/readarr_soul/refs/heads/main/rsoul.png" align="center" width="592" height="691">
</p>
<h1 align="center">R:soul</h1>
<p align="center">
  A Python script that connects Readarr with Soulseek and Stacks!
</p>

# About

**R:soul** is an automated downloader that bridges **Readarr** (for book management) with **Soulseek** (via slskd) and **Stacks** to automatically find and download missing books.

This project is a fork of [Soularr](https://github.com/mrusse/soularr) (originally for Lidarr), now fully refactored, architected with pluggable backends, and adapted for Readarr.

> **Note**: This project is **not** affiliated with Readarr. Please do not contact the Readarr team for support regarding this script.

> **Note2**: This project is **not** affiliated with Soularr. Please do not contact the Soularr team for support regarding this script.

> **Note3**: This project is **not** affiliated with Stacks. Please do not contact the Stacks team for support regarding this script.

## Quick Start

1.  **Prerequisites**:
    *   **Readarr**: Installed and running.
    *   **Slskd**: A Soulseek client (installed and running).
    *   **Stacks** (Optional): A manager for Anna's Archive downloads (installed and running).
    *   **FlareSolverr** (Optional): For bypassing DDoS-Guard on Anna's Archive.
    *   **Python 3.10+**: If running from source (or use Docker).

2.  **Configuration**:
    *   Copy `config.ini` to your data directory.
    *   Edit `config.ini` with your API keys and URLs:
        *   **[Readarr]**: Set `api_key` and `host_url`.
        *   **[Backends]**: Enable/disable backends and set priority (e.g., `priority = slskd,stacks`).
        *   **[Slskd]**: Set `api_key`, `host_url`, and `download_dir`.
        *   **[Stacks]**: Set `api_key`, `host_url`, and `download_dir` (if using Stacks).
        *   **[Stacks] FlareSolverr**: Set `flaresolverr_enabled = True` and `flaresolverr_url` if using FlareSolverr.
    *   **Path Mapping**: If running in Docker, use `readarr_download_dir` in backend sections to map internal paths to what Readarr sees.
    *   Review `[Search Settings]` to tune matching strictness.

3.  **Run**:
    *   **Docker**: `docker-compose up -d`
    *   **Source**: `python rsoul.py`

## Features

- **Pluggable Backends**: Supports multiple download sources:
  - **Soulseek** (via slskd): Peer-to-peer file sharing.
  - **Stacks**: Web-based archive search and download (searches by ISBN first, then falls back to Author-Title search).
- **DDoS-Guard Bypass**: Optional FlareSolverr integration for Anna's Archive when DDoS protection blocks requests.
- **Automated Fallback**: Tries backends in priority order until a book is found.
- **Batch Processing**: Enqueues all downloads first, then monitors them concurrently for faster processing.
- **Smart Matching**: Multi-layer validation with configurable thresholds:
  - Pre-filters: Length ratio, Jaccard token overlap, minimum word count.
  - Component matching: Author and title matched separately.
  - Metadata validation: Title matching for EPUB/MOBI/AZW3 (detects swapped author/title fields).
- **Colored Logging**: Terminal output with ANSI colors for easy parsing (auto-disabled for non-TTY).
- **Blocked Word Fallback**: Progressive query degradation when searches return zero results.
- **Resume Support**: Persists download state to disk. If interrupted, resumes monitoring on restart (reconciles with both Slskd and Stacks).
- **Import Management**: Automatically imports successful downloads into Readarr, handling multi-backend path mappings.
- **Docker Support**: Ready for containerized deployment.

## Configuration Reference

### [Backends]

| Option | Default | Description |
|--------|---------|-------------|
| `priority` | `slskd` | Comma-separated list of backends in order (e.g., `slskd,stacks`) |
| `slskd_enabled` | `True` | Enable Soulseek backend |
| `stacks_enabled` | `False` | Enable Stacks backend |

### [Stacks]

| Option | Default | Description |
|--------|---------|-------------|
| `api_key` | - | Stacks API key |
| `host_url` | `http://localhost:7788` | Stacks server URL |
| `download_dir` | - | Local path to Stacks downloads |
| `readarr_download_dir` | (same as download_dir) | Path as seen by Readarr |
| `search_timeout` | 30 | Timeout for Anna's Archive searches (seconds) |
| `min_match_ratio` | 0.6 | Minimum title/author match score |
| `flaresolverr_enabled` | `False` | Enable FlareSolverr for DDoS bypass |
| `flaresolverr_url` | `http://localhost:8191/v1` | FlareSolverr API endpoint |

### [Search Settings]

| Option | Default | Description |
|--------|---------|-------------|
| `media_mode` | `ebook` | `ebook`, `audiobook`, or `both` (see [Audiobooks & Chaptarr](#audiobooks--chaptarr)) |
| `preferred_formats` | `epub,azw3,mobi` | Preferred ebook formats in priority order |
| `audiobook_formats` | `m4b,mp3` | Preferred audiobook formats in priority order |
| `audiobook_min_size_mb` | 10 | Skip audiobook folders smaller than this (filters out samples) |
| `audiobook_browse_limit` | 5 | Best-matching audiobook folders to browse per search |
| `minimum_filename_match_ratio` | 0.7 | Minimum fuzzy match ratio for filenames |
| `min_length_ratio` | 0.4 | Reject if string lengths differ too much |
| `min_jaccard_ratio` | 0.25 | Minimum word overlap ratio |
| `min_word_overlap` | 2 | Minimum number of matching words |
| `min_title_jaccard` | 0.3 | Minimum Jaccard for title component |
| `min_author_jaccard` | 0.5 | Minimum Jaccard for author component |
| `max_search_fallbacks` | 5 | Max fallback attempts for blocked words |
| `search_type` | first_page | Options: `first_page`, `incrementing_page`, `all` |
| `search_source` | missing | Options: `missing`, `cutoff_unmet`, `all` |

### [Postprocessing]

| Option | Default | Description |
|--------|---------|-------------|
| `match_ratio_exact` | 0.8 | Exact string match threshold |
| `match_ratio_normalized` | 0.85 | Normalized (lowercase, alphanumeric) threshold |
| `match_ratio_word` | 0.7 | Word-based similarity threshold |
| `match_ratio_loose` | 0.85 | Loose match (brackets removed) threshold |
| `match_ratio_jaccard` | 0.5 | Jaccard token similarity threshold |
| `skip_validation` | `False` | Skip all metadata validation (let Readarr handle it) |

## Audiobooks & Chaptarr

R:soul also works with [Chaptarr](https://github.com/Chaptarr/chaptarr), a Readarr fork that manages ebooks and audiobooks in one instance. Chaptarr exposes a Readarr-compatible API, so the `[Readarr]` section simply points at your Chaptarr instance.

Set `media_mode` in `[Search Settings]`:

| Mode | Wanted list | What gets downloaded |
|------|-------------|----------------------|
| `ebook` (default) | Ebooks only (Chaptarr); everything (Readarr) | One file per book, as before |
| `audiobook` | Audiobooks only (Chaptarr); everything (Readarr) | The whole matching folder of audio files |
| `both` | Everything | Decided per book from Chaptarr's `mediaType` field |

`ebook` and `audiobook` also work with a plain Readarr instance that holds only one media type. `both` needs Chaptarr; books without a `mediaType` field are treated as ebooks.

How audiobooks are handled:

- **Matching** uses the folder name (e.g. `Author - Title [Narrator]`), since chapter files are usually named `01.mp3`, `02.mp3`, ... Bracketed parts and release noise such as bitrates or "Unabridged" are ignored. A single-file audiobook (`.m4b`) can also match on its filename.
- **Grabbing**: the best-matching folders are browsed so every audio file in the folder is queued, not just the ones that appeared in the search results. If not all files can be queued, the download is cancelled rather than importing an incomplete book.
- **Import**: files are moved to `<download_dir>/rsoul_audiobooks/<Author>/<Title>/` and imported with one `DownloadedBooksScan` per book folder. Ebook metadata validation is not applied to audio files.
- **Time limits** are progress-based (see [Download timeouts](#download-timeouts)), so a large audiobook from a slow but steady peer is not cancelled.
- **Not handled yet**: audiobooks split across subfolders (`CD1`, `CD2`, ...). Only the files directly in the matched folder are downloaded.

## Download timeouts

Downloads are judged on their own progress, and all of them run in parallel:

| Option | Section | Default | Gives up when |
|--------|---------|---------|---------------|
| `stall_timeout` | `[Download Settings]` | 1800 | No new data arrived for this many seconds while transferring |
| `remote_queue_timeout` | `[Slskd]` | 3600 | The download waited this long in the peer's upload queue |
| `max_download_time` | `[Download Settings]` | 86400 | Overall safety cap, including queue time |

Waiting for your own slskd download slots ("Queued, Locally") doesn't count against any limit except the overall cap. `0` disables a limit. The old `stalled_timeout` (a fixed total time) is no longer used.

### Hand-off between runs

By default a run waits until every download has finished, so one slow download holds up the next search. Set `monitor_window` in `[Download Settings]` (seconds) to cap how long a run monitors: unfinished downloads keep going in slskd, their progress timers are saved, and the next run continues monitoring them while also searching for new books. Books still downloading are not searched again.

## Resume Functionality

R:soul persists its download queue to `grab_list_state.json`. If the application is interrupted:

1. On restart, it detects saved state.
2. Reconciles with each enabled backend (Slskd, Stacks) to check current status.
3. Resumes monitoring active downloads.
4. Triggers imports upon completion.

State is automatically cleaned up after successful imports.

## Status

Active Development. The architecture recently shifted to a modular backend system to support multiple sources.

## Support

Join the Discord: https://discord.gg/mwX4dMSQGH
