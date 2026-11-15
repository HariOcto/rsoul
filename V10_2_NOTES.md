# R:soul audiobook support — v10.2 changes and deployment

v10.2 is built on the entire v10 codebase. **You do not need to install v10 first** if
using the v10.2 complete ZIP or the v9 -> v10.2 consolidated patch.

## Changed since v10

1. Targeted imports never silently become `DownloadedBooksScan`, including missing
   editions/manual-import previews, API errors, folders containing multiple books, and
   resumption of a lost import command. Unsubmitted files remain staged and tracked.
2. Audiobook edition selection uses Chaptarr's `readingFormatId = 2` (ebook = 3,
   physical = 1) or an unambiguously typed edition. A monitored **ebook** edition no
   longer wins when R:soul is importing an audiobook. Multiple indistinguishable audio
   editions cause a safe refusal rather than a guessed narrator/recording.
3. Before `ManualImport`, R:soul records existing bookfile IDs on the wanted book. After
   the import it looks for **new** bookfiles with both the wanted `bookId` and selected
   `editionId`. Disappearing staging files or a successful command alone are not proof.
   Unverifiable imports remain pending for up to 24 hours; a definitive mismatch is a
   failed import. The code never automatically removes or repairs files in the Chaptarr
   library after a misassignment.
4. Failed import cleanup uses R:soul's known local staged folder, even when a
   `ManualImport` result has no `body.path`.
5. The ledger matches author, title **and media type**, not only a numeric book ID;
   negative search history also respects those identifiers. `disable_sync = True` no
   longer records a book as *imported*.
6. An import pending verification cannot trigger a second download in the same run.
   Scan-mode imports that explicitly leave the wanted record empty are not recorded
   as successful.

## Deployment on TrueNAS / Docker

- **Complete ZIP**: replace your working copy with the v10.2 files; retain your
  `/data` volume, persistent configuration, Docker volumes and history. Back up the
  container configuration, Chaptarr library and `/data` first.
- **Consolidated patch**: use the provided `v9-to-v10.2.patch` on the matching source
  tree **before v10**. Do *not* also apply `rsoul-v10.patch`.
- **Incremental patch**: use `v10-to-v10.2.patch` **only** if you already have the v10 ZIP
  source (not necessary for the user who has not yet applied v10).
- For Chaptarr audiobooks, set `RSOUL__READARR__IMPORT_METHOD=targeted` (or
  `[Readarr] import_method = targeted`). The shipped default remains `scan` for
  compatibility; existing configuration files and environment overrides are NOT
  automatically migrated.
- Keep `[Search Settings] remove_wanted_on_failure = False` during validation and
  test a single non-critical audiobook. After an import, verify its Chaptarr title,
  edition and attached files. A missing `readingFormatId` or `bookfile` API response
  can intentionally prevent a targeted import, leaving files staged for inspection.
- A `ManualImport` command might still attach a file to a duplicate record due to
  Chaptarr-side bugs. v10.2 detects that it cannot confirm the requested book;
  it cannot undo the external import safely.

## Validation limitations

The v10.2 regression suite uses fake Chaptarr and slskd servers. It does **not** prove
that the live Chaptarr API returns bookfile records with the fields `id`, `bookId` and
`editionId` on your installed release, or that it handles multi-part files in the same
way as the test double. If those fields are missing, targeted imports fail closed.
Test first using a small, dispensable book or a disposable Chaptarr library.
