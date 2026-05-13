# NewsRAG

Working notes for a RAG / evidence retrieval system over city hall PDF documents.

## Files

- [[rag-overview]] — general explanation of RAG systems, use cases, and baseline technology choices.
- [[city-hall-pdf-rag]] — specific architecture and build plan for searching city hall PDFs with natural-language queries and citations.
- [[recommended-stack]] — current local-first CLI stack recommendation.
- [[discovery-oriented-ingestion-enrichment]] — research plan for evidence-backed document briefs, extracted facts, topics, timelines, and story leads.

## Core idea

Build an **evidence retrieval system**, not just a chatbot:

> Drop city hall PDFs into a folder, automatically OCR/index them, then ask natural-language questions and get cited passages by document and page.

Recommended stack:

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
