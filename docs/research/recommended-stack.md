# Recommended Stack

Recommended direction: build NewsRAG as a **local-first, CLI-oriented evidence retrieval tool**, not a server app or web product.

## Core stack

```text
Language:      Python
Interface:     CLI
Storage:       SQLite
Full-text:     SQLite FTS5
Vector store:  LanceDB
PDF OCR:       ocrmypdf + tesseract
PDF parsing:   PyMuPDF first, pdfplumber when needed
Embeddings:    OpenAI text-embedding-3-small initially
LLM:           Claude/OpenAI initially, optional local Ollama later
Output:        Markdown source packets / reports
```

## Why this stack

- Handles thousands of documents without requiring server infrastructure.
- Keeps the system portable and easy to back up.
- SQLite is enough for document metadata, page text, chunks, and keyword search.
- FTS5 handles exact terms: names, addresses, ordinance numbers, parcel IDs, dollar amounts.
- LanceDB handles semantic/vector search without running a separate vector database service.
- Python has the best ecosystem for PDF parsing, OCR, and document processing.
- Hosted embeddings are cheap enough that local embedding infrastructure is not necessary at first.
- Markdown output fits the actual workflow: generate source material for essays, articles, and reports.

## Architecture

```text
documents folder
  ↓
OCR/text extraction
  ↓
page + chunk storage in SQLite
  ↓
FTS5 keyword index + LanceDB vector index
  ↓
hybrid search
  ↓
source packet / cited evidence
  ↓
LLM-assisted outline, summary, report draft
```

## CLI shape

Example commands:

```bash
newsragg ingest ~/Documents/city-hall-pdfs
newsragg search "stormwater concerns near downtown"
newsragg packet "affordable housing funding since 2024" --out source-packet.md
newsragg ask "What evidence mentions ABC Construction?" --sources-only
newsragg draft "write a neutral source summary about the zoning dispute" --out draft.md
```

## Output format

The primary artifact should be a Markdown source packet:

```markdown
# Source Packet: Stormwater concerns near downtown

## Key Evidence

1. City Council Packet — 2025-03-14 — p. 27
   > “The applicant shall submit a revised stormwater management plan…”

2. Planning Commission Minutes — 2025-04-02 — p. 8
   > “Several residents raised concerns about runoff…”

## Possible angles

## Timeline

## Open questions

## Source list
```

## What not to start with

Do not start with:

- Kubernetes deployment
- Postgres/pgvector server
- FastAPI backend
- local web UI
- OpenSearch/Elasticsearch
- Weaviate/Qdrant/Milvus
- local-only LLM requirement

Those may become useful later, but they add complexity before the retrieval workflow is proven.

## When to reconsider server infrastructure

Move toward Postgres/pgvector or a server deployment only if:

- the corpus grows far beyond thousands of documents,
- multiple users/devices need concurrent access,
- unattended scheduled ingestion becomes important,
- search latency or index size becomes a real SQLite/LanceDB problem,
- this becomes an always-on research appliance instead of a local writing tool.
