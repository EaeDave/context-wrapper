"""Provider-aware LLM model catalog for the web settings UI.

Uses a small bundled fallback plus runtime discovery where the provider exposes
an account-specific catalog. Display names are UI-only; canonical IDs are saved
unchanged in ``Settings.llm_model``.
"""

from __future__ import annotations

from typing import Any

from .config import Settings

CODEX_BACKEND = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = "0.144.4"
CODEX_FALLBACK_MODEL = "gpt-5.5"

ModelOption = dict[str, str | bool]

_BUNDLED_MODELS: dict[str, list[ModelOption]] = {
    "claude-code": [
        {"id": "sonnet", "name": "Claude Sonnet", "recommended": True},
        {"id": "opus", "name": "Claude Opus", "recommended": False},
        {"id": "haiku", "name": "Claude Haiku", "recommended": False},
    ],
    "anthropic": [
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "recommended": True},
        {"id": "claude-fable-5", "name": "Claude Fable 5", "recommended": False},
        {"id": "claude-mythos-5", "name": "Claude Mythos 5", "recommended": False},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "recommended": False},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "recommended": False},
    ],
    "openai": [
        {"id": CODEX_FALLBACK_MODEL, "name": "GPT-5.5", "recommended": True},
        {"id": "gpt-4o", "name": "GPT-4o", "recommended": False},
    ],
    "ollama": [
        {"id": "qwen3:14b", "name": "Qwen 3 14B", "recommended": True},
    ],
}

_DEFAULT_MODELS = {
    "claude-code": "sonnet",
    "anthropic": "claude-sonnet-5",
    "openai": CODEX_FALLBACK_MODEL,
    "ollama": "qwen3:14b",
}


def _model_option(model_id: str, name: str | None = None) -> ModelOption:
    return {"id": model_id, "name": name or model_id, "recommended": False}


def _merge_models(base: list[ModelOption], discovered: list[ModelOption]) -> list[ModelOption]:
    """Merge by canonical model id; discovered metadata wins without reordering."""
    merged = {str(model["id"]): dict(model) for model in base}
    order = [str(model["id"]) for model in base]
    for model in discovered:
        model_id = str(model["id"])
        if model_id not in merged:
            order.append(model_id)
        merged[model_id] = dict(model)
    return [merged[model_id] for model_id in order]


def fetch_codex_models(access_token: str, tokens: dict[str, Any]) -> list[ModelOption]:
    """Fetch models enabled for the connected ChatGPT/Codex account."""
    import httpx

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "originator": "context-wrapper",
        "User-Agent": "context-wrapper/0.1.0",
        "version": CODEX_CLIENT_VERSION,
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-ID"] = str(tokens["account_id"])
    response = httpx.get(
        f"{CODEX_BACKEND}/models",
        params={"client_version": CODEX_CLIENT_VERSION},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    candidates = [
        model
        for model in response.json().get("models", [])
        if (model.get("slug") or model.get("id"))
        and model.get("visibility") == "list"
        and model.get("supported_in_api", True)
    ]
    candidates.sort(key=lambda model: (model.get("priority", 10_000), model.get("slug", "")))
    return [
        {
            "id": str(model.get("slug") or model["id"]),
            "name": str(model.get("display_name") or model.get("name") or model.get("slug") or model["id"]),
            "recommended": index == 0,
        }
        for index, model in enumerate(candidates)
    ]


def _fetch_openai_api_models(api_key: str) -> list[ModelOption]:
    import httpx

    response = httpx.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    response.raise_for_status()
    ids = sorted(
        {
            str(model.get("id"))
            for model in response.json().get("data", [])
            if model.get("id")
            and str(model["id"]).lower().startswith(("gpt-", "chatgpt-", "o1", "o3", "o4"))
        },
        reverse=True,
    )
    return [_model_option(model_id) for model_id in ids]


def _fetch_ollama_models(url: str) -> list[ModelOption]:
    import httpx

    response = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=10)
    response.raise_for_status()
    result: list[ModelOption] = []
    for model in response.json().get("models", []):
        model_id = model.get("name") or model.get("model")
        if model_id:
            result.append(
                {
                    "id": str(model_id),
                    "name": str(model.get("name") or model_id),
                    "recommended": str(model_id) == _DEFAULT_MODELS["ollama"],
                }
            )
    return result


def get_model_catalog(settings: Settings, provider: str) -> dict[str, Any]:
    """Return UI-ready models, falling back to bundled entries on discovery errors."""
    provider = provider.strip().lower()
    if provider not in _BUNDLED_MODELS:
        raise ValueError(f"Provider inválido: {provider!r}")

    bundled = [dict(model) for model in _BUNDLED_MODELS[provider]]
    models = bundled
    default_model = _DEFAULT_MODELS[provider]
    source = "bundled"
    stale = False
    warning: str | None = None

    if provider == "openai":
        from . import openai_oauth

        tokens = openai_oauth.load_tokens(settings)
        try:
            if tokens:
                access = openai_oauth.get_access_token(settings)
                discovered = fetch_codex_models(access, tokens)
                if discovered:
                    models = discovered
                    source = "provider"
                    default_model = str(discovered[0]["id"])
            elif settings.openai_api_key:
                discovered = _fetch_openai_api_models(settings.openai_api_key)
                if discovered:
                    models = _merge_models(bundled, discovered)
                    source = "provider"
                    default_model = "gpt-4o"
            else:
                warning = "Conecte a OpenAI para carregar os modelos disponíveis na sua conta."
        except Exception:
            stale = True
            warning = "Catálogo OpenAI indisponível; exibindo modelos de referência."
    elif provider == "ollama":
        try:
            discovered = _fetch_ollama_models(settings.ollama_url or "http://localhost:11434")
            if discovered:
                models = _merge_models(bundled, discovered)
                source = "provider"
            else:
                warning = "Nenhum modelo instalado no Ollama; você ainda pode informar um ID manualmente."
        except Exception:
            stale = True
            warning = "Ollama indisponível; exibindo o modelo padrão e permitindo ID manual."

    models = [
        {**model, "recommended": str(model["id"]) == default_model}
        for model in models
    ]

    return {
        "provider": provider,
        "default_model": default_model,
        "models": models,
        "source": source,
        "stale": stale,
        "warning": warning,
        "allows_custom": True,
    }
