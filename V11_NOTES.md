# R:soul v11 release notes

- **Version:** `11`
- **Build:** `v11`
- **Prepared:** 23 November 2026
- **Cumulative patch base:** `HariOcto/rsoul`, branch `audiobook-support`, commit `612a90e5e32f6250c9c7b77d8a6a84278e815e5b`

This completed v11 incorporates the earlier v10.4 review changes and implements the corrections from the uploaded v11 review. Apply `rsoul-github-to-v11.patch` directly to the pinned GitHub base. No v10.4 or earlier v11 patch is required. Do not layer this cumulative patch over an older v11 draft. The GitHub branch may have moved; the exact base above is authoritative.

## Release documents

- [V11_CHANGES_AND_UPGRADE.txt](V11_CHANGES_AND_UPGRADE.txt): the complete explanation of all 28 change groups, including what changed, how, why, defaults, upgrade commands and recovery/discard procedures.
- [APPLY_V11_FROM_GITHUB.txt](APPLY_V11_FROM_GITHUB.txt): concise instructions for applying the patch and building/selecting the new image.
- [V11_VALIDATION.txt](V11_VALIDATION.txt): executed regression and package checks, interpreter version, and deployment limitations.
- [README.md](README.md) and [config.ini](config.ini): operation and configuration reference.

## Corrections completed after the uploaded v11 review

| Issue | Completed behavior | Why it matters |
|---|---|---|
| Discard checked planned stage paths while files still lived at the source | Inspect actual file locations and saved/current mappings before moving anything | An active import at the source cannot be missed |
| An old source path could contain another task's replacement file | Require current ownership and file identity; a staged input does not retain a claim on its old path | Another task's bytes remain untouched |
| A partial discard could lose track of files already moved | Journal exact quarantine destinations before moves; reconcile on explicit retry | Process death and failed checkpoints do not erase the recovery map |
| Normal work could resume after an unfinished discard | Pause callbacks, pending imports, transfer resume, startup cleanup and direct transaction entry points | Remaining files cannot be imported as an unintended partial book |
| Retention searched JSON text for raw paths | Inspect structured paths and recheck ownership under the state lock; pause on untrustworthy state | Unicode/quoted/backslash paths and pending discards remain protected |
| Malformed command names or NUL paths looked unrelated | Require valid string names and complete usable targets | Unreadable command evidence cannot authorize recovery |
| Windows root and UNC comparisons missed overlaps | Preserve path qualification, fold Windows case, compare component ancestry | Drive-root or parent scans correctly block overlapping recovery |
| Unknown results were accepted as failure | Require explicit unsuccessful completion; unsupported terminal results still block | Another attempt cannot assume a previous import failed |
| Narrator matching split names into shared surname fragments | Match complete Unicode-normalized names, explicit inversions, and every known cast identity | Ray Porter cannot choose Jane Porter's edition |
| Import batching existed only in a helper | One production coordinator submits later completions while checking accepted commands on fixed deadlines | Slow imports no longer impose serial poll timeouts |
| Webhooks could wait on trickling response bodies | One bounded daemon queue, header-only transport, no redirects/retries and one shared shutdown grace period | Notifications cannot block import transaction processing or process exit |
| HTTP DEBUG logs exposed webhook URL credentials | Scope HTTP logging suppression to the webhook worker | Other diagnostics remain available without leaking hook URLs |
| Required pre-POST phase checkpoint suppressed the actual failure notice | Durably record the unknown-outcome notification decision once per unresolved attempt | Lost/malformed receipts alert when enabled without a duplicate POST |
| Cross-filesystem move guidance promised automatic resumption | Explain EXDEV, retain state/files, and describe operator recovery for review-required work | Instructions match the actual state machine |

## Cumulative v11 protections retained

- Complete input-to-output import proof on the intended work; valid many-input conversion coverage.
- Stable book identity, conservative compatible editions, and no silent targeted-to-scan fallback.
- Durable pre-request intent, operation manifests, staging and checkpoint failure propagation.
- Lost-response reconciliation without blind enqueue/import retries.
- Exact local completeness checks before and after restart, independent of disappearing transfer rows.
- Scoped transfer cleanup and retired-attempt ownership.
- Sibling-title isolation, format coverage, long chapter numbering and fragment checks.
- Every-file audio inspection, catalog-dependent identity checks and optional recording policies.
- Persistent rate-limit backoff, useful surname/title queries, typed failures and scoped source history.
- Durable wanted backlog with eligibility before caps; duplicate and unavailable records do not silently consume pages.
- Explicit local exclusions, paused imports, read-only diagnostics, dry run and meaningful task progress.

## Defaults and operator features

The configurable bitrate floor remains `8` kbit/s (`0` disables it). Runtime comparison remains off (`audiobook_duration_tolerance=0`). Quarantine retention remains off (`failed_retention_days=0`). A missing webhook URL disables notification delivery; enabled default events are `needs_review,persistence_error`.

Notifications are **best effort**. `send()` reports queue acceptance, not delivery. The queue holds at most 32 waiting notices; one reusable daemon performs delivery. Requests have a three-second socket inactivity timeout, but core processing does not wait on them. Responses are not buffered, redirects are not followed, and all outstanding notices share a one-second shutdown grace period. Queued work can be dropped on overload/shutdown; saved application state remains authoritative.

`--apply` and `--discard` are explicit per-book operator actions, require the normal instance to be stopped, acquire the same lock, and repeat live evidence checks. Apply creates a fresh validating transaction for an eligible legacy import; it does not POST an import. Discard quarantines currently owned files and retires the task after durable completion. In-progress discards stay journaled and require an explicit retry. Neither command deletes a Chaptarr library record.

## Deployment scope

This is a source patch. Build an image from the resulting tree, make it available to the host, and select it in the app configuration. Preserve actual persistent state, downloads, credentials and path mappings; use a single instance. The expected version output is `R:soul 11 (v11)`.

Tests exercise real local files, audio parsing, state and clients against controlled services. They do not prove every deployed Chaptarr/Readarr/slskd combination. Structural audio checks are not spoken-content verification. Missing remote evidence remains pending/review. Preparing this release does not push GitHub changes, publish an image, or alter the live NAS.
