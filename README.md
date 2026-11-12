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
    *   **Readarr** or **Chaptarr**: Installed and running (for audiobooks, see [Audiobooks & Chaptarr](#audiobooks--chaptarr)).
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

## Configuring with environment variables

Every option can also be set with an environment variable, which is handy in Docker Compose or TrueNAS custom apps:

```
RSOUL__<SECTION>__<OPTION>=value
```

The section name is case-insensitive, with spaces written as underscores: `RSOUL__READARR__API_KEY` sets `api_key` in `[Readarr]`, and `RSOUL__SEARCH_SETTINGS__MEDIA_MODE` sets `media_mode` in `[Search Settings]`.

- **Environment variables win** over `config.ini`.
- **`config.ini` becomes optional.** If there is none in the data folder, the sample `config.ini` shipped with R:soul provides the defaults and the environment variables fill in the rest. At minimum set the Readarr/Chaptarr and slskd `api_key` and `host_url`; R:soul stops with a clear error if an API key is still the sample placeholder.
- **Typos are reported, not ignored silently.** A variable naming a section that doesn't exist is skipped with a log message. The log lists every value taken from the environment, with API keys and passwords hidden.

```yaml
services:
  rsoul:
    image: mephistopheles1/rsoul:latest
    restart: unless-stopped
    user: "1000:1000"
    environment:
      - SCRIPT_INTERVAL=300
      - RSOUL__READARR__HOST_URL=http://192.168.1.10:8787
      - RSOUL__READARR__API_KEY=your-readarr-or-chaptarr-key
      - RSOUL__SLSKD__HOST_URL=http://192.168.1.10:5030
      - RSOUL__SLSKD__API_KEY=your-slskd-key
      - RSOUL__SLSKD__DOWNLOAD_DIR=/downloads
      - RSOUL__SLSKD__READARR_DOWNLOAD_DIR=/downloads
      - RSOUL__SEARCH_SETTINGS__MEDIA_MODE=audiobook
    volumes:
      - /path/to/slskd/downloads:/downloads
      - /path/to/config/dir:/data   # still needed: resume state and history live here
```

Values in the environment are visible to anyone who can inspect the container, the same as in `config.ini`.

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
| `audiobook_browse_limit` | 5 | Most audiobook folders tried, best match first, until one can be listed in full |
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
| `skip_validation` | `False` | Skip all metadata validation, including the audiobook tag check (let Readarr handle it) |
| `audiobook_tag_check` | `reject` | What to do when a downloaded audiobook's tags name another book: `reject`, `warn` or `off` |
| `import_failure_retry_days` | 7 | Days a peer is skipped for a book after a failed import (`0` = for good) |

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

- **Matching** uses the folder name (e.g. `Author - Title [Narrator]`), since chapter files are usually named `01.mp3`, `02.mp3`, ... Bracketed parts and release noise such as bitrates or "Unabridged" are ignored. A book can also match on its own file names when the folder is not named after it (`Audiobooks\\Author - Title.m4b`, or `Author - Title - 01.mp3`, `... - 02.mp3`). Then only the files sharing that name are downloaded, apart from evident chapter and part numbers (`- 02`, `02`, `Part 2`, `CD2`; not a bare `Title 2`), because the folder around them (`Audiobooks`, an author folder) usually holds other books too.
- **The author must appear in the folder path** (e.g. `Brandon Sanderson\The Final Empire` or `Brandon Sanderson - The Final Empire`). Titles repeat across authors, so a folder named only after the title is not trusted.
- **Title forms**: besides the full title, a shorter form can match, such as "The Final Empire" for "Mistborn: The Final Empire". Ebook matching is unchanged.
- **Exact title required**: one part of the folder's own name (split at " - " and commas; a parent folder doesn't count) must be the title. In front of it, that part may hold the author, the series name (from Chaptarr), numbers and words like "Book"; behind it only the author, recording words like "Unabridged", a year, or the series together with the book's number. So "Frontlines 2 - Lines of Departure" matches *Lines of Departure*, but "The Saints of Salvation" and "Salvation Lost" don't match *Salvation*. A number right behind the title makes it another book too: "Dune 2" does not match *Dune*, while "Dune Book 1" and "Dune 1965" do, and in file names so do evident chapter numbers ("Dune 01", "Dune - 2", "Dune Part 2").
- **Series named after their first book**: what follows the title must not name another book. "Red Rising 02 - Golden Son" is *Golden Son*, not *Red Rising*; "Red Rising Trilogy" and "Red Rising Saga" are the whole set. When Chaptarr reports the book's position ("Red Rising Saga #1"), a "Book 2" or "#2" anywhere in the name rules the folder out. The price: "Title - Narrator Name" is not matched unless it says "read by" or "narrated by", or the narrator is in brackets.
- **Only complete folders are downloaded.** Candidates are tried best match first; each is listed in full on the peer's share, and every audio file in it is queued. A folder that can't be listed is skipped, because search results only show some of its files. If not every file can be queued, the download is cancelled and the next candidate is tried.
- **Collections are not the book**: "Red Rising (Books 1-6)", "Red Rising [Complete Series]" and "Red Rising (Trilogy)" are turned away, also when the collection is only named in brackets. Brackets with ordinary details ("(Unabridged)", "(2014)", "[narrator]") or the series with this book's number are fine.
- **A folder named after the book may be a shelf.** `Dune\Dune.m4b` next to `Dune Messiah.m4b` and `Children of Dune.m4b`: when a file of at least 100 MB carries the title itself and the others are named differently, only that file (with its own parts) is taken. If no file carries the title and all are book-sized and named differently, the folder is skipped. Folders of chapters, numbered or named, are taken whole as before.
- **The numbering must be complete.** "Dune - Part 2 of 4" alone, chapters 02 and 03 without 01, or a gap in the sequence mean the peer doesn't have the whole book, and the folder is skipped. Only plain numberings are judged (every file numbered, below 100).
- **Chapters spread over several formats are skipped.** A folder with `01.mp3` next to `02.m4a` can't be downloaded completely in one format, so it is left alone. A second complete copy in another format (an `.m4b` next to the mp3 chapters) is no obstacle.
- **Multi-disc audiobooks are skipped.** Folders named like `CD1`, `Disc 2` or `Part 3` are one part of a larger book, so they are not downloaded.
- **Import**: files are moved to `<download_dir>/rsoul_audiobooks/<Author>/<Title>/` and imported with one `DownloadedBooksScan` per book folder. Chaptarr decides from the file extension that they are an audiobook. Ebook metadata validation is not applied to audio files. Use formats Chaptarr can import (e.g. `m4b`, `mp3`, `m4a`, `flac`); R:soul warns at startup about others.
- **Failed downloads are cleared away**: whatever a failed download left behind (e.g. finished chapters) is moved to `<download_dir>/failed_downloads/`, so it can't mix with a later attempt. The same happens to a saved download that can no longer be resumed; its remaining transfers are cancelled in slskd.
- **Audio tags are checked before import.** Once an audiobook is downloaded, the album and title tags of its files (first, middle and last file) are compared with the wanted book. The check has three outcomes: the tags confirm the book, they confirm **another** book, or they are inconclusive. Only a confirmed other book has consequences: the download is moved to `<download_dir>/failed_imports/` instead of being handed to Chaptarr, and that peer is not used for the book again. `audiobook_tag_check` under `[Postprocessing]` sets this to `reject` (default), `warn` (log only) or `off`.
  - *Another book* means: a tag names another book by the same author (from Chaptarr's list of the author's books), or says collection or another position in the series than Chaptarr gives.
  - *The series is not another book.* Albums often hold the series, and many series are named after their first book: "Red Rising" on the files of *Golden Son* is inconclusive, as are "Red Rising Saga" and "Red Rising Book 2". R:soul recognises series from Chaptarr's data for the author; where Chaptarr lists no series for the author at all, an album alone is never proof.
  - *The title tag outweighs the album* for a single-file book: album "Dune" with title "Dune Messiah" is *Dune Messiah*. For a book in several files the title tags are chapter names; they only count when most of the sampled files name another book ("Dune Messiah, Chapter 1"). "Book Two" in a chapter name is a part of the novel.
  - *Everything else is inconclusive* and imported as before: no tags, a publisher or anything else R:soul can't place ("Audible"), the author as album, the title with extras ("Dune Disc 1"), a title in another language.
  - The dry run can't show this check, because it downloads nothing. For the first real downloads you can set `warn` and read what the check would have done in the log.
- **One title, one download.** Readarr/Chaptarr can hold two records for the same book. A wanted book is skipped (with a warning naming the record) when another wanted record with the same author and title is already being downloaded, or when Chaptarr already has files for another record of that title and media type; otherwise the same book would be fetched again every time its page comes round. Unmonitor or remove the duplicate record in Chaptarr to make the warning go away. In an ebook-only or audiobook-only run, records of the other media type are left out as well.
- **Books from one folder of a peer download side by side.** Peers often keep an author's books as single files in one folder. R:soul's own downloads may share the local folder that slskd creates for it, because each only moves the files it expects by name. Downloads of other tools in that folder (Soularr, manual ones) still block it.
- **What Chaptarr knows is used for matching.** The author's other titles tell a shelf from a folder of chapters whatever the file sizes: `Dune\Dune Messiah.m4b` is not *Dune*. Narrator names, where Chaptarr has them, are no longer mistaken for another title ("Red Rising - Tim Gerard Reynolds"). And when Chaptarr gives the book's duration, a download below 8 kbit/s over that duration is skipped as a fragment. Each of these is simply left out when Chaptarr doesn't provide the information.
- **Rejected imports**: if Readarr/Chaptarr finishes the import without taking the files, they are moved to `<download_dir>/failed_imports/` and the peer is skipped for this book for `import_failure_retry_days` (`[Postprocessing]`, default 7; `0` = for good), so the next run tries another peer instead of downloading the same files again. The pause is temporary because the cause can also be on Readarr/Chaptarr's side (path mapping, permissions, import settings). The same pause applies when R:soul itself can't find or move the downloaded files. Only files that are themselves the wrong book (an ebook whose metadata, or an audiobook whose tags, belong to another book) exclude their peer for good, as do entries written by earlier versions. After fixing such a problem, delete the affected entries from `failed_downloads.json` to retry at once.
- **Time limits** are progress-based (see [Download timeouts](#download-timeouts)), so a large audiobook from a slow but steady peer is not cancelled.

**Sharing slskd with other tools.** R:soul removes only its own records from slskd's transfer list once a download has finished or failed; it no longer clears every finished transfer, so tools such as Soularr can share the same slskd. The reverse is handled too:

- **Transfers cleared by another tool.** Soularr clears all finished transfers after each of its runs, R:soul's included. A cleared file that is in the download folder at its full size counts as finished; one that isn't counts as failed, so R:soul doesn't wait for it until `max_download_time`.
- **Unfinished downloads in the same folder.** R:soul won't start a download into a local folder that any unfinished download is still writing into. When Soularr gives up on an album, it deletes that album's whole local folder.

**slskd download folder layout.** R:soul expects slskd's default layout, where each file lands in `<download_dir>/<name of the peer's folder>/`, i.e. `transfers.download.destination.subdirectory` left at `${SOURCE_DIRECTORY}`. Downloads from different peers whose folders have the same name share one local folder, and slskd renames a new file if its name is taken. R:soul therefore won't start a download whose files already exist in, or are still downloading into, the same local folder; it retries on a later run.

## Dry run

Matching books by the names people give their folders is guesswork, so before letting R:soul download, look at what it would pick for your own wanted list:

```
python rsoul.py --dry-run
```

In Docker, set `RSOUL__GENERAL__DRY_RUN=true` (or `dry_run = True` under `[General]` in `config.ini`), let one run finish, read the report, then remove the setting and restart the container.

A dry run fetches the wanted list, searches, matches and lists candidate folders on the peers' shares, exactly like a normal run, and then stops. For each book it logs, and writes to `dry_run_report.txt` in the data folder:

- what it **would download**: the peer, the folder, the files, their size, and whether the folder name or the files' own names matched;
- the **other matching folders**, in the order they would be tried;
- what it **turned away** and why (another title, another author, a disc folder, ...), with the folder names.

- matching folders that **could not be used**, each with its reason: the peer didn't answer, the book is incomplete, the local folder is in use, and so on.

It is a search preview that downloads nothing: no download is queued, no saved download is resumed, nothing is imported, no book is unmonitored and no peer is recorded as failed. The page counter of `search_type = incrementing_page` and the saved-download file are only read, so the next real run sees the same books, and a damaged file is reported but not repaired. What a dry run does write is the report, the lock file and a `last_dry_run` entry in `health.json`; the record of the last real run stays as it was.

Two things to keep in mind:

- **It stays on.** With `SCRIPT_INTERVAL` set, every scheduled run is a dry run until you remove the setting and restart the container.
- **It is not an exact preview of the next run.** Soulseek answers differ from one search to the next. And with `monitor_window = 0`, a real run that still has saved downloads resumes those and searches for nothing new; the report says so at the top when that applies.

## Download timeouts

Downloads are judged on their own progress, and all of them run in parallel:

| Option | Section | Default | Gives up when |
|--------|---------|---------|---------------|
| `stall_timeout` | `[Download Settings]` | 1800 | No new data arrived for this many seconds while transferring |
| `queue_timeout` | `[Download Settings]` | 3600 | The download waited this long in the peer's upload queue |
| `max_download_time` | `[Download Settings]` | 86400 | Overall safety cap, including queue time |

Progress means new bytes (slskd) or a higher percentage (Stacks, which reports only a percentage). `0` disables a limit. Two situations pause the stall and queue timers, because they aren't the peer's fault:

- waiting for your own slskd download slots ("Queued, Locally");
- slskd not answering (for example while it restarts).

The overall cap still applies in both cases. Timers start when a download is queued.

Imports run on a background thread, so the other downloads keep being monitored while Readarr or Chaptarr imports a finished one. A download only counts as successful in the run summary once its import succeeded; an import that is still running when the run ends is followed up on the next run (see [Reliability](#reliability)).

### Hand-off between runs

By default a run waits until every download has finished, so one slow download holds up the next search. Set `monitor_window` in `[Download Settings]` (seconds) to cap how long a run monitors: unfinished downloads keep going in slskd, their progress timers are saved, and the next run continues monitoring them while also searching for new books. Books still downloading are not searched again.

Each run can add up to `number_of_books_to_grab` new downloads. To keep slow downloads from piling up, set `max_active_downloads`: the most downloads running at once, including the ones handed over from earlier runs. Keep `number_of_books_to_grab` at or below it: with `search_type = incrementing_page`, wanted books that don't fit under the cap are skipped until the page comes round again.

## Reliability

- **Request timeouts.** Requests to slskd and Readarr/Chaptarr give up after `request_timeout` seconds (`[Slskd]` default 30, `[Readarr]` default 60), so a hung server can't freeze a run. The download simply continues on the next poll or run.
- **Imports that outlast a run.** A run waits up to `import_poll_timeout` seconds (`[Readarr]`, default 600) for an import. If the import is still running then, or Readarr/Chaptarr couldn't be reached to start it, the book stays in the saved state with its staged files, and the next run checks the import again or submits it once more (up to 3 times). After 24 hours without a result, or after 3 submissions, R:soul gives up and moves the files to `failed_imports/` for a manual import, like any other failed import.
- **A damaged state file isn't lost.** Entries in `grab_list_state.json` that R:soul can't use are skipped, the other downloads are still resumed, and the file is kept as `grab_list_state.json.damaged-<date>` for troubleshooting.
- **Outages don't erase saved downloads.** If slskd can't be reached when a run starts, saved downloads are kept for the next run instead of being given up.
- **A failed search isn't mistaken for an empty wanted list.** If Readarr/Chaptarr can't be reached, or a search setting is invalid, saved downloads are still followed up and the run ends with exit code `2`.
- **One R:soul per data folder.** On filesystems that support OS file locks (local disks and ZFS datasets do), a lock on `.rsoul.lock` prevents two R:soul processes, or two containers sharing a data folder, from running at once. The lock is released automatically when R:soul exits, even after a crash. Where locking isn't supported, R:soul logs a warning and runs without it.
- **Books stay wanted unless nobody shares them.** With `remove_wanted_on_failure = True`, a book is not unmonitored when it couldn't be searched for at all (slskd unreachable, every search failing) or was found but couldn't be used right now: the peer's folder couldn't be listed, the local folder is in use, the download couldn't be queued, or there were more matching folders than `audiobook_browse_limit` allowed to look at. It is unmonitored when nothing matched, or every match was unsuitable for good (incomplete, too small, another book).
- **Settings are checked at startup.** Every number and every True/False setting is checked, so a typo stops R:soul at once instead of making every search fail in the middle of a run. In detail: Time limits must be `0` or a positive number (decimals are fine), `monitor_window` and `max_active_downloads` must be whole numbers, and `number_of_books_to_grab` must be at least 1. Values like `-100`, `nan` or a `monitor_window` of `2.5` stop R:soul with a clear error instead of silently disabling a limit or failing later. So does a `download_dir` that is still the sample placeholder (`/path/to/...`); a placeholder `readarr_download_dir` falls back to `download_dir` with a warning.
- **Exit codes.** Each run exits with `0` (finished), `1` (configuration or startup problem, another instance running, or a crash) or `2` (saved downloads were handled, but searching for new books failed). In Docker, `run.sh` logs any non-zero exit and stops promptly on `docker stop`.
- **Health check.** R:soul writes `health.json` to its data folder at the start and end of each run, and about once a minute while it searches, monitors downloads or waits for imports. The Docker image's health check reports *unhealthy* when that file is older than `max(900, 2 × SCRIPT_INTERVAL + 120)` seconds (override with `RSOUL_HEALTH_MAX_AGE`), meaning R:soul is stuck or not running. Nothing is restarted automatically. This is a liveness check only: whether slskd and Chaptarr work is recorded separately in `health.json` under `last_run` (failures, pending imports, a failed search, downloads that couldn't be checked).

## Upgrading from earlier versions

- **Timeouts changed.** `stalled_timeout` (a fixed total time per download) and `remote_queue_timeout` are no longer used; R:soul logs a warning if they are still in your `config.ini`. Remove them and, if needed, set `stall_timeout`, `queue_timeout` and `max_download_time` in `[Download Settings]`. `remote_queue_timeout` was never actually applied before, so it is ignored rather than switched on: the old sample value of 300 would otherwise cancel most queued downloads after five minutes.
- **Downloads can now run longer.** A download that keeps receiving data is no longer cancelled after an hour; only the 24-hour cap applies.
- **Resume drops unrecoverable downloads.** Saved downloads that can't be found in slskd or on disk are removed from the resume state instead of staying there forever.
- **The run summary is stricter.** A download whose import fails is now reported as failed instead of successful.
- **Busy local folders are left alone.** A download is not started if files with the same names are already in, or still downloading into, its local slskd folder.
- **Other slskd transfers are left alone.** R:soul used to clear every finished transfer in slskd at the end of each run; it now removes only its own.
- **Configuration can come from environment variables** (`RSOUL__<SECTION>__<OPTION>`), and `config.ini` is optional when they are used.
- **Page counters.** With `search_type = incrementing_page`, the cutoff-unmet list now keeps its own page counter (`.current_page_cutoff_unmet.txt`). With `search_source = all` the two lists used to share one counter, so each saw only every other page.
- **Everything else is opt-in.** `media_mode` defaults to `ebook`, `monitor_window` to `0` and `max_active_downloads` to `0` (no limit).

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
