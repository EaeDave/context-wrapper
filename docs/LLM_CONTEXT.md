<!-- business-readme:context:start -->
# LLM Context

<!-- Admission filter: only context not recoverable from repository code. -->

## Current business rule map

- Pipeline, extração LLM por blocos, rastreabilidade de fatos/tarefas e responsabilidade pessoal → `README.md#regras-do-produto`, `src/meet/pipeline.py`, `src/meet/extract.py`, `tests/test_extract.py`, `tests/test_traceable.py`; **jobs internos:** `process`, `reprocess`, `reextract`.
- Projects Hub, associação de reuniões e filtros por projeto → `README.md#regras-do-produto`, `src/meet/store.py`, `src/meet/web/app.py`, `tests/test_projects.py`; **endpoints internos:** `/api/projects/*`, `/api/meetings/*`.
- Task Studio, escopos pessoal/delegado e pacote canônico para outra LLM → `README.md#regras-do-produto`, `src/meet/store.py`, `src/meet/context_export.py`, `tests/test_context_export.py`; **endpoints internos:** `GET /api/tasks`, `POST /api/context/export`.
- Job lifecycle, structured progress, and interruption recovery → `README.md#regras-do-produto`, `src/meet/progress.py`, `src/meet/web/jobs.py`, `tests/test_progress.py`, `tests/test_jobs.py`; **endpoints internos:** `/api/jobs/*`.
- Claude OAuth connection lifecycle → `README.md#regras-do-produto`, `src/meet/anthropic_oauth.py`, `tests/test_settings_local.py`; **endpoints internos:** `/api/auth/anthropic/*`.
- ChatGPT/Codex OAuth lifecycle, model discovery and Responses transport → `README.md#regras-do-produto`, `src/meet/openai_oauth.py`, `src/meet/extract.py`, `tests/test_openai_oauth.py`, `tests/test_extract.py`; **endpoints internos:** `/api/auth/openai/*`.
- Visual LLM model catalog and provider-specific discovery → `README.md#regras-do-produto`, `src/meet/model_catalog.py`, `src/meet/extract.py`, `frontend/src/pages/SettingsPage.tsx`, `tests/test_openai_oauth.py`; **endpoint interno:** `GET /api/settings/models?provider=...`.

## Non-inferable technical facts

- Anthropic Claude Pro/Max OAuth refresh tokens rotate and are effectively single-use. Every successful refresh must persist the returned `refresh_token`; reusing the previous token returns `invalid_grant`.
- A refresh token that already returns `invalid_grant` cannot be repaired locally. The user must complete one new OAuth authorization, after which automatic rotation resumes.
- O backend ChatGPT/Codex exige uma versão compatível do protocolo no catálogo e nas chamadas Responses. A integração declara a versão Codex cujo wire format implementa; não deve usar a versão do produto `context-wrapper` nesse header.

## Conflicts and unknowns

- Anthropic does not publish a stable public OAuth contract for this subscription flow. The implementation follows observed token responses and current Claude-compatible clients; revalidate if Anthropic changes the endpoint or envelope.
- O device-code OAuth e o backend ChatGPT/Codex seguem o contrato implementado no cliente oficial open-source Codex, mas não constituem uma API pública estável. Revalidar endpoints, versão de protocolo e payload quando o Codex mudar esse contrato.

## Durable decisions and gotchas

- Validate LLM credentials before audio/model work so auth failures do not waste a long transcription run.
- OAuth refresh is serialized inside the server process and credentials are re-read after acquiring the lock to avoid consuming one rotating token twice.
- Progress is hybrid by design: report observed work inside measurable stages (including completed LLM blocks) and keep blocking operations such as a single LLM call or final consolidation indeterminate rather than deriving an ETA from historical guesses.
- Model display names are presentation-only. Persist canonical provider IDs unchanged; an empty `llm_model` means automatic provider selection, not a copied default ID.
<!-- business-readme:context:end -->
