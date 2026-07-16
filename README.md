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
- Antes de resumo, tarefas e fatos, a LLM faz uma revisão conservadora do
  transcript: corrige somente erros fonéticos ou terminológicos com confiança
  mínima de 90%, sustentados pela própria reunião ou pelo histórico do projeto.
  Termos corretos descobertos em qualquer bloco ajudam todos os demais; números
  não podem mudar. Cada ajuste preserva o texto bruto do Whisper, a justificativa
  e a confiança, e a página da reunião permite revelar essa auditoria. Correções
  confirmadas alimentam automaticamente o vocabulário transitório das próximas
  reuniões do mesmo projeto e são repassadas ao Whisper como `hotwords`, sem
  cadastro ou configuração manual. Falha nessa revisão mantém o transcript bruto.
  A opção “Sem LLM” não executa a revisão. **Jobs internos:** `process`,
  `reprocess`, `reextract`; **integração externa:** LLM configurada.
- A análise de tela é opcional. Quando habilitada em uma gravação com vídeo, o
  pipeline prioriza momentos em que a fala referencia a interface, acrescenta
  amostras periódicas de segurança, remove telas quase idênticas e envia apenas
  os frames selecionados ao provider configurado. As evidências usadas ficam
  persistidas na reunião: uma galeria mostra thumbnail, timestamp, descrição e
  texto visível; tarefas e fatos exibem as imagens temporalmente vinculadas.
  Clicar em qualquer thumbnail leva o player ao instante correspondente. Falha
  em um frame ou transporte sem imagens degrada para o transcript. **Jobs
  internos:** `process`, `reprocess`; **endpoint interno:** `POST /api/process`;
  **integrações externas:** Anthropic, OpenAI por API key ou ChatGPT/Codex OAuth,
  e Ollama com modelo visual.
- A extração preserva todas as tarefas discutidas, inclusive as atribuídas só a
  terceiros, e separa quem pediu de quem executará. Também registra decisões,
  requisitos, restrições e questões em aberto. Tarefas e fatos guardam o trecho
  temporal, a citação usada como evidência, se o conteúdo foi explícito ou
  inferido e se a evidência pôde ser confirmada no transcript. A mesma regra
  vale ao processar, reprocessar ou reextrair. **Jobs internos:** `process`,
  `reprocess`, `reextract`.
- Na página da reunião, o timestamp de uma tarefa ou fato navega ao mesmo tempo
  para a mídia e para o turno correspondente no transcript. A citação da
  evidência desambigua falas sobrepostas; o turno recebe foco, rolagem
  centralizada e destaque temporário para preservar o contexto da extração.
- O Task Studio abre como lista pessoal: inclui tarefas sem responsável ou com
  `me` entre os responsáveis e deixa tarefas exclusivas de terceiros na visão
  “Delegadas”. É possível alternar entre abertas, concluídas e todas, além de
  filtrar por projeto, prioridade e busca. **Endpoint interno:** `GET /api/tasks`.
- Reuniões podem ficar sem projeto ou ser organizadas em projetos com nome,
  descrição e caminho do repositório. Um projeto agrega reuniões e contadores
  das tarefas pessoais; excluí-lo apenas desassocia suas reuniões, sem apagar
  reuniões ou conteúdo. **Endpoints internos:** `/api/projects/*`,
  `PATCH /api/meetings/{id}` e `PATCH /api/meetings/bulk-project`.
- Tarefas selecionadas no Task Studio viram um pacote de contexto para outra
  LLM, em Markdown ou JSON. O pacote pode incluir objetivo, resumos, fatos,
  evidências e, opcionalmente, o transcript integral; ele só agrupa conteúdo já
  persistido, sem pedir nova geração à LLM. **Endpoint interno:**
  `POST /api/context/export`.
- Em reuniões com mais de 10 minutos ou transcript denso, nenhum trecho
  intermediário é descartado para caber no contexto da LLM. O transcript é
  analisado em blocos temporais sobrepostos de até 8 minutos. Fatos e tarefas
  vêm desses blocos, são unidos em ordem e têm duplicatas do overlap removidas
  sem perder evidências ou timestamps; a consolidação final gera somente título
  e resumo, evitando que listas extensas sejam cortadas. Decisões, requisitos,
  correções posteriores e tarefas discutidas no meio da conversa participam do
  resultado final. A reunião curta mantém uma única chamada. Resposta cortada
  pelo limite da LLM é repetida uma vez em formato mais conciso; falhas
  temporárias do gateway Anthropic também são repetidas antes de falhar o job. **Jobs internos:**
  `process`, `reprocess`, `reextract`; **integração externa:** LLM configurada.
- Quando o LLM está habilitado, suas credenciais são validadas antes das etapas
  caras. Sessão Claude ou OpenAI expirada/revogada encerra o job imediatamente e
  orienta reconexão em Configurações, evitando transcrever antes da falha.
  **Integrações externas:** Anthropic OAuth e OpenAI OAuth; **job interno:**
  `process`.
- A autenticação Claude Pro/Max renova tokens automaticamente. Cada renovação
  persiste o novo refresh token rotacionado; token já revogado exige uma nova
  autenticação manual. **Endpoints internos:** `/api/auth/anthropic/*`.
- ChatGPT Plus/Pro também pode ser conectado por device code, usando a assinatura
  no backend Codex sem API key; tokens são renovados e persistidos localmente.
  Com modelo vazio, o app escolhe o primeiro modelo visível e suportado no
  catálogo da conta; um modelo explícito continua prevalecendo. **Endpoints
  internos:** `/api/auth/openai/*`.
- Em Configurações, o modelo é escolhido visualmente por provider. Anthropic e
  Claude Code usam um catálogo de referência; OpenAI e Ollama atualizam a lista
  com os modelos disponíveis na conta ou instalação. “Automático” preserva o
  default do provider, e “Outro modelo” aceita um ID canônico explícito sem
  reescrevê-lo. Falha de descoberta não bloqueia a configuração: a interface
  mantém referências locais e mostra o aviso. **Endpoint interno:**
  `GET /api/settings/models?provider=...`.
- Jobs são executados em fila, um por vez, porque Whisper e pyannote compartilham
  GPU. O estado é persistido; jobs interrompidos por reinício do servidor viram
  erro em vez de serem retomados silenciosamente. **Endpoints internos:**
  `/api/jobs` e `/api/jobs/{id}/events`.
- Durante o processamento, a interface mostra porcentagem geral ponderada,
  etapa atual, tempo total e tempo medido de cada etapa. Preparação de áudio,
  transcrição, diarização e importação usam avanço observado. Em reuniões
  longas, a extração LLM mostra `Analisando bloco N de M` e avança quando cada
  bloco termina; a consolidação final aparece separadamente como indeterminada.
  Reuniões curtas continuam indeterminadas durante sua chamada LLM única, sem
  inventar ETA. Progresso e tempos são persistidos e transmitidos em tempo real.
  **Jobs internos:** `process`, `reprocess`, `reextract`; **endpoints internos:**
  `/api/jobs` e `/api/jobs/{id}/events`.
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
- Token Hugging Face (diarização); assinatura Claude/ChatGPT, API key de LLM ou Ollama (extração)

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
llm_provider = "anthropic"  # API key ou assinatura Claude Pro/Max via Configurações
# llm_provider = "openai"   # API key ou assinatura ChatGPT Plus/Pro via Configurações
# llm_provider = "ollama"   # 100% local, ex.: llm_model = "qwen3:14b"
llm_model = ""              # vazio = modelo padrão/disponível do provider
```

Os mesmos valores podem ser escolhidos visualmente em **Configurações → Provider
LLM**. Modelo “Automático” acompanha o catálogo do provider; IDs customizados
continuam aceitos para modelos novos ou locais.

A opção **Analisar conteúdo da tela** aparece ao processar ou reprocessar uma
gravação com vídeo. Ela envia frames relevantes ao provider; Anthropic por API
key/OAuth, OpenAI por API key e Ollama com modelo vision são suportados neste
MVP. Claude Code CLI e ChatGPT/Codex OAuth continuam com extração textual.

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
e organizar reuniões por projeto, processar gravações, acompanhar jobs em tempo
real (SSE), revisar transcript/fatos/action items com evidência, gerenciar
tarefas no Task Studio, exportar contexto para outra LLM, nomear falantes e
buscar no histórico (inline + paleta `Ctrl+K`).

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
