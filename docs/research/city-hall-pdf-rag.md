# City Hall PDF RAG / Evidence Retrieval System

This is a strong RAG/search use case.

The goal is not primarily to make the AI “know” everything. The goal is to build a system that can:

1. ingest city hall PDFs,
2. extract usable text,
3. index them,
4. let you ask natural-language questions,
5. return relevant passages with citations,
6. let you reuse those results for reports, timelines, summaries, or analysis.

The key output should be **retrieved evidence**, not just an AI-generated answer.

## What you might use it for

Example queries:

- “Find documents discussing the proposed zoning change near Main Street.”
- “Which meetings mention affordable housing funding?”
- “Show references to stormwater management from the last six months.”
- “Find public comments opposing the parking ordinance.”
- “What contracts mention ABC Construction?”
- “Summarize city council discussions about bike lanes since January.”
- “Find mentions of budget increases for police, fire, and parks.”
- “What documents discuss tax abatements?”
- “Which PDFs mention parcels near 123 Example Road?”
- “Build a timeline of decisions related to the downtown redevelopment project.”

The useful thing is that the system can return:

```text
Relevant passage
Document title
PDF filename
Page number
Meeting date
Agenda item, if known
Link to source PDF
Confidence/relevance score
```

That gives you material you can later use in a report.

## Important distinction

This is more **document intelligence** than simple chatbot RAG.

For this case, build a **retrieval and evidence system** first, with AI layered on top.

The dangerous version would be:

> “Ask a chatbot and trust the summary.”

The better version is:

> “Search semantically, retrieve passages, show page-level citations, then optionally summarize selected evidence.”

For civic/government documents, citations and traceability matter a lot.

## Recommended architecture

```text
PDF folder / source feed
        ↓
Ingestion watcher
        ↓
PDF text extraction + OCR if needed
        ↓
Document metadata extraction
        ↓
Chunking by page / section / agenda item
        ↓
Embeddings
        ↓
Search index: vector + keyword
        ↓
Retriever API
        ↓
UI / CLI / report workflow
```

## Core capabilities

### 1. PDF ingestion

Since documents are added weekly or daily, use an automated ingestion process.

It should track:

- filename
- source URL, if available
- date downloaded
- document date
- document type
- meeting body, e.g. city council, planning commission, zoning board
- agenda item, if extractable
- page numbers
- hash of file contents to avoid duplicate indexing

You want idempotent ingestion: if the same PDF appears twice, it should not duplicate results.

### 2. Text extraction

City hall PDFs may be a mix of:

- digitally generated PDFs with selectable text,
- scanned image PDFs,
- agenda packets,
- meeting minutes,
- staff reports,
- attachments,
- maps,
- tables,
- forms.

So you need both normal PDF parsing and OCR.

Recommended tools:

- **PyMuPDF** / `fitz` for PDF parsing
- **pdfplumber** for text and tables
- **Tesseract OCR** for scanned documents
- **ocrmypdf** to create searchable PDFs
- optional: **unstructured** for document partitioning

Recommendation:

> Use `ocrmypdf` first for scanned PDFs, then extract text with PyMuPDF or pdfplumber.

This makes later searching much better.

### 3. Chunking strategy

For city documents, do not only chunk arbitrarily by token count.

You likely want chunks with strong provenance:

- document ID
- page number
- section heading
- agenda item
- paragraph text
- surrounding context

Good chunking options:

#### Basic

Chunk by page, then split long pages into smaller passages.

#### Better

Chunk by:

- agenda item,
- heading,
- staff report section,
- public comment,
- resolution/ordinance section,
- table description,
- page range.

For the first version:

> Store page-level text and create smaller overlapping chunks from each page.

That gives reliable page citations.

### 4. Hybrid search

Use both:

1. **keyword search** for exact terms, names, parcel IDs, ordinance numbers, addresses, dollar amounts;
2. **vector search** for meaning-based natural language queries.

This is important.

For example:

- Query: “affordable housing”  
  Vector search works well.

- Query: “Ordinance 2024-17”  
  Keyword search works better.

- Query: “stormwater runoff from new developments”  
  Hybrid search works best because docs may say “drainage impacts,” “impervious surface,” or “MS4 compliance.”

Strong recommendation:

> Use hybrid search: full-text search + vector search.

## Technology choices

### Best practical stack

For a serious but manageable local/private system:

#### Language

**Python**

Reason: better PDF, OCR, document processing, and NLP ecosystem.

#### Database/search

### Option A — simplest solid local stack

**PostgreSQL + pgvector + full-text search**

Pros:

- one database
- stores metadata, chunks, embeddings
- supports vector search
- supports keyword search
- mature and reliable
- easy to query later for reporting
- can grow with you

Cons:

- more setup than SQLite

This is the top recommendation.

### Option B — lightweight local-first stack

**SQLite + sqlite-vec + SQLite FTS5**

Pros:

- simple
- local file database
- easy backups
- no server
- good for thousands/tens of thousands of PDFs depending on size

Cons:

- less robust for larger multi-user workflows
- fewer advanced ranking tools

Good if this is just for one user and the collection is moderate.

### Option C — search-focused stack

**OpenSearch or Elasticsearch + vector search**

Pros:

- excellent full-text search
- great filtering, facets, highlighting
- good for large document corpora
- strong search UI possibilities

Cons:

- more operational complexity
- heavier than you may need

Good if search is the core product.

### Option D — vector DB plus separate text DB

**Qdrant + PostgreSQL/SQLite**

Pros:

- excellent vector search
- clean separation
- scalable

Cons:

- two systems to manage
- full-text search needs separate support

Good if semantic retrieval becomes central.

## Recommended stack

```text
Ingestion:     Python
PDF OCR:       ocrmypdf + tesseract
Text parsing:  PyMuPDF, pdfplumber
Database:      PostgreSQL
Vector index:  pgvector
Keyword index: Postgres full-text search
Embeddings:    OpenAI text-embedding-3-small or local BGE/Nomic
LLM:           optional, for summaries/report drafting
API:           FastAPI
UI:            simple web app, CLI, or notebook
```

In short:

> **Python + PostgreSQL + pgvector + Postgres full-text search + OCRmyPDF + PyMuPDF/pdfplumber**

This gives you a durable, report-friendly system.

## Embeddings recommendation

### Easiest high-quality option

**OpenAI `text-embedding-3-small`**

Pros:

- good quality
- simple API
- inexpensive
- no local model management

Cons:

- sends document text to OpenAI
- may be inappropriate if documents are sensitive, though city hall docs are often public

### Local/private option

Use:

- `nomic-embed-text`
- `bge-small-en`
- `bge-base-en`
- `e5-base`
- `mxbai-embed-large`

Run with:

- Ollama
- sentence-transformers
- FastEmbed

Recommendation:

- If documents are public: use **OpenAI `text-embedding-3-small`** to start.
- If you want local-only: use **BGE** or **Nomic embeddings**.

## LLM recommendation

The LLM is optional.

Build the retrieval system first without generation.

Use the LLM for:

- summarizing selected passages,
- drafting reports,
- comparing documents,
- generating timelines,
- extracting structured facts,
- identifying entities,
- turning evidence into prose.

But the search system should work even without an LLM.

Good hosted models:

- GPT-4.1 / GPT-4o
- Claude
- Gemini

Good local models:

- Llama 3.1/3.3
- Qwen
- Mistral

For serious report drafting, hosted models are usually better.

## Data model

At minimum:

### `documents`

- `id`
- `filename`
- `source_url`
- `sha256`
- `title`
- `document_type`
- `meeting_date`
- `published_date`
- `body` — city council, planning commission, etc.
- `created_at`
- `processed_at`

### `pages`

- `id`
- `document_id`
- `page_number`
- `raw_text`
- `ocr_used`
- `text_quality_score`

### `chunks`

- `id`
- `document_id`
- `page_start`
- `page_end`
- `section_title`
- `chunk_text`
- `embedding`
- `metadata`

### Optional extracted entities

- people
- organizations
- addresses
- parcel numbers
- ordinance numbers
- resolution numbers
- dollar amounts
- dates
- projects

This makes future reporting much easier.

## Search behavior to build

When you query:

> “Find documents about stormwater concerns related to the Westside development.”

The system should:

1. embed the query;
2. run vector search;
3. run keyword search;
4. merge and rerank results;
5. show top passages;
6. group by document;
7. expose filters:
   - date range,
   - committee/body,
   - document type,
   - source,
   - meeting date;
8. allow selecting passages for a report packet.

The output should look like:

```text
1. Planning Commission Packet — 2025-03-14 — p. 27
   “The applicant shall submit a revised stormwater management plan...”
   Relevance: 0.87

2. City Council Minutes — 2025-04-02 — p. 8
   “Several residents raised concerns about runoff from the proposed development...”
   Relevance: 0.82

3. Staff Report — Westside Rezoning — 2025-02-21 — p. 4
   “Impervious surface coverage is expected to increase...”
   Relevance: 0.79
```

Then optionally:

> “Summarize these into a neutral report section with citations.”

## Things to watch out for

### OCR quality

Government PDFs can be ugly. Bad OCR means bad search.

Store OCR confidence/text quality where possible.

### Tables

Budgets, contracts, and agenda tables may extract poorly.

Use `pdfplumber` for tables, or treat table extraction as a separate enhancement.

### Maps/images

Maps may need special handling. A normal RAG system will not understand map content unless OCR catches labels or you add image analysis.

### Duplicates

Agenda packets often include repeated attachments. Use file hashes and maybe text hashes.

### Citations

Do not skip citations. For this use case, page numbers and source PDFs are essential.

### Hallucination

Make the system say:

> “I found no matching evidence”

instead of inventing an answer.

## Build plan

### Phase 1: Evidence search

Build:

- PDF ingestion folder
- OCR pass
- text extraction
- document/chunk storage
- hybrid search
- result page with citations

No chatbot needed yet.

### Phase 2: AI-assisted synthesis

Add:

- summarize selected search results
- compare documents
- generate timelines
- draft report sections
- extract structured facts

### Phase 3: Automation

Add:

- watched folder
- scheduled ingestion
- source scraping/downloading if needed
- duplicate detection
- email/report export
- saved searches/alerts

## Short recommendation

Build:

```text
Python ingestion pipeline
PostgreSQL database
pgvector for semantic search
Postgres full-text search for keyword search
ocrmypdf + tesseract for scanned PDFs
PyMuPDF/pdfplumber for text extraction
OpenAI text-embedding-3-small for embeddings
FastAPI backend
Simple web UI for search + citation export
```

Design it as an **evidence retrieval system**, not just a chatbot.

The most valuable first version would be:

> Drop city hall PDFs into a folder, automatically OCR/index them, then ask natural-language questions and get cited passages by document and page.
