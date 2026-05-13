# Discovery-oriented ingestion enrichment

## Problem

NewsRAG currently works best when the user already knows what to search for. That is useful for evidence lookup, but the article-writing workflow also needs discovery: after a city hall packet is ingested, the system should help the user understand what is in it, what changed, which people/organizations/projects appear, what dates or dollar amounts matter, and which possible story leads deserve follow-up.

The key product constraint is that discovery output must remain evidence-first. Summaries, entities, timelines, extracted facts, and story leads should point back to page-grounded passages or quotes so a writer can verify the source before using it.

## Research summary

The best fit for this project is a staged hybrid enrichment pipeline. Deterministic extraction should handle high-confidence facts such as dates, dollar amounts, page counts, document inventory metadata, URLs, file properties, and simple keyword/topic counts. Optional LLM extraction should handle higher-level interpretation such as document briefs, notable actions, story leads, and open questions, but only through structured JSON schemas and quote/page validation. Hosted LLMs may produce better summaries, but the default direction should remain local-first with an explicit provider abstraction so the user can choose Ollama/local models or a hosted model later.

Recent RAG ingestion guidance emphasizes enriching documents with metadata, entities, semantic tags, page ranges, section titles, and provenance before retrieval. LlamaIndex exposes this as metadata extractors for summaries, titles, questions answered, and entities. Elasticsearch and enterprise RAG guidance similarly describe using keyphrases/entities as ingestion-time metadata. Docling-style document parsing is relevant for later because city hall PDFs often contain layout and tables, but adding a heavy parser should wait until the current PyMuPDF/pdfplumber pipeline shows specific layout failures.

## Approach comparison

| Approach | Strengths | Weaknesses | Best use in NewsRAG |
| --- | --- | --- | --- |
| Deterministic extraction | Fast, cheap, private, testable, stable, easy to validate | Weak at summarization, topic abstraction, and story judgment | First pass for inventory, dates, dollar amounts, URLs, simple entities, file/page stats, deadline/action regexes |
| Local LLM structured extraction | Private by default, flexible, good at summaries/leads/open questions, aligns with local-first goals | Slower, model quality varies, can hallucinate, needs quote validation | Optional enrichment provider for document briefs, notable actions, possible story leads, and questions |
| Hosted LLM structured extraction | Stronger models, better long-context reasoning, less local setup | Privacy/cost concerns, network dependency, harder default story for civic PDFs | Optional provider after local schema/provenance design is stable |
| Keyphrase/entity libraries | Good middle ground for terms and named entities, no generation | Adds dependencies/models, may be noisy on OCR text, needs canonicalization | Later enhancement if regex plus FTS term statistics are not enough |
| Layout-aware parser such as Docling | Better section/table/layout understanding | Heavier dependency, may complicate local setup | Later extractor option for agenda tables, scanned layouts, and structured packets |

## Recommended architecture

Keep `ingest-file` focused on source durability, OCR, canonical page text, chunks, passages, and search indexes. Add a separate enrichment stage after successful ingestion. The ingestion job can enqueue `enrich-document` for the new document, or users can run enrichment manually for existing documents. Enrichment failures should not invalidate an otherwise searchable document; they should be visible and retryable like other jobs.

The enrichment pipeline should run in layers:

1. Build a document profile from existing rows: title, metadata, page count, source URL/path, text length, extraction quality, top FTS terms, and likely section headings.
2. Run deterministic civic fact extraction over pages/passages: dates, dollar amounts, percentages, ordinance/resolution numbers, URLs, action verbs, deadline phrases, and simple capitalized entity candidates.
3. Persist evidence-backed extraction records with page/passage provenance.
4. Optionally run a structured LLM extractor on selected passages or page windows to produce document briefs, notable actions, open questions, and possible story leads.
5. Validate each LLM evidence reference by checking that quoted support text appears in the cited page or passage. Reject or mark unsupported items instead of storing unverifiable claims.
6. Aggregate document-level items into corpus-level browsing views such as topics, entities, timelines, and lead lists.

## Provenance rules

Generated discovery data should never be stored as unsupported free text. Every extracted item should have at least one evidence reference where possible.

Minimum evidence reference fields:

- `document_id`
- `page_start`
- `page_end`
- `passage_id` when available
- `quote` copied from canonical page/passage text
- `extractor` or provider identity
- `confidence` or validation status

For the first implementation, passage-level provenance plus a stored quote is enough. Character offsets can be added later if the extraction layer needs exact highlighting. Page text remains the citation source of truth.

## Proposed data model

Prefer a small generic model first, then specialize after usage is clearer.

```text
document_profiles
  id, document_id, page_count, text_length, extraction_quality_json, created_at, updated_at

document_briefs
  id, document_id, summary, significance, open_questions_json, extractor, model, status, created_at, updated_at

discovery_items
  id, document_id, item_type, label, value_json, summary, confidence, extractor, model, created_at

discovery_evidence
  id, item_id, document_id, passage_id, page_start, page_end, quote, validation_status, created_at
```

Initial `item_type` values should include `topic`, `entity`, `date`, `money`, `action`, `deadline`, `vote`, `contract`, and `story_lead`. SQLite JSON columns can hold type-specific details while the CLI and tests establish real usage. Add FTS indexes over `document_briefs.summary`, `discovery_items.label`, and `discovery_items.summary` so discovery output is browsable without vector search.

A later normalized model can split `entities`, `entity_mentions`, `topics`, `topic_mentions`, `events`, and `story_leads` into separate tables once the generic extraction output stabilizes.

## Proposed CLI workflows

```bash
newsrag documents list --recent
newsrag documents show <document-id>
newsrag documents brief <document-id>
newsrag discover document <document-id>
newsrag discover recent --since 2026-01-01
newsrag topics list
newsrag entities list --type organization
newsrag timeline --body "City Council" --since 2026-01-01
newsrag leads list --status new
newsrag leads show <lead-id>
newsrag enrich <document-id> --provider local
newsrag enrich --all --missing
```

`documents list` should solve the first discovery problem by showing what has been ingested. `documents show` should expose metadata, page count, source path/URL, brief status, and top extracted items. `discover recent` and `leads list` should answer the journalism question: what might be worth looking into?

## Recommended implementation sequence

1. `task-fe65ea9a` — Add document inventory commands using existing tables before adding new AI behavior.
2. `task-2cba53c1` — Add discovery storage and provenance helpers.
3. `task-8b4aa3e9` — Add deterministic extraction for dates, money, deadlines/actions, and basic entity/topic candidates.
4. `task-facda6f8` — Generate evidence-backed document briefs from deterministic/extractive evidence.
5. `task-c491cd87` — Add optional structured LLM enrichment with strict schema validation and quote/page checks.
6. `task-36887b88` — Add corpus discovery commands for topics, entities, timelines, and story leads.
7. `task-69418a8b` — Add refresh/retry behavior and status visibility for enrichment jobs.

## Testing strategy

Use small mocked page/passage fixtures rather than large PDFs. Test deterministic extractors with page-grounded examples. Test LLM enrichment through a fake provider returning valid and invalid JSON. Include tests that unsupported LLM claims are rejected when their quote does not appear in the cited passage. Keep ingestion tests separate from enrichment tests so OCR/search behavior remains stable.

## Risks and mitigations

- Hallucinated summaries: require structured output with evidence references and quote validation.
- Noisy OCR text: preserve extraction quality metadata and expose low-confidence items clearly.
- Slow enrichment: run enrichment as separate durable jobs, allow missing/failed enrichment without blocking search.
- Schema churn: start with generic `discovery_items` and JSON details, then normalize after real usage.
- Privacy/cost concerns: default to deterministic and local providers, make hosted providers explicit opt-ins.
- Over-indexing low-value items: store confidence and extractor type, and let CLI filters hide low-confidence candidates by default.

## Recommendation

Build discovery enrichment as an evidence-backed layer on top of the existing ingestion pipeline, not as a replacement for search. The first release should make the corpus browsable and inspectable without any LLM. The second release should add local structured LLM extraction for document briefs and possible story leads, with quote validation as a hard gate. This preserves NewsRAG's evidence-first identity while making it useful when the user does not yet know what to search for.
