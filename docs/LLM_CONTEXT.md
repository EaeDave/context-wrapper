<!-- business-readme:context:start -->
# LLM Context

<!-- Admission filter: only context not recoverable from repository code. -->

## Current business rule map

- Pipeline, extração LLM por blocos em reuniões longas e responsabilidade pessoal dos action items → `README.md#regras-do-produto`, `src/meet/pipeline.py`, `src/meet/extract.py`, `tests/test_extract.py`; **jobs internos:** `process`, `reprocess`, `reextract`.
- Job lifecycle, structured progress, and interruption recovery → `README.md#regras-do-produto`, `src/meet/progress.py`, `src/meet/web/jobs.py`, `tests/test_progress.py`, `tests/test_jobs.py`; **endpoints internos:** `/api/jobs/*`.
- Claude OAuth connection lifecycle → `README.md#regras-do-produto`, `src/meet/anthropic_oauth.py`, `tests/test_settings_local.py`; **endpoints internos:** `/api/auth/anthropic/*`.

## Non-inferable technical facts

- Anthropic Claude Pro/Max OAuth refresh tokens rotate and are effectively single-use. Every successful refresh must persist the returned `refresh_token`; reusing the previous token returns `invalid_grant`.
- A refresh token that already returns `invalid_grant` cannot be repaired locally. The user must complete one new OAuth authorization, after which automatic rotation resumes.

## Conflicts and unknowns

- Anthropic does not publish a stable public OAuth contract for this subscription flow. The implementation follows observed token responses and current Claude-compatible clients; revalidate if Anthropic changes the endpoint or envelope.

## Durable decisions and gotchas

- Validate LLM credentials before audio/model work so auth failures do not waste a long transcription run.
- OAuth refresh is serialized inside the server process and credentials are re-read after acquiring the lock to avoid consuming one rotating token twice.
- Progress is hybrid by design: report observed work inside measurable stages (including completed LLM blocks) and keep blocking operations such as a single LLM call or final consolidation indeterminate rather than deriving an ETA from historical guesses.
<!-- business-readme:context:end -->
