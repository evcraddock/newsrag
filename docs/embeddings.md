# Embedding configuration

NewsRAG has one embedding integration: the OpenAI-compatible `/v1/embeddings` API. Local and hosted services use the same `openai_compatible` provider configuration.

NewsRAG requires `embedding.provider`, `embedding.base_url`, and `embedding.model`. Set `embedding.api_key_env` when the service requires bearer authentication. NewsRAG reads the named environment variable at request time and never stores its value.

Changing the embedding model changes the vector space. Do not mix vectors from different models in one corpus; use a separate data directory or rebuild the corpus embeddings when changing models.

## llama.cpp

For local Apple Silicon use, the recommended model is Nomic Embed Text v1.5 in GGUF format. Run an embedding-specific `llama-server` on localhost and give the model a stable API alias:

```bash
llama-server \
  --model /path/to/nomic-embed-text-v1.5.f16.gguf \
  --alias nomic-embed-text-v1.5 \
  --embedding \
  --pooling mean \
  --host 127.0.0.1 \
  --port 8080
```

Configure NewsRAG in `~/.config/newsrag/config.yaml`:

```yaml
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: nomic-embed-text-v1.5
```

The llama.cpp executable, model file, and server lifecycle are managed separately from NewsRAG.

## LM Studio

Load an embedding model in LM Studio, start its local server, and use the model identifier returned by `GET /v1/models`:

```yaml
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:1234/v1
  model: your-loaded-embedding-model-id
```

## Ollama

Ollama remains usable through its OpenAI-compatible API. Install and start Ollama separately, pull the desired model, and include `/v1` in the base URL:

```bash
ollama pull nomic-embed-text
```

```yaml
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:11434/v1
  model: nomic-embed-text
```

NewsRAG does not use Ollama's native `/api/embed` endpoint.

## Hosted OpenAI

Export the API key without writing its value to NewsRAG configuration:

```bash
export OPENAI_API_KEY='your-api-key'
```

```yaml
embedding:
  provider: openai_compatible
  base_url: https://api.openai.com/v1
  model: text-embedding-3-small
  api_key_env: OPENAI_API_KEY
```

The same pattern works for other hosted OpenAI-compatible services. Set `base_url`, `model`, and `api_key_env` to the values required by that service.

## Verify the configuration

Run:

```bash
newsrag doctor
```

The embedding check calls the configured service's `/v1/models` endpoint and confirms that the configured model is listed. Missing settings, unavailable services, missing API-key environment variables, malformed responses, and unavailable models produce actionable warnings or errors.

You can also test a service directly:

```bash
curl http://127.0.0.1:8080/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"nomic-embed-text-v1.5","input":["city council agenda"]}'
```

## Migrate from the native Ollama provider

The old configuration used `provider: ollama`, a base URL without `/v1`, and Ollama's native `/api/embed` endpoint. Replace it as follows:

```yaml
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:11434/v1
  model: nomic-embed-text
```

NewsRAG rejects `provider: ollama` with this migration guidance rather than silently changing endpoint behavior.
