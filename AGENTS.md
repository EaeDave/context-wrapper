# AGENTS.md — context-wrapper

Local pipeline: gravação → Whisper (GPU) → diarização/voicebank → LLM → SQLite + SPA.

## Layout

| Path | Role |
|------|------|
| `src/meet/` | Package Python (CLI `meet`, pipeline, store, OAuth, FastAPI) |
| `src/meet/web/app.py` | API JSON + serve SPA |
| `src/meet/web/jobs.py` | Fila single-worker de jobs GPU |
| `frontend/` | React + Vite + shadcn (build → `src/meet/web/dist/`) |
| `tests/` | pytest (SQLite tmp, TestClient) |
| `docs/LLM_CONTEXT.md` | Regras de negócio não-inferíveis + mapa de arquivos |

## Verify

```sh
cd frontend && bun install && bun run build   # dist/ exigido pelo hatch force-include
uv sync --group dev
uv run ruff check src tests
uv run pytest -q
cd frontend && bun run typecheck && bun run lint
```

## Constraints

- Single-user local; bind default `127.0.0.1`. Remoto só com `MEET_ALLOW_REMOTE=1`.
- Não commitar mídia (`.mkv`/`.wav`/…), `.env`, tokens, nem `src/meet/web/dist/`.
- Torch via index `pytorch-cu128`; não trocar CUDA major sem revalidar.
- OAuth refresh tokens rotacionam — ver `docs/LLM_CONTEXT.md`.
- Paths de mídia na API: sob `$HOME`, fora de `data_dir` / `~/.config/meet`.

## Product pointers

README § Regras do produto; jobs internos `process` / `reprocess` / `reextract`.
