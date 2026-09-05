# Source revisions and change detection

Status: Approved on 2026-09-05. This is a design specification; the refresh commands and revision behavior described below are not implemented yet.

## Goal and boundary

Complete the revision and refresh policy left open by [Source identity and repeated ingestion](source-identity-and-repeated-ingestion.md). This is the design deliverable for `task-9603f915`, not an implementation of refresh or versioning.

The source-neutral design already created the follow-up tasks. Reuse `task-08cdcec9` for implementation; do not create a duplicate backlog. Reprocessing remains separately assigned to `task-aa5c6e7a` and `task-aec94144`.

### Already designed and implemented

- Source identity is the conservatively normalized submitted URL or absolute local path.
- Artifact identity is the exact acquired-byte SHA-256 within one corpus.
- One published artifact has at most one document; exact duplicates from other locations do not create aliases or merge metadata.
- Ordinary ingestion preserves changed artifacts without publishing a replacement, using `change_detected_artifact_saved` or `change_already_detected`.
- Publication preserves immutable artifacts and exposes no partially indexed document.

PR #66 defined the identity policy; PR #71 implemented exact-byte duplicates and changed-artifact preservation. This design does not redo either task.

### Agreed workflow

1. Refresh is an explicit, manual operation on one known source. There are no schedules, bulk refreshes, or URL/path reassignment in the first version.
2. Refresh reads the source again and compares exact content hashes. It does not trust file timestamps, HTTP validators, or extracted-text similarity as proof of unchanged content.
3. Changed content is processed and becomes the current local searchable revision automatically after complete success. There is no separate publication-approval command. “Publish” means local search visibility, not uploading or sharing.
4. Search and new packets use current revisions by default, with an explicit history option. Existing packets remain unchanged.
5. Historical metadata and citations are preserved. User metadata carries forward; unchanged bytes do not create a revision or overwrite metadata. Metadata editing is separate from refresh.

## Revision identity and storage

Keep the existing source, artifact, document, and job identities. A revision is a published document's durable membership in its source's version history, not another copy of its bytes or text.

The implementation adds a `source_revisions` relation with a stable revision ID, source ID, document ID, monotonically assigned per-source revision number, first-publication timestamp, and publishing job ID when known. Artifact identity is obtained through the document's existing artifact relationship. Enforce uniqueness of document membership and `(source_id, revision_number)`, and validate that the document's artifact belongs to that source. No cross-source alias relationships are introduced.

Each source stores a nullable `current_revision_id` and a monotonically increasing publication generation. The pointer must identify one of that source's revisions. The pointer changes only in a successful publication/reactivation transaction. Revision numbers describe first-publication order, not which revision happens to be current now. The generation increments whenever the current pointer changes, including reactivation, so an old worker cannot mistake an A → B → A sequence for an unchanged source.

- New raw bytes published for a source get a new artifact, document, and revision identity.
- A previously preserved, unpublished artifact is reused rather than copied.
- Existing historical artifacts, documents, source units, passages, metadata, and published indexes remain intact when another revision becomes current.
- Historical artifacts remain `published`; “historical” is a relationship to the current pointer, not a failed or unpublished artifact state.
- Only completely published documents enter `source_revisions`. Staged and failed candidates are not revisions and never appear in history-mode search.
- Jobs remain the refresh-attempt audit records. Do not add a separate retrieval-history entity.

### Reversions and exact duplicates

These two cases require explicit handling before ordinary duplicate short-circuiting:

- **The source returns one of its own historical artifacts:** reactivate that existing revision, with outcome `revision_reactivated`. Do not reindex, create another document/revision, renumber history, or rewrite its metadata/provenance. The new refresh job records the observation and pointer change. This is distinct from manually selecting an old snapshot: refresh must have actually reacquired those bytes from the source.
- **The source returns an artifact already published for another source:** report `duplicate_ignored`, identify the existing document, and leave the requested source's current revision unchanged. Do not copy the other document, attach it as this source's revision, create an alias, or merge metadata. This preserves the approved first-successful-import-wins policy. The CLI must explicitly say that no revision was published for the requested source.

If the same bytes are preserved but unpublished under a different source, fail with a contextual `artifact_source_conflict` rather than silently adopting its ownership. Resolving unpublished cross-source ownership is outside this task. Concurrent first publication elsewhere is rechecked transactionally: a now-published cross-source artifact becomes `duplicate_ignored`, not a uniqueness traceback.

Ordinary `ingest` retains its existing behavior: a published exact hash is ignored even if it belongs to this source's historical revision. Only explicit refresh may change the current pointer after a source reversion.

## Change detection and source references

### Public HTTP(S) URLs

Refresh reacquires the source's originally submitted URL through the existing safe acquisition service. Normalize identity with the approved URL rules; redirects and canonical links do not replace source identity.

Perform an unconditional GET and hash the complete accepted response representation using the same content-decoding and artifact hashing rules as ingestion. No HEAD-only checks, conditional GETs, ETag-only decisions, Last-Modified-only decisions, or byte-range requests are introduced. An unexpected 304, partial response, or failed acquisition is a failed refresh, not an unchanged result.

Keep all existing public-network validation, DNS pinning, redirect revalidation, HTTPS downgrade rejection, timeouts, compressed/decompressed byte limits, and prohibition on ambient credentials and secondary-resource fetching. Type selection and adapter validation must still run for changed content. A format change can be a revision if the new content is supported and valid; unsupported or contradictory content fails without replacing the current revision.

A new final redirect URL with unchanged bytes does not create a revision. Record operational observation details on the job, without rewriting the original artifact's acquisition provenance. Do not add an alias for the new destination.

### Local files

Refresh rereads the stored normalized absolute source path, not a relative path resolved against the current working directory and not the artifact cache path. Use the existing regular-file, size, inode/device, modification-time, resolved-target, and read-stability checks. Modification time and size are safety checks, not substitutes for hashing.

An explicitly registered symlink is resolved again and must target a stable regular file throughout that read. A changed target between refreshes is an observation, not a new submitted source identity; changed target bytes follow the same hash decision table. Missing files, special files, unreadable files, unstable reads, and limit violations fail without deleting the previous revision.

### Reference-only and metadata-only changes

| Change | Result |
| --- | --- |
| Submitted URL/path differs under the approved normalization rules | A different source; submit through ordinary ingest, not a refresh override |
| Different submitted source returns published exact bytes | Ordinary duplicate policy; no new source alias or metadata |
| Redirect destination or resolved symlink target changes, bytes unchanged | No new revision; historical provenance unchanged; observation recorded in the refresh job |
| HTTP headers, filesystem timestamps, or reported media type change, bytes unchanged | No new revision or metadata rewrite after successful safe acquisition |
| Caller wants to change title, meeting date, body, document type, or jurisdiction | Not a refresh operation; no metadata-edit flags in this version |
| User resubmits identical bytes with different metadata through ingest | Existing `duplicate_ignored` policy; published metadata unchanged |
| Metadata embedded in the source changes its raw bytes | Content change, even if extracted body text is identical; process a revision |
| Extractor, chunker, model, or index changes but source bytes do not | No refresh revision; defer to the reprocessing design |

## Metadata across revisions

Each published document retains an immutable metadata snapshot for the purposes of refresh. A new revision inherits the source's explicit user metadata; new adapter metadata candidates may supply other safe fields, but cannot override those user values or infer civic metadata.

Preserve explicit user metadata separately from merged adapter metadata for new ingestions so refresh can distinguish a user-supplied title from an extracted title. Carry those explicit values into each new revision. Do not copy acquisition timestamps, stored paths, hashes, extents, or extractor identity from the previous revision; those describe the new artifact and processing run.

Existing documents do not reliably separate explicit and extracted metadata. Migration conservatively treats existing descriptive metadata as inherited overrides, excluding generated acquisition/storage/processing fields, and marks that origin as legacy rather than inventing attribution. Legacy values therefore remain stable rather than being silently replaced by freshly extracted candidates. Document this compatibility behavior in the implementation.

Reactivation restores the original revision as-is. It does not apply newer metadata to an old artifact. Unchanged refreshes never run extraction just to update metadata.

## CLI and job lifecycle

The approved command surface is deliberately small:

```bash
newsrag documents show <document-id>
newsrag refresh <source-id>
newsrag documents versions <document-id>
newsrag search "budget" --include-history
newsrag packet "budget" --include-history --out packets/budget-history.md
newsrag jobs list
newsrag jobs retry <failed-refresh-job-id>
```

`documents show` exposes source ID, revision ID/number, and whether the document is current. `documents versions` accepts any published document ID, resolves its source, and lists that source's published revisions with document IDs, hashes, original acquisition times, first-publication times, and an explicit current marker. History is ordered by revision number, not by presumed meeting date. No separate source-catalog command is required.

`refresh` accepts one existing source ID with a published current revision. It does not accept an arbitrary replacement URL/path, metadata overrides, directories, manifests, `--all`, an artifact selection, or a scheduled mode. Unknown sources and sources without a published revision fail before enqueueing with guidance to ingest or retry the initial ingestion instead.

### Processing sequence

1. Validate the source and enqueue a durable `refresh-source` job; network and file acquisition happen in the daemon, not during CLI registration.
2. Claim the job and capture the source's current revision and publication generation before acquisition. At most one pending/running refresh is allowed per source; a repeated request returns the existing job ID without adding another job.
3. Reacquire and hash bytes under the existing acquisition limits. Each new refresh command reacquires the source, even when ordinary ingestion previously staged changed bytes; it must not blindly publish a possibly outdated staged candidate.
4. Compare against the current artifact, this source's historical artifacts, published cross-source duplicates, and preserved unpublished artifacts, in that order.
5. For new or reusable same-source unpublished bytes, durably preserve the artifact and checkpoint the candidate artifact ID, acquisition observation, base revision/generation, inherited metadata, and processing options on the refresh job before extraction. No document is visible yet.
6. Validate and process that immutable candidate through the shared adapter, chunking, embedding, FTS, and vector pipeline.
7. Publish the complete document and revision and switch the current pointer atomically with a generation check. Complete the job with structured identifiers and an outcome. A separate approval step is not required.

### Outcomes

| Condition | Job status/outcome | Effect on current revision |
| --- | --- | --- |
| Acquired hash equals current artifact | `done` / `unchanged` | None; no extraction, indexing, or metadata mutation |
| Changed, valid new artifact | `done` / `revision_created` | New revision becomes current after complete publication |
| Changed hash matches a same-source staged candidate | `done` / `revision_created` after processing succeeds | Reuse artifact; publish once |
| Hash matches this source's historical revision | `done` / `revision_reactivated` | Existing revision becomes current without reindexing |
| Hash matches another source's published document | `done` / `duplicate_ignored` | None; return existing document without linking it |
| Source or processing failure | `failed` with stage-specific error | None |
| Base generation is no longer current | `failed` with `refresh_conflict` | None; require a fresh refresh command |

Except for `duplicate_ignored`, job results identify the requested source, observed artifact, previous/current revision and document IDs, the observed/read time, and outcome where applicable. A refresh `duplicate_ignored` result retains the existing artifact/document identifiers plus `requested_source_id` identifying the already registered source whose current pointer was not changed. This is job outcome context, not a new source reference or alias; retain no copied URL/path, metadata, or acquisition observation from the duplicate, and keep the existing payload-discard behavior. Ordinary ingestion duplicate results are unchanged. Job output must explicitly explain that no revision was published for the requested source before presenting the existing duplicate document.

Job states remain `pending`, `running`, `done`, and `failed`. Do not add a published job status or approval queue. Error stages distinguish acquisition, artifact integrity/ownership, adapter validation/extraction, chunking, embeddings, FTS/vector publication, and current-generation conflicts. Logs must not expose source bodies, raw URL queries, or credentials.

### Retry and interruption

Reuse `jobs retry` for failed refresh jobs. Before an artifact checkpoint exists, retry reacquires the source. After the checkpoint exists, retry processes those exact preserved bytes and options without refetching; job output explains that it is retrying the saved candidate. A new `refresh` command is the way to check the live source again.

The captured base generation must still match before any retried publication. If another refresh has changed the current pointer, fail the old retry as `refresh_conflict`; do not overwrite newer work or silently adopt a new base. Verify the stored hash and size before retrying; missing or corrupt preserved bytes fail clearly rather than fetching replacement bytes under the same checkpoint.

Publication must include a durable job completion receipt in the same SQLite transaction as the revision/pointer change, or atomically mark that refresh job done there. This lets recovery distinguish “not published” from “published, but the worker died before acknowledging completion.” Recovery must recognize the recorded success without creating another revision or reactivating stale content. Interrupted refresh jobs without a committed receipt become failed/retryable after confirming their worker is no longer active; do not take over a live worker's job.

A new command after a failed job creates a new refresh job; it does not change the old checkpoint. Concurrent retry/enqueue operations must obey the same one-active-refresh-per-source rule.

## Publication, concurrency, and failure safety

SQLite is the authoritative visibility boundary. The document bundle, revision membership, current pointer/generation, and refresh completion receipt are committed together only after all required extraction and indexing writes succeed. The previous current document remains usable while work is pending or running.

LanceDB and SQLite do not provide one distributed transaction. Reuse and extend the existing staged-vector cleanup pattern: compensate failed vector writes, report cleanup failures, and reject vectors without a committed eligible document/revision through authoritative SQLite checks. Do not delete the previous revision's indexes when making a new revision current.

A transaction checks the expected publication generation and artifact-to-document uniqueness before committing. Same-artifact races cannot create two documents/revisions. Failed or stale workers must never clean up vectors belonging to an already committed document from another job.

The invariant is one current revision per published source, not one published revision per source. Ordinary ingestion and refresh must both use the explicit current pointer rather than selecting the earliest document for the source. Ordinary changed-content detection remains staging-only.

A missing remote/local source does not imply deletion of evidence. Retention, pruning, manual rollback commands, aliases, and forced publication of saved historical bytes are outside this version.

## Retrieval and historical citations

Search and packet generation select current published revisions by default. `--include-history` includes all published revisions, not staged/failed candidates, and composes with existing metadata and source-type filters. It does not mean “historical only.”

Use a consistent eligible-document snapshot for keyword retrieval, vector retrieval, contextual candidate expansion, ranking, and packet construction. Filtering only after a fixed candidate limit is insufficient: historical vector hits must not crowd current results out of the candidate set. Apply eligibility at retrieval or refill candidates until the eligible limit/exhaustion is reached. Context expansion must not reintroduce an excluded revision.

Once a query has selected a concrete document/revision, packet provenance must resolve that exact artifact even if a concurrent refresh changes the current pointer. Do not substitute the now-current document halfway through a packet. Existing packet files are never updated by refresh.

Each result carries source/revision/document identity plus its existing typed source-unit range. History-mode output labels the revision number and current/historical state so identical titles and civic dates are distinguishable. Packet source entries retain document/revision IDs, exact artifact SHA-256, the original artifact acquisition provenance, and typed locations. A current marker is explicitly the state observed when the query ran, not a promise about the live source later.

Refreshing must not renumber old source units, regenerate their text, rewrite their metadata, or redirect an old citation to a new revision. Hashes and document IDs remain the immutable evidence anchors even if human-facing citation text is identical across revisions.

Inventory listing remains a record of all published documents, now with revision/current markers; `documents show <id>` continues to work for historical documents. Explicit document-level brief, extraction, and enrichment operations also remain available for historical document IDs. Corpus-wide topics/entities/timeline/leads browsing should default to evidence from current documents and offer the same `--include-history` scope to avoid presenting obsolete evidence as current. Old discovery records are preserved, not copied to a new revision; refresh does not automatically run enrichment.

Reprocessing unchanged artifacts is not implemented here. Its separate design must account for retained citation anchors and any derived-data generations before changing previously referenced source units.

## Migration and compatibility

For each existing published document, create a revision membership without changing its document ID, artifact hash, metadata snapshot, source-unit IDs, or derived indexes. Backfill each source's current pointer to its existing published document and start the publication generation consistently. Preserve original acquisition/document timestamps; mark legacy publication job IDs unknown rather than inventing historical events.

Existing ingestion enforces one published document per source before refresh is introduced. Migration must verify that assumption. If it encounters multiple distinct published documents for a source or invalid ownership, fail with a diagnostic instead of silently choosing a winner or deleting history. Failed and change-detected artifacts remain unpublished and have no revision row.

After migration, initial ingestion publishes revision 1 and its current pointer through the same visibility boundary. Re-running migration is idempotent. Ordinary duplicate outcomes, unchanged metadata behavior, and current single-revision PDF/HTML search remain compatible.

## Existing implementation task

`task-08cdcec9` implements this approved design, retaining its dependencies on `task-96a0b951` and `task-9603f915`. Its requirements and acceptance criteria are updated in place; no new backend task records are created.

Implement incrementally within that existing task, with these bounded stages:

1. **Revision storage and migration:** add revision membership/current generation and metadata-origin preservation; migrate without altering citation identities; make initial publication revision-aware. Verify fresh and legacy corpora, integrity rejection, and idempotency.
2. **Refresh job and safe publication:** add one-source manual refresh, hash decisions, retry checkpoints, reactivation, duplicate handling, completion recovery, and failure-safe pointer switching. Verify deterministic local and mocked-URL cases, races, interruption, and every no-op/failure outcome.
3. **History-aware retrieval and presentation:** expose version inventory and current/history scope across search, packets, and corpus discovery. Verify mixed PDF/HTML results, exclusion before candidate truncation, concurrent refresh/query consistency, and unchanged historical packet provenance.

Any need for independently tracked subtasks should be proposed to the user rather than silently creating another backlog or changing downstream dependencies. Reprocessing remains in the existing reprocessing tasks.

## Acceptance evidence expected from implementation

- URL and local refresh: unchanged bytes, metadata/reference-only observations, new valid bytes, source-type changes, same-source staged bytes, and failed acquisition.
- Historical reuse: A → B → A reactivates the original document without new derived records; ordinary ingest of A remains an ignored duplicate.
- Duplicate identity: cross-source published matches never add aliases/metadata/revisions; unpublished ownership conflicts and concurrent publication are explicit.
- Metadata: explicit user values carry forward, legacy origin is conservative, adapter candidates cannot override user values, and historical snapshots remain unchanged.
- Failure isolation: inject extraction, chunking, embedding, FTS, vector, generation-conflict, and crash-boundary failures; the previous current revision remains searchable and retries cannot duplicate publication.
- Retrieval: current-only defaults, explicit history, metadata/source-type filter composition, ranking eligibility, contextual expansion, historical inventory, and discovery scope.
- Provenance: old document IDs, typed locations, exact hashes, and packet files survive refresh/reactivation; a query cannot mix old text with a new artifact's provenance.
- Verification: existing tests plus focused refresh/migration/retrieval coverage pass formatting, Ruff, mypy, `make check`, and `./scripts/pre-pr.sh`.

## Approval record

- Automatic local publication after successful explicit refresh: agreed.
- Current-only search/new packets by default, with explicit history: agreed.
- Historical metadata preservation, inherited user metadata, and no metadata-only revision: agreed.
- Manual one-source scope and full-byte hash comparison: agreed.
- Consolidated design, edge cases, CLI details, and update of existing implementation task `task-08cdcec9`: approved on 2026-09-05.
