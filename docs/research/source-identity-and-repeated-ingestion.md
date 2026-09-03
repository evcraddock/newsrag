# Source identity and repeated ingestion

Status: Approved on 2026-09-02.

## Goal

Define how NewsRAG identifies sources, artifacts, documents, and ingestion jobs and what happens when submitted bytes have already been seen.

This design covers identity and exact-byte duplicate handling. It does not implement the policy or define source refresh, version selection, or reprocessing.

## Identity model

Identity is scoped to one corpus/data directory. NewsRAG does not deduplicate or share records across separate corpora.

### Source

A source is the location submitted to NewsRAG: a public HTTP(S) URL or local path. A source may return different bytes over time.

URL identity uses conservative normalization:

- scheme and host casing are normalized;
- default ports are removed;
- fragments are ignored because they are not sent to the server;
- query parameters remain significant;
- path casing and trailing slashes remain significant;
- redirect destinations are recorded but do not replace the submitted source identity; and
- an HTML canonical link is metadata, not identity.

Local source identity uses the normalized absolute submitted path. Differently named paths are different sources. An explicitly submitted symlink is allowed when it resolves to a regular file; NewsRAG records both the submitted path and resolved target and verifies that the target does not change while being read. Recursive directory ingestion skips symlinks.

### Artifact

An artifact is the exact stored bytes acquired from a source. Its identity is the SHA-256 hash of those raw bytes within one corpus. Byte size is retained for integrity checks.

Filename, URL, path, media type, user metadata, normalized text, extracted text, and semantic similarity are not part of artifact identity. NewsRAG performs no normalized-text, fuzzy, or semantic duplicate detection.

### Document

A document is the searchable representation of one exact artifact within one corpus. One published artifact has at most one document.

A document keeps the same ID when its unchanged artifact is reprocessed. If changed bytes are later approved as a source revision, they receive a different document ID linked to the same source. Revision publication is defined by `task-9603f915`, not this design.

### Job

A job is one submitted ingestion attempt. Jobs remain durable even when no new document is created so callers can see what happened.

Successful jobs store structured results with the applicable source, artifact, and document IDs. Defined outcome codes are:

- `created` — a new artifact and document were published;
- `duplicate_ignored` — exact bytes already belong to a published document;
- `change_detected_artifact_saved` — an existing source returned new bytes, which were preserved but not published; and
- `change_already_detected` — those changed bytes were already preserved and still await the refresh workflow.

Failed jobs retain a stage-specific error and do not publish a document.

The job itself is the audit record for an ingestion attempt. NewsRAG does not create a separate retrieval-history entity. A source may retain `last_checked_at` for current status.

## Exact-byte duplicate policy

A submission is a duplicate only when its raw SHA-256 hash matches an artifact that already belongs to a successfully published document in the same corpus.

For an exact duplicate, NewsRAG:

- marks the new job `done` with outcome `duplicate_ignored` and the existing document ID;
- performs no extraction, chunking, embedding, or indexing;
- stores no new artifact, source reference, document, metadata, alias, chunk, passage, embedding, or index record;
- does not modify the existing document; and
- ignores all metadata and location differences from the duplicate submission.

The rule is first successful import wins. An exact hash match is treated as an accidental repeat regardless of whether it came from the same URL/path, a renamed file, or a different URL/path.

## Decision table

| Submission | Outcome | Stored result |
| --- | --- | --- |
| New source and new bytes | `created` | New source, artifact, and published document |
| Same source, same bytes as its published document | `duplicate_ignored` | Job result only; existing records unchanged |
| Different source, same bytes as any published document | `duplicate_ignored` | Job result only; new location and metadata discarded |
| Same bytes with different submitted metadata | `duplicate_ignored` | Job result only; existing metadata unchanged |
| Different bytes with identical normalized or extracted text | Not a duplicate | Continue according to source/new-content rules |
| Artifact exists only because an earlier first-time ingestion failed | Retry processing | Reuse preserved bytes; create a document only after complete success |
| Existing source returns a new hash | `change_detected_artifact_saved` | Preserve new artifact; keep current document unchanged |
| Existing source returns an already preserved changed hash | `change_already_detected` | Job result only; keep current document unchanged |
| Two concurrent jobs acquire the same new hash | One `created`, one `duplicate_ignored` | Exactly one artifact and published document |

## Changed source content

Different raw bytes are never treated as a duplicate.

When an existing source returns a new hash, NewsRAG preserves the new artifact but does not replace the current document or publish a second searchable document through ordinary ingestion. The job completes with `change_detected_artifact_saved`. A repeated submission of the same staged bytes completes with `change_already_detected` without storing another artifact.

The current document remains searchable until the explicit refresh/version workflow defined by `task-9603f915` determines how a changed artifact becomes a new revision.

## Failed ingestion and retry

An artifact preserved by a failed first-time ingestion is not an ignored duplicate because it has no successfully published document. A retry or later submission may reuse those stored bytes and run extraction and indexing again.

A document becomes visible only after all publication stages succeed. Repeated failures create job records but no duplicate artifact or partial searchable document.

## Concurrency

Artifact hashes and published artifact-to-document relationships require database uniqueness. Concurrent jobs with identical new bytes converge on one artifact and one document. One job reports `created`; the other reports `duplicate_ignored`. A race must not surface as a user-facing uniqueness error or leave partial derived records.

## Existing PDF behavior and migration

Current PDF ingestion hashes the local or downloaded PDF, looks up `documents.source_hash`, and silently returns when a matching document exists. The daemon then marks the job done without recording whether it created or reused anything. The current behavior already avoids duplicate indexing but loses the outcome and does not use the source/artifact/document separation.

Migration to the source-neutral model will:

- preserve each unique existing document ID;
- create an artifact from its stored raw PDF and existing source hash;
- create the source from the original URL for URL-ingested PDFs or the original local path for local PDFs;
- treat downloaded cache paths as artifact storage rather than source identity;
- leave historical jobs as legacy records without fabricated result codes;
- consolidate any existing exact-hash duplicate documents using the first successfully published document; and
- remove later exact-hash duplicate documents and their derived records without retaining aliases, copied locations, or duplicate metadata.

After migration, repeated PDFs use the same `duplicate_ignored` policy as every other format.

## Provenance retained

For non-duplicate ingestion, NewsRAG retains enough information to explain identity decisions:

- submitted and normalized source reference;
- resolved URL or symlink target where applicable;
- raw artifact SHA-256 and byte size;
- source, artifact, document, and job IDs;
- job status and structured outcome;
- retrieval/read and job timestamps; and
- the published document associated with the artifact, when one exists.

For an ignored duplicate, only the job result and existing document ID are retained from the repeated submission.

## Out of scope

This design does not define:

- normalized-text, fuzzy, or semantic deduplication;
- metadata editing or metadata merging;
- how changed artifacts become current revisions;
- automatic source monitoring or change detection schedules;
- content replacement or revision history presentation;
- reprocessing after extractor, chunking, embedding, or index changes; or
- duplicate handling across separate corpora.

Source revisions and refresh are defined by `task-9603f915`. Reprocessing is defined by `task-aa5c6e7a`.

## Implementation task

`task-96a0b951` implements this approved policy after the source-neutral storage and adapter foundations in `task-dbf01cd8` and `task-9de42124`.

Implementation must test exact duplicates from the same and different locations, changed hashes, failed unpublished artifacts, retries, and concurrent submissions. It must prove that ignored duplicates create no source, artifact, document, metadata, alias, or derived search records.

## Approval record

- Individual decisions: discussed and agreed
- Consolidated document review: approved on 2026-09-02
- Implementation task: `task-96a0b951` updated to the agreed exact-byte policy
