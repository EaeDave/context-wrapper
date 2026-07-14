# context-wrapper

Transforma gravações de reunião em notas acionáveis: transcreve (Whisper na GPU),
identifica quem falou o quê (pyannote + banco de vozes) e extrai action items
estruturados via LLM — tudo local, exceto a chamada opcional ao LLM.

```
gravação (OBS) ──> wav 16k ──> transcrição + diarização ──> LLM ──> markdown + busca
```

<!-- business-readme:business-rules:start -->
## Regras do produto

- Uma gravação pode ter uma faixa única ou faixas separadas de microfone e
  participantes. Com faixas separadas, a voz do usuário entra como `me`; com
  faixa única, todos os falantes passam pela diarização.
- O processamento completo prepara o áudio, transcreve, diariza, reconhece
  vozes, extrai resumo/action items, salva o resultado e, por padrão, importa a
  mídia. A opção “Sem LLM” mantém transcrição e diarização, sem resumo nem
  action items. **Job interno:** `process`; **endpoint interno:** `POST /api/process`.
- Quando o LLM está habilitado, suas credenciais são validadas antes das etapas
  caras. Sessão Claude expirada/revogada encerra o job imediatamente e orienta
  reconexão em Configurações, evitando perder minutos transcrevendo antes da
  falha. **Integração externa:** Anthropic OAuth; **job interno:** `process`.
- A autenticação Claude Pro/Max renova tokens automaticamente. Cada renovação
  persiste o novo refresh token rotacionado; token já revogado exige uma nova
  autenticação manual. **Endpoints internos:** `/api/auth/anthropic/*`.
- Jobs são executados em fila, um por vez, porque Whisper e pyannote compartilham
  GPU. O estado é persistido; jobs interrompidos por reinício do servidor viram
  erro em vez de serem retomados silenciosamente. **Endpoints internos:**
  `/api/jobs` e `/api/jobs/{id}/events`.
- Durante o processamento, a interface mostra porcentagem geral ponderada,
  etapa atual, tempo total e tempo medido de cada etapa. Preparação de áudio,
  transcrição, diarização e importação usam avanço observado; chamadas sem
  granularidade confiável, como a extração LLM, aparecem como indeterminadas em
  vez de inventar uma estimativa. Progresso e tempos são persistidos e
  transmitidos em tempo real. **Endpoints internos:** `/api/jobs` e
  `/api/jobs/{id}/events`.
- Falantes desconhecidos ficam como `SPEAKER_XX`. Ao nomeá-los, a voz pode ser
  reconhecida automaticamente em reuniões futuras conforme o limiar de
  similaridade configurado. **Endpoint interno:** `/api/meetings/{id}/assign`.
- Excluir uma reunião remove banco, markdown e mídia gerida. Mídia apenas
  vinculada externamente não é apagada. **Endpoint interno:**
  `DELETE /api/meetings/{id}`.
<!-- business-readme:business-rules:end -->

<!-- business-readme:technical:start -->

## Requisitos

- Linux com GPU NVIDIA (testado: RTX 3060 12GB), `ffmpeg` no PATH
- [uv](https://docs.astral.sh/uv/)
- Token Hugging Face (diarização); Claude Code instalado OU uma API key de LLM (extração)

## Setup

```sh
uv sync
```

### 1. Token Hugging Face (diarização)

1. Crie um token em <https://hf.co/settings/tokens>
2. Aceite os termos em <https://hf.co/pyannote/speaker-diarization-community-1>
3. `export HF_TOKEN=hf_...`

### 2. LLM (extração de action items)

Default é `claude-code`: usa o CLI do Claude Code já instalado (`claude -p`),
consumindo sua **assinatura** (Pro/Max) — sem API key, sem custo extra.
Divide o rate limit com seu uso normal do Claude Code.

Alternativas via `~/.config/meet/config.toml`:

```toml
llm_provider = "anthropic"  # API oficial ($; exige ANTHROPIC_API_KEY)
# llm_provider = "openai"   # exige OPENAI_API_KEY
# llm_provider = "ollama"   # 100% local, ex.: llm_model = "qwen3:14b"
llm_model = ""              # vazio = default do provider
```

### 3. OBS multi-track (recomendado)

Gravar sua voz numa track separada corta a maior parte do trabalho de diarização —
sua fala já entra identificada como `me`.

1. Settings → Output → Recording → Audio Track: marque **1 e 2**
2. Edit → Advanced Audio Properties:
   - **Mic/Aux** → somente track **1**
   - **Desktop Audio** → somente track **2**
3. Settings → Output → Recording Format: **Matroska (mkv)**.
   **Nunca mp4**: mp4 no OBS mantém só a track 1 — o áudio dos outros
   participantes (track 2) é descartado silenciosamente.

Gravações com uma track só também funcionam (diarização cobre todos os falantes).

### Desktop Audio “baixo” (Discord / track 2)

Na prática o OBS **não está atenuando** o desktop: ele grava o monitor digital
do sink default (`pulse_output_capture` → `device_id=default`) com volume 1.0.
Medições típicas de call Discord nessas tracks:

| Fonte | Peak típico | O que significa |
|-------|-------------|-----------------|
| Track 2 (desktop) | ~−10 dBFS | normal para VoIP; tem ~10 dB de headroom |
| Track 1 (mic) | ~−17 dBFS | frequentemente **mais baixa** que o desktop |
| Monitor PipeWire (teste com tom) | igual à fonte (±1–2 dB) | path digital sem perda |

Se a voz dos outros **parece** baixa, as causas reais (em ordem) são:

1. **Player só toca a track 1** — num mkv multi-track o default é o mic.
   Ouça a track do Discord com:
   ```sh
   mpv --aid=2 reuniao.mkv
   ```
2. **Volume por-usuário no Discord** — clique direito no participante → slider
   Volume (isso muda o que o OBS captura).
3. **Volume do app no PipeWire** — com a call tocando, `pavucontrol` → Playback
   → stream do Discord em 100% (OBS captura **pós** volume de app).
4. **Headset alto no hardware, sinal digital quieto** — no fone a call soa forte
   porque o amp do H510 está no talo; o arquivo grava o nível digital (bem mais
   baixo). Não é bug do OBS.

Ajustes se quiser a gravação mais “cheia” (opcional — o pipeline já aplica
`speechnorm` e usa Whisper `large-v3`, que aguentam SNR ruim):

- OBS → Desktop Audio → Filters → **Gain** +6 a +12 dB (peaks em −10 dB
  aguentam +9 dB sem clipar).
- Subir o slider do amigo no Discord **antes** de gravar.

## Uso

```sh
uv run meet process reuniao.mkv            # pipeline completo
uv run meet process reuniao.mkv --no-llm   # só transcript diarizado
```

Saída: `~/reunioes/YYYY-MM-DD-titulo.md` com resumo, tabela de action items
(o quê / onde / detalhes técnicos / quem pediu / prioridade) e transcript completo.

### Ouvir a gravação completa (mic + Discord)

O mkv multi-track mantém as faixas **separadas** (bom pro pipeline). Player
normal só toca a track 1. Pra escutar **você + o pessoal** juntos:

```sh
uv run meet play reuniao.mkv     # toca o mix na hora (mpv/ffplay)
uv run meet mix  reuniao.mkv     # gera reuniao.listen.m4a (duplo-clique depois)
```

O `.listen.m4a` é só pra consulta humana — o `process` continua usando as
tracks separadas do mkv original.

### Interface web

UI local em **React + Tailwind + shadcn/ui** (SPA servida pelo FastAPI): listar
reuniões, processar gravações, acompanhar jobs em tempo real (SSE), ver
transcript com seek no player, action items, nomear falantes, busca no
histórico (inline + paleta `Ctrl+K`).

O frontend precisa ser compilado uma vez (e a cada mudança em `frontend/`):

```sh
cd frontend && bun install && bun run build   # gera src/meet/web/dist/
```

```sh
uv run meet serve            # http://127.0.0.1:8741 (abre o browser)
uv run meet serve --no-open  # só sobe o servidor
uv run meet serve -p 9000    # outra porta
```

Dev do frontend com hot-reload: `cd frontend && bun run dev` (proxy pra API em
`127.0.0.1:8741` — suba o `meet serve` junto).

Só escuta em `127.0.0.1` por padrão (dados de reunião ficam na máquina).

#### SQLite + mídia gerida

- **Banco:** `~/.local/share/meet/meet.db` (transcript, action items, paths)
- **Vídeo importado (default):** `~/.local/share/meet/media/{id}/original.ext`
- **Lista/detalhe** mostram se o vídeo está **ok** ou **ausente**
- **CRUD:** editar título, excluir reunião (apaga DB + pasta media + markdown)
- **Relink:** se o arquivo sumiu, apontar de novo (com ou sem reimportar)
- Processar com checkbox “Importar vídeo pro meet”; CLI: `--no-import` pra só linkar

O path do OBS fica em `source_origin` (histórico). O player usa o path canônico
(`source`), de preferência dentro de `media/{id}/`.

### Banco de vozes

Na primeira reunião os falantes saem como `SPEAKER_00`, `SPEAKER_01`...
Nomeie uma vez:

```sh
uv run meet speakers assign 1 SPEAKER_00 "Chefe"
```

Nas próximas reuniões a voz é reconhecida automaticamente (similaridade de
embedding, limiar configurável via `similarity_threshold`).

```sh
uv run meet speakers list          # vozes conhecidas
uv run meet speakers rename A B    # renomear
```

### Histórico

```sh
uv run meet list                   # reuniões processadas
uv run meet search "tela de login" # busca full-text (FTS5) em falas e action items
```

## Configuração

Env vars (`HF_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MEET_LLM_PROVIDER`,
`MEET_LLM_MODEL`, `MEET_OLLAMA_URL`) têm precedência sobre
`~/.config/meet/config.toml`. Campos disponíveis: ver `src/meet/config.py`
(`whisper_model`, `language`, `device`, `similarity_threshold`, `output_dir`...).

## Notas técnicas

- Whisper `large-v3-turbo` int8 e pyannote rodam sequencialmente e liberam VRAM
  entre etapas — cabe folgado em 12GB.
- torch é fixado no index cu128 para alinhar com o runtime CUDA 12 exigido pelo
  ctranslate2 (faster-whisper); não troque para cu13x sem revalidar.
- Dados: `~/.local/share/meet/meet.db` (SQLite + FTS5), embeddings pendentes em
  `~/.local/share/meet/pending/`.

<!-- business-readme:technical:end -->
