"""Extração de resumo e action items via LLM."""

from __future__ import annotations

import json
import re
import warnings
from abc import ABC, abstractmethod

from .config import Settings
from .models import ActionItem, TranscriptSegment

# Limite aproximado de chars do transcript antes de truncar.
_MAX_TRANSCRIPT_CHARS = 100_000
_TRUNCATE_EACH_SIDE = 40_000

# Prompt do sistema em PT-BR — usa __PARTICIPANTS__ como placeholder.
_SYSTEM_PROMPT = """\
Você é um assistente especialista em análise de reuniões técnicas.
Analise o transcript a seguir e retorne SOMENTE um bloco JSON válido — sem nenhum texto antes ou depois do JSON.

Preserve LITERALMENTE todos os detalhes técnicos mencionados: nomes de endpoints, URLs, \
nomes de telas, nomes de campos, nomes de tabelas, comandos, siglas e tecnologias \
exatamente como aparecem no transcript.

O JSON deve seguir exatamente este schema (sem markdown, sem comentários):
{
  "title": "<título conciso da reunião em PT-BR>",
  "summary": "<resumo executivo em PT-BR, 3-6 frases, preservando termos técnicos literais>",
  "action_items": [
    {
      "what": "<o que precisa ser feito>",
      "where": "<tela, endpoint, módulo, repositório — ou null se não aplicável>",
      "details": "<detalhes técnicos literais mencionados — ou null>",
      "requested_by": "<nome de quem pediu, ou null se não identificado>",
      "assigned_to": "<me se o dono destas notas participa da execução; nome/label de terceiro; ou null se não há responsável claro>",
      "priority": "<alta | media | baixa>"
    }
  ]
}

Participantes identificados: __PARTICIPANTS__

REGRA OBRIGATÓRIA DE RESPONSABILIDADE DOS ACTION ITEMS:
- O participante com label literal "me" é o dono destas notas.
- A lista é pessoal: inclua somente tarefas atribuídas a "me", tarefas compartilhadas que
  incluam "me", ou tarefas acionáveis cujo responsável realmente ficou indefinido.
- NÃO inclua tarefa atribuída explicitamente a outra pessoa ou equipe sem participação de
  "me". Isso vale mesmo que a tarefa seja importante ou tenha sido discutida em detalhes.
- Use "assigned_to": "me" quando "me" participa da execução. Use null somente quando o
  contexto não define responsável. Se identificar responsabilidade exclusiva de terceiro,
  use seu nome/label para classificar, mas omita esse item de action_items.
- "requested_by" é quem pediu a tarefa, não quem vai executá-la. Um terceiro pode pedir uma
  tarefa para "me"; nesse caso ela deve ser incluída.
- Resolva pronomes pelo falante e pelo contexto: se "me" diz "eu faço", a tarefa é de "me";
  se outro participante diz "eu faço", a tarefa é desse participante e deve ser omitida;
  se alguém atribui explicitamente algo a "você" referindo-se a "me", inclua.
- Se "me" não aparecer no transcript, não invente sua identidade. Ainda omita atribuições
  inequívocas a terceiros e mantenha como null somente o que de fato ficou sem dono claro.
"""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model or "claude-sonnet-5"

    def complete(self, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text  # type: ignore[union-attr]


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model or "gpt-4o"

    def complete(self, system: str, user: str) -> str:
        import openai

        client = openai.OpenAI(api_key=self._api_key)
        resp = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


class OllamaProvider(LLMProvider):
    def __init__(self, url: str, model: str) -> None:
        self._url = url.rstrip("/")
        self._model = model or "qwen3:14b"

    def complete(self, system: str, user: str) -> str:
        import httpx

        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        resp = httpx.post(f"{self._url}/api/chat", json=payload, timeout=300.0)
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class ClaudeCodeProvider(LLMProvider):
    """Usa o CLI do Claude Code (`claude -p`) — consome a assinatura, não a API."""

    def __init__(self, model: str) -> None:
        self._model = model or "sonnet"

    def complete(self, system: str, user: str) -> str:
        import shutil
        import subprocess

        if shutil.which("claude") is None:
            raise ValueError(
                "CLI 'claude' não encontrado no PATH. "
                "Instale o Claude Code ou troque llm_provider "
                "(anthropic/openai/ollama) em ~/.config/meet/config.toml."
            )

        cmd = [
            "claude", "-p",
            "--model", self._model,
            "--append-system-prompt", system,
            "--output-format", "text",
        ]
        # Transcript via stdin: evita estourar limite de tamanho de argv.
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p falhou: {proc.stderr[-500:]}")
        return proc.stdout


class AnthropicOAuthProvider(LLMProvider):
    """Provider usando token OAuth (sk-ant-oat…) — sem API key, via assinatura Claude."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.llm_model or "claude-sonnet-5"

    def complete(self, system: str, user: str) -> str:
        import httpx

        from .anthropic_oauth import _check_response, get_access_token

        access = get_access_token(self._settings)
        headers = {
            "Authorization": f"Bearer {access}",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/2.0.0 (external, cli)",
            "Content-Type": "application/json",
        }
        # system como lista de blocks — PRIMEIRO block é o spoof obrigatório.
        system_blocks = [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": system},
        ]
        payload = {
            "model": self._model,
            "max_tokens": 8192,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
        }
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
            )
            _check_response(resp)
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        if not parts:
            blocks = [b.get("type") for b in data.get("content", [])]
            raise RuntimeError(
                f"Resposta sem bloco de texto (stop_reason={data.get('stop_reason')}, "
                f"blocks={blocks})."
            )
        return "".join(parts)



def get_provider(settings: Settings) -> LLMProvider:
    """Instancia o provider LLM configurado, validando credenciais."""
    provider = settings.llm_provider.lower()
    model = settings.llm_model

    if provider == "claude-code":
        return ClaudeCodeProvider(model)

    if provider == "anthropic":
        from .anthropic_oauth import load_tokens

        if load_tokens(settings):
            return AnthropicOAuthProvider(settings)
        if settings.anthropic_api_key:
            return AnthropicProvider(settings.anthropic_api_key, model)
        raise ValueError(
            "Provider 'anthropic' sem credenciais. "
            "Acesse a página Configurações para conectar via OAuth "
            "ou configure ANTHROPIC_API_KEY."
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "openai_api_key não configurado. "
                "Defina a variável de ambiente OPENAI_API_KEY "
                "ou adicione ao ~/.config/meet/config.toml."
            )
        return OpenAIProvider(settings.openai_api_key, model)

    if provider == "ollama":
        return OllamaProvider(settings.ollama_url, model)

    raise ValueError(
        f"Provider LLM desconhecido: {provider!r}. "
        "Valores válidos: 'claude-code', 'anthropic', 'openai', 'ollama'."
    )



def validate_credentials(settings: Settings) -> None:
    """Valida credenciais antes das etapas caras do pipeline."""
    provider = get_provider(settings)
    if isinstance(provider, AnthropicOAuthProvider):
        from .anthropic_oauth import get_access_token

        get_access_token(settings)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _fmt_mm_ss(seconds: float) -> str:
    total = int(seconds)
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _build_transcript(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for seg in segments:
        speaker = seg.speaker or "Desconhecido"
        ts = _fmt_mm_ss(seg.start)
        lines.append(f"[{ts}] {speaker}: {seg.text.strip()}")
    return "\n".join(lines)


def _maybe_truncate(text: str) -> str:
    if len(text) <= _MAX_TRANSCRIPT_CHARS:
        return text
    aviso = (
        f"\n\n[... TRANSCRIPT TRUNCADO: {len(text):,} caracteres no total. "
        f"Exibindo início e fim ({_TRUNCATE_EACH_SIDE:,} chars cada) ...]\n\n"
    )
    warnings.warn(
        f"Transcript truncado de {len(text)} para ~{2 * _TRUNCATE_EACH_SIDE} caracteres.",
        stacklevel=4,
    )
    return text[:_TRUNCATE_EACH_SIDE] + aviso + text[-_TRUNCATE_EACH_SIDE:]


def _parse_json_response(text: str) -> dict:
    """Extrai o primeiro objeto JSON da resposta do LLM.

    Tenta: bloco ```json ... ```, depois primeiro { até último }.
    Levanta ValueError com o texto bruto se não conseguir.
    """
    # Verifica se há fence ```json ou ```
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    candidate_text = fenced.group(1) if fenced else text

    start = candidate_text.find("{")
    end = candidate_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = candidate_text[start : end + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(text)


def _action_item_is_for_owner(d: dict) -> bool:
    """Mantém tarefas do dono ou sem responsável; exclui terceiros explícitos."""
    assigned_to = d.get("assigned_to")
    if assigned_to is None:
        return True
    if isinstance(assigned_to, list):
        return any(
            isinstance(owner, str) and owner.strip().casefold() == "me"
            for owner in assigned_to
        )
    if not isinstance(assigned_to, str):
        return False
    normalized = assigned_to.strip().casefold()
    if re.search(r"(?:^|[^\w])me(?:$|[^\w])", normalized):
        return True
    return normalized in {
        "",
        "null",
        "none",
        "não identificado",
        "nao identificado",
        "indefinido",
        "unknown",
        "unclear",
    }


def _action_item_from_dict(d: dict) -> ActionItem:
    return ActionItem(
        what=d.get("what") or "",
        where=d.get("where") or None,
        details=d.get("details") or None,
        requested_by=d.get("requested_by") or None,
        priority=d.get("priority") or "media",
    )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def extract(
    segments: list[TranscriptSegment],
    participants: list[str],
    settings: Settings,
) -> tuple[str, list[ActionItem], str]:
    """Chama o LLM para extrair resumo, action items e título sugerido.

    Retorna: (summary, action_items, suggested_title).
    Levanta ValueError com o texto bruto se o parse JSON falhar.
    """
    provider = get_provider(settings)

    raw_transcript = _build_transcript(segments)
    transcript = _maybe_truncate(raw_transcript)

    part_str = ", ".join(participants) if participants else "não identificados"
    system = _SYSTEM_PROMPT.replace("__PARTICIPANTS__", part_str)

    response = provider.complete(system, transcript)

    try:
        data = _parse_json_response(response)
    except ValueError:
        raise ValueError(response)

    summary: str = data.get("summary") or ""
    title: str = data.get("title") or ""
    items_raw = data.get("action_items") or []
    action_items = [
        _action_item_from_dict(item)
        for item in items_raw
        if isinstance(item, dict) and _action_item_is_for_owner(item)
    ]

    return summary, action_items, title
