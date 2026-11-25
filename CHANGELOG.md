# Changelog

All notable changes to this fork are recorded here. Versions follow [Semantic Versioning](https://semver.org/); a hyphenated suffix marks a pre-release.

## [0.1.0-beta.5] — 2026-11-25 (pre-release)

Fixes from a fourth external review, of 0.1.0-beta.4. Each finding is reproduced by a permanent test that fails on beta.4. Where the review's suggested fix depended on how Chaptarr writes its history, Chaptarr's `HistoryService.cs` (current `main`, and the commit R:soul's comments pin) was checked first.

### Fixed

- **Two people side by side in one folder were read as a co-credit.** `Audiobooks\Correct Author - Wrong Author\Intended Work` and `Audiobooks\Correct Author (Wrong Author Remix)\Intended Work` were accepted and could reach an import. A conflicting person in the same folder is now cleared only by a genuine list of people credited together, joined by `&`, `and`, `+`, `,`, `;` or `with`, with no brackets or titles in between (`Ryk Brown & Scott Brown`, `Ryk Brown and Scott Brown`). A dash, brackets or parentheses no longer make a co-credit. The author named in full further down the path still clears a conflict above it.
- **The ordinary import verifier ignored a contradictory `Size`.** On a `BookFileImported` receipt, Chaptarr records the size of the file it picked up (`BookInfo.Size`, falling back to the imported file, then 0). The verifier now compares that with the file R:soul staged: a different size means the receipt is about another file. It is deliberately not compared with the library file, which can change size when Chaptarr writes tags. A missing or zero `Size` stays neutral for the verifier.
- **History-only confirmation compared `Size` with the wrong file.** Beta.4 compared a plain import's `Size` with the library file; it now compares it with the staged file, as above, and still requires it to be recorded and non-zero. Conversions still compare `OutputSize` with the output.
- **Unreadable exclusion fields were read as "nothing excluded".** Chaptarr writes `ExcludedSourcePaths` only when something was excluded, always as a JSON list of paths. A field that is present but empty, `null`, an object or a list with an empty entry now makes the receipt inconclusive, in the verifier and in history-only confirmation alike. An absent field or `[]` still excludes nothing.
- **File names joined for the evidence check.** When several file names were checked together (`... - 01.mp3 Frank Herbert - Dune - 02.mp3`), only the last extension was removed, so `mp3 Frank Herbert` read as another person. Every audio extension is now removed. The stricter co-credit rule above exposed this; it was previously masked.

### Known limitations

- History-only confirmation needs a recorded, non-zero `Size` (or `OutputSize`). If Chaptarr records none for an import, that import cannot be confirmed from history and stays in review.
- Unchanged from 0.1.0-beta.1: tests use controlled local services; audio checks are structural; uncertain imports stay in review; searches try the full title first; very short "audiobooks" go to review.

## [0.1.0-beta.4] — 2026-11-25 (pre-release)

Fixes from a third external review, of 0.1.0-beta.3. Each finding is reproduced by a permanent test that fails on beta.3.

### Fixed

- **The author in a parent folder cleared another person below it.** `Audiobooks\Correct Author\Wrong Author\Intended Work` was accepted because the full name anywhere in the path outweighed the conflict, and it could reach an import. A conflicting person is now decisive unless the author is named in full in the same path segment (a co-credit, `Ryk Brown & Scott Brown`) or in a more specific one below it (`Scott Brown\Resistance\Ryk Brown - Resistance.m4b`). Listed narrators are unaffected. This applies at search and again before import.
- **History-only confirmation accepted incomplete receipts.** A receipt without an output path or size still linked its file. `--confirm --accept-history` now requires every receipt to record the output's path and size (`Size`, or `OutputSize` for a conversion), both matching a file on the book now. This is stricter than the import verifier, to make up for the missing pre-import baseline; the beta.3 notes overstated how closely the two matched.
- **Unreadable conversion exclusions were treated as "nothing excluded".** An `ExcludedSourcePaths` list with entries that are not path strings (objects, numbers) now makes the receipt inconclusive, in the import verifier as well as in history-only confirmation. An empty list still excludes nothing.

### Known limitations

- History-only confirmation needs complete receipts. If Chaptarr records no size for an import, that import cannot be confirmed from history and stays in review.
- Unchanged from 0.1.0-beta.1: tests use controlled local services; audio checks are structural; uncertain imports stay in review; searches try the full title first; very short "audiobooks" go to review.

## [0.1.0-beta.3] — 2026-11-25 (pre-release)

Fixes from a second external review, of 0.1.0-beta.2. Each finding is reproduced by a permanent test that fails on beta.2.

### Fixed

- **`--confirm --accept-history` accepted contradictory receipts.** History-only confirmation now applies the import verifier's receipt rules; the only allowance left is the missing pre-import file baseline:
  - a FileId the event records must still be on the book; a different file now at the same path no longer stands in for it, and a recorded ImportedPath must be that file's path;
  - a recorded Size (or a conversion's OutputSize) must match the file;
  - a conversion's SourceFileCount must match the sources it lists;
  - one output accounts for one input, unless a single consistent conversion receipt names them all; two independent imports onto one remaining file link neither;
  - an output on another book, author, edition or medium blocks confirmation.
  Plain `--confirm` already refused these cases; only the history-only mode was affected.
- **A file named with a title word beside the surname is no longer author evidence.** With no separator, `Carrie King.m4b` for Stephen King's *Carrie* may be title + surname or a person called Carrie King. Such a name is accepted only when the path also names the author in full (`Stephen King\Carrie\Carrie King.m4b`); a bare surname folder does not settle it. Search rejects these names, and a download queued by an earlier build is held for review before import. Separated names (`Carrie - King.m4b`) are unaffected; an unseparated `Resistance Brown.m4b` alone is now rejected as well, a deliberate trade of some matches for no wrong-author imports.

### Known limitations

- Unchanged from 0.1.0-beta.1: tests use controlled local services; audio checks are structural; uncertain imports stay in review; searches try the full title first; very short "audiobooks" go to review.

## [0.1.0-beta.2] — 2026-11-25 (pre-release)

Fixes from an external review of 0.1.0-beta.1. Each finding is reproduced by a permanent test.

### Fixed

- **A conflicting author whose first name is the book's title.** For Stephen King's *Carrie*, a folder `Audiobooks\Carrie King\Carrie` was read as the bare surname "King" and could be imported. Title words are now set aside only in file names (`Resistance Brown.m4b` still works); a folder is read as written, so `Carrie King` is another person. This applies at search and again before import.
- **`--confirm` is rebuilt on the normal checks** instead of its own, weaker proof:
  - every expected file must be accounted for; a manifest entry without a server path no longer shrinks the expected set;
  - the book's current author, title and medium are checked (`inspect_identity`); a changed identity blocks;
  - running or unknown import commands are looked for under both the saved and the current path mapping, and history is matched on exact paths under those mappings only (the folder-and-file-name fallback is gone);
  - the import verifier (`verify_import_evidence`) decides: conversions that exclude a requested file, or include an unrequested one, never count, and outputs must be on the book now;
  - when the verifier cannot prove an import that history fully links, `--confirm --accept-history` closes it as an operator decision, recorded as `history` in `recover_archive.jsonl` and as `operator: history only` in the book ledger.
- **Upgrading in the middle of staging.** Work validated under older rules and interrupted half-way through staging (some files moved, some still at the source) is now validated as one complete set where each file actually is, then staged and imported, instead of being held for review. The audio-tag check reads the exact files rather than one folder.
- **Audiobooks in music folders named by their file.** `Music\Ryk Brown - Resistance.m4b` and `Music\Resistance\Ryk Brown - Resistance.m4b` were rejected before their file names were read. The music-folder rule (author in full, plus an m4b or a listed narrator) now sees the file name.

### Known limitations

- A *file* named only with the title and the surname (`Carrie King.m4b`, no separator) is still read as title + surname. It is accepted only with other evidence of an audiobook (a spoken-word folder, an m4b, a listed narrator). The audio-tag check (reject by default) still stops files whose tags name another author, but untagged files get past it.
- Unchanged from 0.1.0-beta.1: tests use controlled local services; audio checks are structural; uncertain imports stay in review; searches try the full title first; very short "audiobooks" go to review.

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

[0.1.0-beta.5]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.5
[0.1.0-beta.4]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.4
[0.1.0-beta.3]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.3
[0.1.0-beta.2]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.2
[0.1.0-beta.1]: https://github.com/HariOcto/rsoul/releases/tag/v0.1.0-beta.1
