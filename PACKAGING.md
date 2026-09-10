# Packaging — `docker compose up`

The proposal promises *"docker compose up, three containers"* and *"offline
install bundle on a USB drive"*. This is that, and it is also the cleanest
version of the sovereignty demo, because the air gap becomes **structural**
rather than a claim.

---

## The idea

Two containers on two networks:

```
        ┌──────────────────────────────────────────────┐
        │  sutra   (internal: true — NO gateway)       │
        │                                              │
        │   workbench  ──────────►  ollama             │
        │   API, agent, RAG, UI     serves the models  │
        └──────────────────────────────────────────────┘
                                        │
                            sutra_setup │  (setup only —
                                        │   delete for the demo)
                                        ▼
                                    internet
```

`workbench` sits on `sutra` and nothing else. `sutra` is declared
`internal: true`, which means Docker gives it **no gateway at all**. There is no
route off the host for the workbench to take, whether or not anything tries.

`ollama` gets a second network only so `ollama pull` can fetch weights once. You
delete that line before the demo, and the stack keeps running from its volume.

This is worth saying out loud to judges: *the workbench is not trusted not to
call out — it is placed somewhere it cannot.*

---

## First run (needs the network, once)

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:14b
docker compose exec ollama ollama pull qwen3:4b
docker compose up -d --build workbench
```

Then open <http://127.0.0.1:8000>.

Check it came up correctly:

```bash
curl -s http://127.0.0.1:8000/api/health | python -m json.tool
```

`inference_available` should be `true` and `missing_models` should be empty. If
a model was never pulled, that field is how you find out **before** a judge does
— a missing model looks exactly like a broken agent from the audience.

Index the corpus once:

```bash
curl -X POST http://127.0.0.1:8000/api/index
```

---

## Before the demo — cut the wire

Edit `docker-compose.yml` and comment out the `sutra_setup` line under `ollama`:

```yaml
    networks:
      - sutra
      # - sutra_setup        # <- commented out: no route off the host
```

Then:

```bash
docker compose up -d --force-recreate
```

Now **neither** container has a route to the internet. Prove it in front of the
room — this is the moment the claim stops being rhetoric:

```bash
docker compose exec workbench curl -sS --max-time 5 https://api.openai.com || echo "NO ROUTE — as designed"
```

```bash
docker network inspect sih_sutra --format '{{.Internal}}'
```

The second prints `true`. That is Docker itself confirming the network has no
gateway — not our code marking its own homework.

Run the full task with the Wi-Fi physically off as well. Two independent
layers (`netguard` in-process, `netmonitor` out-of-process) keep reporting
throughout, and `/api/network` stays at zero external calls.

---

## Offline bundle (USB)

On a machine with network:

```bash
docker compose build
docker save sih-workbench ollama/ollama | gzip > sutra-images.tar.gz
```

Copy `sutra-images.tar.gz`, the repo, and the Ollama volume to the target, then:

```bash
gunzip -c sutra-images.tar.gz | docker load
docker compose up -d
```

Nothing is fetched. Every dependency, the embedding weights and the OCR
language data are already inside the image — `Dockerfile` runs
`scripts/fetch_models.py` at **build** time precisely so that a cold start with
no network behaves like a warm one.

---

## Running without Docker

Unchanged, and still the fastest path on a laptop:

```bash
ollama serve
```
```bash
ollama pull qwen3:14b && ollama pull qwen3:4b
```
```bash
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

---

## Configuration

`config.yaml` remains the single source of truth. The container overrides only
what genuinely differs per deployment, via environment variables:

| Variable | Overrides | Used for |
|---|---|---|
| `SUTRA_INFERENCE_ENDPOINT` | `inference.endpoint` | `http://ollama:11434` inside compose |
| `SUTRA_API_STYLE` | `inference.api_style` | switch to vLLM without a rebuild |
| `SUTRA_MODEL_DOCUMENT` / `_CODE` / `_GENERAL` | `models.*` | drop to `qwen3:4b` on a small machine |

Nothing else is env-configurable on purpose. Two places to look for a setting is
already one too many.

---

## Hardware

| | Models | Notes |
|---|---|---|
| Minimum | `qwen3:4b` everywhere | ~4 GB RAM for the model. Set the three `SUTRA_MODEL_*` vars |
| Recommended | `qwen3:14b` + `qwen3:4b` | ~12 GB. The tier the routing badge demonstrates |
| Plant deployment | 48 GB GPU server | vLLM via `SUTRA_API_STYLE=openai-compatible` |
