# Changelog

All notable changes to this fork are recorded here. Versions follow [Semantic Versioning](https://semver.org/); a hyphenated suffix marks a pre-release.

## [0.1.0-beta.1] — 2026-11-24 (pre-release)

First tagged pre-release of this fork, which adds audiobook support for Chaptarr to the ebook-only R:soul. Untagged drafts before it were distributed as patches; their saved state is migrated on upgrade. The 0.x version marks the fork's own release line and says the interfaces may still change.

### Added

- **Audiobook media mode** (`media_mode = audiobook | ebook | both`) for Chaptarr, with separate format lists, monitoring fields and per-medium history.
- **Audiobook folder and file matching.** Releases are matched on folder names, or on file names inside shared author folders. Disc/part subfolders, music libraries, sequels ("Title 2"), collections, bracketed series and other books by the same author are told apart. Chapter numbering is checked for completeness, including sets over 99 parts.
- **Catalog-aware checks.** The author's other titles, series positions and narrators from Chaptarr settle ambiguous names. Audio tags are compared with the wanted book before import. A download far too small for the book's duration is skipped.
- **Targeted imports** (`import_method = targeted`) that name the author, book and audio edition, with no silent fallback to a folder scan.
- **Verified, durable imports.** An import counts as successful only when Chaptarr's records tie every staged file to the intended book, including many-to-one MP3-to-M4B conversion. Intent is journaled before every request. Lost responses are reconciled, never blindly resubmitted. Partial or uncertain imports keep their files for review.
- **Operator recovery tool** `python -m rsoul.recover`:
  - `--apply` revalidates an eligible legacy import.
  - `--discard` quarantines a retained import's files.
  - `--confirm` closes a review item that Chaptarr's history shows fully imported.
  - Listing and `--book` inspection are read-only.
- **Wanted backlog and scheduling.** A persistent queue with eligibility checked before page caps. Duplicate records and recently imported or unfound books are skipped. Search pacing and rate-limit backoff persist across runs.
- **Optional features**, all off or neutral by default: webhook notifications, recording policies (language, narrator, abridgement, dramatisation), a bitrate floor, runtime comparison, catalog exclusions, quarantine retention, paused imports, and a read-only dry run.
- Read-only diagnostics (`python -m rsoul.diagnostics`), a Docker health check, and per-book transfer progress logs.
- Configuration through `RSOUL__<SECTION>__<OPTION>` environment variables.

### Changed

- Source failures are scoped to backend, author, title, medium and release. Local, API and import-verification problems no longer ban a peer.
- After the container's first run, the banner and unchanged startup report are logged at debug level only.
- Duplicate wanted records are reported in one line per run instead of one warning each.
- File-name matches inside generic folders are logged with the matching file name.
- `Needs review` log lines include the book ID and the `recover` command to inspect it.
- The README credits upstream R:soul and Soularr, states that this is a modified fork under the GPL v3, and shows the logo again.
- The sample `docker-compose.yml` uses this fork's image instead of upstream's ebook-only one.

### Known limitations

- The tests use controlled local services. They do not cover every Chaptarr/Readarr/slskd combination.
- Audio checks are structural; they do not verify spoken content.
- Where Chaptarr's evidence is incomplete, an import stays pending or in review rather than being marked successful. Use `rsoul.recover` to decide it.
- Searches still try the full Chaptarr title (with series and edition suffixes) before shorter variants.
- Very short "audiobooks" (samples) go to review rather than failing outright.

[0.1.0-beta.1]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.1
