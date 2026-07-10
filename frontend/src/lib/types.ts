export type JobStatus = "queued" | "running" | "done" | "error"

export interface Job {
  id: string             // 10 hex
  kind: "process" | "mix"
  label: string
  status: JobStatus
  stage: string          // progresso textual
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
}

export interface SearchResult {
  meeting_id: number
  title: string
  date: string
  kind: "segment" | "action_item"
  snippet: string        // com <mark>…</mark> do FTS
}

export interface ActionItem {
  what: string
  where: string | null
  details: string | null
  requested_by: string | null
  priority: "alta" | "media" | "baixa"
}

export interface TranscriptGroup {
  speaker: string        // "me" | "SPEAKER_XX" | nome atribuído | "?"
  start: number          // segundos — usar p/ seek no player
  end: number
  text: string           // concatenado
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
  groups: TranscriptGroup[]
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
}

export interface ProcessRequest {
  video: string
  title?: string
  mic_track?: number
  others_track?: number
  no_llm?: boolean
  import_media?: boolean
}
