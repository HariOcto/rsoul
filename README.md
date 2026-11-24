<p align="center">
<img src="rsoul.png" align="center" width="296" height="345" alt="R:soul logo">
</p>
<h1 align="center">R:soul — audiobook fork</h1>

R:soul connects Readarr or Chaptarr to slskd, with an optional Stacks backend for ebooks. It searches wanted books, tracks downloads across restarts, checks their contents, and submits imports for the intended book.

This repository is a fork of **R:soul**, the ebook downloader that bridges Readarr with Soulseek and Stacks (GitHub shows the original repository as "forked from" at the top of this page). R:soul is itself a fork of [Soularr](https://github.com/mrusse/soularr) by mrusse, originally written for Lidarr. All credit for the original design and code goes to those projects; see [Credits and license](#credits-and-license).

> **0.1.0-beta.1 is a pre-release** of this fork, which adds audiobook support for Chaptarr to the ebook-only R:soul: folder and file matching for audiobooks, verified imports that survive restarts and lost API responses, and operator tools for imports that need review. It has been run against a real Chaptarr/slskd setup, but not across many configurations. Back up your `/data` directory before upgrading. See [CHANGELOG.md](CHANGELOG.md) for what is new.

## Installation and upgrade

Use Python 3.11 or 3.12, or build the supplied Dockerfile.

```bash
python -m pip install -r requirements.txt
python rsoul.py --version          # R:soul 0.1.0-beta.1 (pre-release)
python rsoul.py --config-dir /path/to/config
```

For a local container image, run from this source directory:

```bash
docker build -t rsoul:0.1.0-beta.1 .
```

Stop the old rsoul container before replacing it. Back up its persistent `/data` directory and preserve the same download mount. Keep API keys and existing path mappings. Start one instance with the new image. Do not run old and new versions concurrently against the same data volume. An OS lock is required; inability to acquire or support it stops mutations.

Existing download and import state is retained and migrated conservatively. A task from an earlier draft with insufficient evidence of completeness may need manual review. Old source-ban entries remain on disk but cannot silently ban a different author, medium, or backend under the new scoped queries.

Legacy pending imports are never automatically submitted again, even when an old record has no command ID and says zero submissions: that does not prove the server never accepted a request. They can still reconcile to success when their saved evidence accounts for the complete import. Otherwise they remain protected for review.

## Configuration

Copy `config.ini` into the persistent configuration directory and edit the Readarr/Chaptarr and slskd connection settings. Alternatively, supply environment variables; the bundled sample provides defaults when no configuration file exists.

Every setting supports:

```text
RSOUL__<SECTION>__<OPTION>=value
```

For example, `[Search Settings] media_mode` becomes `RSOUL__SEARCH_SETTINGS__MEDIA_MODE`. Environment settings override the configuration file. API keys, passwords and token values are hidden in normal configuration logs.

A minimal audiobook environment is:

```text
RSOUL__READARR__HOST_URL=http://your-chaptarr:8787
RSOUL__READARR__API_KEY=your-key
RSOUL__READARR__IMPORT_METHOD=targeted
RSOUL__SLSKD__HOST_URL=http://your-slskd:5030
RSOUL__SLSKD__API_KEY=your-key
RSOUL__SLSKD__DOWNLOAD_DIR=/downloads
RSOUL__SLSKD__READARR_DOWNLOAD_DIR=/downloads/slskd/downloads
RSOUL__SEARCH_SETTINGS__MEDIA_MODE=audiobook
RSOUL__DOWNLOAD_SETTINGS__MAX_ACTIVE_DOWNLOADS=10
RSOUL__DOWNLOAD_SETTINGS__MONITOR_WINDOW=1800
RSOUL__DOWNLOAD_SETTINGS__QUEUE_TIMEOUT=7200
SCRIPT_INTERVAL=300
```

The two download paths deliberately may differ. `download_dir` is the path **inside rsoul**; `readarr_download_dir` is the path **inside Chaptarr** to the same physical files. Keep your existing mapping if it already resolves correctly. Mount the actual completed slskd directory at `download_dir`, and persist `/data` for journals, queue state, history and diagnostics.

slskd must use its normal `${SOURCE_DIRECTORY}` completed-download subdirectory layout. R:soul protects other tools' transfers and existing files; it does not assume ownership of every file in a shared author folder.

### Media and imports

| Setting | Default | Meaning |
|---|---|---|
| `media_mode` | `ebook` | `ebook`, `audiobook`, or `both`; `both` uses the API's media discriminator, with missing values treated as ebook |
| `preferred_formats` | `epub,azw3,mobi` | Ebook format priority |
| `audiobook_formats` | `m4b,mp3` | Audiobook format priority |
| `audiobook_min_size_mb` | `10` | Minimum total candidate size |
| `audiobook_min_bitrate_kbps` | `8` | Candidate size/runtime sanity floor when catalog duration is known; zero disables |
| `audiobook_duration_tolerance` | `0` | Optional fraction of permitted downloaded/runtime drift; zero disables the extra lookup and check |
| `audiobook_browse_limit` | `5` | Maximum complete directory candidates inspected per search result set |
| `[Readarr] import_method` | `targeted` in new sample | Explicit `ManualImport` book/edition selection; existing `scan` settings remain supported with a warning |
| `[Readarr] disable_sync` | `False` | Pauses both new imports and pending import submissions; downloaded files remain tracked |
| `[Readarr] import_poll_timeout` | `600` | Maximum seconds spent awaiting an import in a run; unfinished work is retained |

Targeted import never falls back silently to a scan. Edition selection uses compatible explicit media information and the existing conservative fallback for Chaptarr records that lack a usable edition format. If several eligible audio editions remain, complete narrator identities from downloaded tags can resolve a unique choice; a shared surname is insufficient, all known cast identities must match, and a unique monitored edition retains priority. Missing `editionId` on bookfile responses is supported when the record's identity, medium, and complete source/output proof are otherwise sufficient.

### What counts as an imported book

Before staging, R:soul saves a transaction containing the intended author/work identity and every input's path, size and SHA-256 digest. The exact import request, pre-import bookfile IDs and submission attempt are saved before the API call; the command receipt is saved immediately afterwards.

A successful command response is insufficient. Verification requires all manifest inputs to be accounted for by new files associated with the intended record, using exact staged-source to output history or hashes of readable library outputs. Pre-existing files, a coincidental new file, a changed book ID, or files merely disappearing from staging do not establish success.

MP3-to-M4B conversion is accepted only when server history links **every input** to the committed output and the output record/path/size agree. If a server cannot provide that evidence and the output cannot otherwise be proven, the import remains pending or needs review. It is not marked successful by file count alone.

A lost POST response has an unknown outcome. R:soul checks existing evidence and does not blindly submit another command. Interrupted staging replays its saved manifest without overwriting collisions. Active commands do not expire into destructive cleanup. Partial imports, changed identities and uncertain outcomes retain their files and journals.

These checks are evidence checks, not a guarantee about the literary completeness of an arbitrary recording. Audio container validation checks every selected file's stream information and metadata; it does not decode every audio sample or compare spoken content with the printed book.

### Audiobook matching and recording choices

The candidate must identify the intended author and title. Full directory inspection is required before downloading a multi-file audiobook. Known sibling titles are isolated regardless of file size. A generic `Book 1` label cannot turn another known work into the requested title; series exceptions require catalog evidence. Raw `sample`, `preview`, `prologue`, excerpt and similar fragment evidence survives title normalization.

Unambiguous, fully numbered chapter sets are checked for missing starts and gaps, including ordinary sequences above 99; explicit part totals are also checked. The check is bounded at 10,000 parts and preserves recognized multi-disc numbering conventions. Mixed numbered and unnumbered names may remain inconclusive, and numbering alone does not prove a recording is complete. Cross-format chapter fragments are rejected; a separately complete alternative recording may still be selected. A failed candidate does not hide a different valid directory from the same peer.

Every selected audio file is structurally inspected before import. Valid audio without descriptive tags is distinct from unreadable or invalid audio. Existing `audiobook_tag_check=reject|warn|off` controls title-tag identity checks; stream integrity and explicit recording policies remain enforced.

Optional recording policies live under `[Search Settings]`:

| Option | Default | Values |
|---|---|---|
| `audiobook_languages` | empty | Comma-separated language codes/names; empty is unrestricted |
| `audiobook_narrators` | empty | Pipe-separated full narrator names; empty is unrestricted |
| `audiobook_abridged` | `allow` | `allow` or `reject` |
| `audiobook_booktrack` | `allow` | `allow` or `reject` |
| `audiobook_dramatized` | `allow` | `allow` or `reject` |
| `audiobook_unknown_recording` | `warn` | `allow`, `warn`, or `reject` when a restricted field is unknown after download |

Policies use evidence from the actual candidate or downloaded files. A narrator or language on the wanted catalog record is not proof that the downloaded recording matches it. Defaults preserve existing recording preferences. Unknown metadata with strict rejection is retained for review.

### Searches and scheduling

| Option | Default | Purpose |
|---|---|---|
| `number_of_books_to_grab` | `10` | Eligible books attempted per run |
| `wanted_scan_pages` | `5` | Bounded pages scanned per wanted-list run |
| `wanted_backlog_limit` | `500` | Retained-candidate bound per list/media queue, with an effective minimum of the requested page size |
| `search_query_budget` | `6` | Distinct queries per book |
| `search_min_interval` | `1` second | Minimum interval between search submissions |
| `search_rate_limit_max_wait` | `60` seconds | Maximum individual wait before a search submission; longer deadlines stay saved and defer that search |
| `not_found_retry_days` | `1` | Cooldown after a completed search with no acceptable result |
| `redownload_after_days` | `30` | Avoid immediately repeating a verified imported title that reappears as missing |
| `max_active_downloads` | `0` | Concurrent task cap, including resumed/unchecked work; zero is unlimited |
| `monitor_window` | `0` | Seconds monitoring per run; zero waits for all active downloads |
| `queue_timeout` | `3600` | Peer queue limit; local queuing is tracked separately |
| `stall_timeout` | `1800` | Limit without transferred-byte progress |
| `max_download_time` | `86400` | Overall known-download time limit |
| `completed_file_settle_time` | `30` | Grace for a completed file's final rename before size/regular-file verification |

Eligibility is checked before limiting attempts or concurrent transfers. Duplicate records, explicit exclusions and cooldowns do not consume the batch limit. The wanted queue stores unattempted records and the page cursor together, so a full download queue cannot silently skip the rest of a page. `all` and `incrementing_page` scan bounded rotating pages; `first_page` retains its page-one behavior. Existing page counters seed the new queue on first use.

Searches favor surname plus distinctive title terms. Download polling continues between searches and while waiting on search pacing, so completion/import and timeout processing need not wait for the entire batch. Confirmed asynchronous peer rejection can try another retained candidate within a bounded attempt budget. Unknown enqueue ownership is reconciled first.

HTTP 429 and incomplete/outage searches are deferred service errors. Their outcomes are not recorded as a completed negative search. `Retry-After` (seconds or HTTP date), bounded exponential fallback and jitter are shared across books and persisted across runs. `search_min_interval` is a client pacing control, not permission to exceed the service's requested delay.

Audiobook searches with a known catalog author ID require an available, valid author bibliography. A catalog outage is deferred rather than treated as evidence that no sibling titles exist. Pending title-tag validation also waits for that catalog under the default reject policy.

The production import coordinator submits newly completed downloads while earlier commands are pending, then checks them against fixed per-operation deadlines. One worker serializes import mutations. Existing accepted imports receive one nonwaiting reconciliation check at startup; synchronous API and validation steps still take their normal time.

### Catalog exclusions

The catalog exclusion filters do not delete records or change Chaptarr monitoring. Default failure handling also preserves wanted status (`remove_wanted_on_failure=False`); enabling that existing option explicitly changes failure behavior. Clean up unwanted records in Chaptarr, or use explicit local exclusions:

```ini
[Search Settings]
excluded_book_ids = 6328,6444,6631
excluded_titles = Frontlines: Requiem: The Graphic Novel|The Commonwealth Saga
excluded_publication_types =
```

`excluded_titles` uses exact normalized titles. `excluded_publication_types` uses actual `publicationType`/`bookType` values supplied by your server; missing metadata is not guessed from a title. These are examples, not defaults. An anthology, graphic adaptation or collection may be intentional, so choose the records you want excluded.

### Optional operator features

`[Postprocessing] failed_retention_days=0` keeps quarantine retention off. When enabled, age is measured from R:soul's quarantine marker, not a moved folder's old modification time. Structured saved ownership protects referenced folders, including Unicode paths and unfinished discards. Missing/untrustworthy state pauses cleanup. An unmarked legacy folder starts its clock when first seen. Only unreferenced folders older than the threshold are deleted.

`[Notifications] webhook_url` is empty by default. Configure it and select events from `needs_review,partial,submission_unknown,persistence_error`; the default events are `needs_review,persistence_error`. Delivery uses one bounded background worker, no body buffering or redirects, and one shared one-second shutdown grace period. HTTP debug details are suppressed only for that worker. Queue acceptance is not proof of delivery; notices can be dropped on overload or shutdown and are not retried. Durable state remains authoritative.

### Recovery and diagnostics

Check the running version and view saved work without making API calls or changing files:

```bash
python rsoul.py --version
python -m rsoul.diagnostics --config-dir /data
```

Useful persistent files:

| File | Contents |
|---|---|
| `grab_list_state.json` | Owned download tasks, manifests, import transactions and retained review/cleanup states |
| `wanted_queue.json` | Unattempted wanted records and per-list/media cursor |
| `failed_downloads.json` | Scoped source failure history; legacy entries retained separately by schema |
| `book_ledger.json` | Verified imports and completed negative searches |
| `slskd_search_backoff.json` | Search pacing and rate-limit deadline |
| `diagnostics.json` | rsoul/Python and observed server versions, without credentials |
| `health.json` | Liveness and last-run counts, pending imports/cleanup/review, persistence errors |
| `recover_archive.jsonl` | Explicit operator recovery/discard decisions |

Standalone import callers can create sidecar journals under the download mount's `.rsoul-import-journal` directory; normal workflow uses the central task state. Recoverable sidecars are adopted on startup. Never delete a journal to force a retry while its remote operation may still be running.

For a `needs_review` task, inspect its reason, the actual Chaptarr record/history, command status and retained file paths. Correct a known record/path/permission issue before proceeding. If a command receipt was lost, verify what happened server-side before arranging another import. R:soul deliberately does not erase or automatically requeue ambiguous work. Resolved imports with sufficient evidence are recognized on a subsequent run.

Correcting a problem on a `needs_review` transaction does not automatically restart staging or submit a new import. Transient pre-submission states such as `validating`, `staging`, and `staged` can retry their recorded step; an uncertain or review-required operation needs reconciliation first. The diagnostic command summarizes central saved state; it does not query live services or scan standalone sidecar journals.

Checkpoint writes are atomic and fsynced. An absent or unwritable configured data directory, damaged recovery store, or failed durable checkpoint stops further mutations. Keep the intended persistent volume mounted at that directory; rsoul cannot distinguish it from a different writable directory at the same path. Health reports durable checkpoint failures as unhealthy; it does not restart the container or repair the filesystem. Source failure history is scoped by backend, author, title, medium and release. Local permission, API, path or import-verification faults do not automatically ban a peer.

Per-book transfer logs report queued/downloading state, completed versus expected files, bytes, peer, time since progress, and the next timeout. They are emitted on meaningful status changes or at most once a minute for unchanged work. A monitor-window handoff preserves the task and does not mean that an import succeeded.

Recovery inspection uses `python -m rsoul.recover --config-dir /data` or `--book YOUR_BOOK_ID` and reads live service evidence. With the normal instance stopped, `--apply` can create a fresh validating transaction for an eligible legacy import, and `--discard` can quarantine its proven owned files. They require a specific book and cannot be combined. Malformed targets, unknown outcomes, active overlapping scans/imports, or unclear ownership block the decision. Windows drive/UNC and POSIX ancestry are checked using path components.

`--confirm` closes an import that was set aside for review (`needs_review` or `submission_unknown`) when Chaptarr did in fact complete it: R:soul could not prove the outcome itself, but Chaptarr's history names every staged file as imported since staging and the book has files. Run `--book YOUR_BOOK_ID` first to see the evidence; `--confirm` (instance stopped) then archives the record to `recover_archive.jsonl`, notes the import in the book ledger and removes the task. It moves and deletes nothing; leftover staged files are listed for you to delete. Partial imports are never confirmed. The `Needs review` log line names the book ID to use.

A discard saves exact destinations before moves. An interrupted discard remains protected from automatic imports/cleanup and must be inspected and retried explicitly; its journal must not be erased to force progress. Neither recovery action deletes a Chaptarr library record.

`--apply`, `--discard` and `--confirm` change saved state, so the normal instance must be stopped; they take the application lock and refuse otherwise. Back up `/data` first and run them with the same image, environment, user and mounts as the normal service. A stopped container cannot be entered with `docker exec`; for a Compose service named `rsoul`:

```bash
docker compose run --rm --no-deps --entrypoint python rsoul -m rsoul.recover --config-dir /data --book YOUR_BOOK_ID --confirm
```

On TrueNAS, use the app's equivalent one-off or maintenance runtime. Listing and `--book` inspection only read, and can run while R:soul is running.

### Dry run

```bash
python rsoul.py --config-dir /data --dry-run
```

Dry run performs searches and reports candidates without enqueuing or importing, changing monitoring, advancing the normal wanted cursor, or mutating recovery state. It cannot inspect audio that has not been downloaded. Its report is `dry_run_report.txt` in the configuration directory. Search requests still consume the server's normal search budget.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
bash -n run.sh
```

The suite includes valid audio fixtures, corruption/wrong-record cases, restart and failed-write recovery, lost API responses, partial imports, conversion evidence, rate limits and local fake HTTP services using the real clients. CI runs Python 3.11 and 3.12 before image publication. The tests use controlled local services; they do not prove every deployed Chaptarr/Readarr/slskd combination, and audio checks are structural, not spoken-content verification.

## Credits and license

- **R:soul** — the original Readarr ↔ Soulseek/Stacks downloader this fork is based on, including its pluggable backends, matching and import workflow for ebooks. The logo is R:soul's.
- **[Soularr](https://github.com/mrusse/soularr)** by mrusse — the Lidarr ↔ Soulseek script R:soul was forked from.
- **[slskd](https://github.com/slskd/slskd)** and its Python client [slskd-api](https://pypi.org/project/slskd-api/), which R:soul uses to talk to Soulseek.

**This fork** is maintained by [HariOcto](https://github.com/HariOcto) and adds audiobook support for Chaptarr. It has been modified from upstream R:soul since November 2026; [CHANGELOG.md](CHANGELOG.md) lists the changes. It is not an official R:soul release: please report problems with it in this repository, not upstream.

R:soul, and this fork with it, is free software under the **GNU General Public License v3** (see [LICENSE](LICENSE)). You may redistribute and modify it under those terms; it comes with no warranty.

> **Note**: This project is **not** affiliated with Readarr, Chaptarr, Soularr or Stacks. Please do not contact those teams for support regarding this script.
