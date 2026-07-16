export type JobStatus = "queued" | "running" | "done" | "error"

export type ProgressStepState = "pending" | "running" | "done" | "error"

export interface ProgressStep {
  key: string
  label: string
  state: ProgressStepState
  elapsed_seconds: number | null
}

/** Progresso estruturado de um job — contrato em src/meet/progress.py. */
export interface ProgressUpdate {
  percent: number              // 0..100, geral
  step: string                 // key da etapa atual
  step_label: string           // rótulo humano da etapa atual
  step_percent: number | null  // 0..100 dentro da etapa; null = indeterminado
  detail: string
  steps: ProgressStep[]
  elapsed_seconds: number
}

export interface Job {
  id: string             // 10 hex
  kind: "process" | "mix" | "reprocess" | "reextract"
  label: string
  status: JobStatus
  stage: string          // progresso textual (legado — nunca interpretar para calcular progresso)
  progress: ProgressUpdate | null  // progresso estruturado; null em jobs legados/sem dados ainda
  error: string | null   // últimos 800 chars do traceback
  meeting_id: number | null   // preenchido quando process termina
  result_path: string | null  // .md gerado
  created_at: string     // ISO UTC
  finished_at: string | null
}

export interface MeetingRow {
  id: number
  date: string           // "YYYY-MM-DD HH:MM"
  title: string
  source: string
  source_origin: string
  media_managed: boolean
  media_ok: boolean      // source existe no disco
  duration: number       // segundos (float)
  project_id: number | null
  project_name: string | null
}

export interface SearchResult {
  meeting_id: number
  title: string
  date: string
  kind: "segment" | "action_item"
  snippet: string        // com <mark>…</mark> do FTS
}

export interface VisualEvidence {
  id: number
  timestamp: number
  thumbnail_url: string
  description: string
  visible_text: string[]
  relevance: "high" | "medium" | "low"
}

export interface ActionItem {
  id: number
  what: string
  where: string | null
  details: string | null
  requested_by: string | null
  assigned_to: string[] | null
  priority: "alta" | "media" | "baixa"
  status: "aberto" | "feito"
  due: string | null
  source_start: number | null
  source_end: number | null
  evidence_quote: string | null
  explicitness: "explicit" | "inferred"
  review_status: "confirmed" | "needs_review"
  visual_evidence?: VisualEvidence[]
}

export interface MeetingFact {
  id: number
  kind: "decision" | "requirement" | "constraint" | "open_question"
  text: string
  source_start: number | null
  source_end: number | null
  evidence_quote: string | null
  explicitness: "explicit" | "inferred"
  review_status: "confirmed" | "needs_review"
  visual_evidence?: VisualEvidence[]
}

export interface TranscriptGroup {
  speaker: string        // "me" | "SPEAKER_XX" | nome atribuído | "?"
  start: number          // segundos — usar p/ seek no player
  end: number
  text: string           // concatenado
  seg_ids: number[]      // ids dos segmentos deste turno agrupado
}

export interface MeetingDetail {
  id: number
  title: string
  date: string
  duration: number       // segundos
  source: string
  source_origin: string
  media_managed: boolean
  md_path: string | null
  source_exists: boolean
  has_video: boolean
  preview_ready: boolean
  preview_full_ready: boolean
  mix_ready: boolean           // .listen.m4a já gerado
  source_w: number
  source_h: number
  quality_web_h: number
  quality_full_h: number
  participants: string[]
  summary: string
  action_items: ActionItem[]
  pending: string[]            // labels SPEAKER_XX aguardando nome
  speaker_matches: Record<string, number>  // nome → similaridade cosseno
  groups: TranscriptGroup[]
  project_id: number | null
  project_name: string | null
  facts?: MeetingFact[]
  visual_evidence?: VisualEvidence[]
}

export interface BrowseEntry {
  name: string
  path: string           // absoluto
  kind: "dir" | "file"
  size: number | null    // bytes; null p/ dir
}

export interface BrowseResult {
  path: string           // dir atual (absoluto)
  parent: string | null  // null na raiz
  quick: string[]        // quick-dirs existentes
  entries: BrowseEntry[]
}

export interface ProbeResult {
  audio_streams: number
  video_streams: number
}

export interface Speaker {
  name: string
  dims: number           // len(blob)//4
  meetings: number       // nº de reuniões com segmentos deste falante
}

export interface VoiceUsage {
  meeting_id: number
  title: string
  date: string
  count: number          // nº de segmentos com este falante nessa reunião
}

export interface ProcessRequest {
  video: string
  title?: string
  mic_track?: number
  others_track?: number
  num_speakers?: number  // nº de falantes remotos (0 = automático)
  no_llm?: boolean
  analyze_visual?: boolean
  import_media?: boolean
  project_id?: number | null
}

export interface SettingsInfo {
  hf_token: { configured: boolean; masked: string | null; source: "local" | "config" | "env" | null }
  anthropic: { connected: boolean; email: string | null; expires: number | null; api_key_configured: boolean }
  openai: { connected: boolean; email: string | null; expires: number | null; plan: string | null; api_key_configured: boolean }
  llm: { provider: string; model: string }
  tuning: {
    whisper_model: string
    language: string
    similarity_threshold: number
    device: string
    compute_type: string
  }
}

export interface LlmModelOption {
  id: string
  name: string
  recommended: boolean
}

export interface LlmModelCatalog {
  provider: string
  default_model: string
  models: LlmModelOption[]
  source: "bundled" | "provider"
  stale: boolean
  warning: string | null
  allows_custom: boolean
}

export interface AuthorizeResult { url: string; state: string }
export interface OpenAIAuthorizeResult { url: string; state: string; user_code: string }

export interface Task {
  id: number
  meeting_id: number
  meeting_title: string
  date: string
  what: string
  where: string | null
  details: string | null
  requested_by: string | null
  assigned_to: string[] | null
  priority: "alta" | "media" | "baixa"
  status: "aberto" | "feito"
  due: string | null
  project_id: number | null
  project_name: string | null
  review_status?: "confirmed" | "needs_review"
  explicitness?: "explicit" | "inferred"
  evidence_quote?: string | null
}

export interface ContextExportRequest {
  task_ids: number[]
  objective?: string
  format?: "markdown" | "json"
  include_summary?: boolean
  include_facts?: boolean
  include_evidence?: boolean
  include_transcript?: boolean
}

export interface ContextExportResponse {
  format: "markdown" | "json"
  filename: string
  content: string
  task_count: number
  meeting_count: number
}

export interface Project {
  id: number
  name: string
  description: string
  repo_path: string
  meeting_count: number
  open_task_count: number
  done_task_count: number
  last_meeting_date: string | null
  created_at: string
  updated_at: string
}
