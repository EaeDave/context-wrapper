import type {
  Job,
  MeetingRow,
  MeetingDetail,
  SearchResult,
  BrowseResult,
  Speaker,
  ProcessRequest,
  ProbeResult,
  SettingsInfo,
  AuthorizeResult,
  ActionItem,
  Task,
  VoiceUsage,
  Project,
  ContextExportRequest,
  ContextExportResponse,
} from "./types"

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 204) return undefined as T
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data as T
}

export const getMeetings = (projectFilter?: "none" | number): Promise<MeetingRow[]> => {
  if (projectFilter === undefined) return request("GET", "/api/meetings")
  return request("GET", `/api/meetings?project_id=${projectFilter}`)
}

export const search = (q: string, projectId?: "none" | number): Promise<SearchResult[]> => {
  const base = `/api/search?q=${encodeURIComponent(q)}`
  return request("GET", projectId !== undefined ? `${base}&project_id=${projectId}` : base)
}

export const getMeeting = (id: number): Promise<MeetingDetail> =>
  request("GET", `/api/meetings/${id}`)

export const updateTitle = (
  id: number,
  title: string,
): Promise<{ ok: true }> =>
  request("PATCH", `/api/meetings/${id}`, { title })

export const deleteMeeting = (id: number): Promise<void> =>
  request("DELETE", `/api/meetings/${id}`)

export const bulkDelete = (ids: number[]): Promise<{ deleted: number }> =>
  request("POST", "/api/meetings/bulk-delete", { ids })

export const relink = (
  id: number,
  path: string,
  import_media: boolean,
): Promise<{ ok: true }> =>
  request("POST", `/api/meetings/${id}/relink`, { path, import_media })

export const assignSpeaker = (
  id: number,
  label: string,
  name: string,
): Promise<{ ok: true }> =>
  request("POST", `/api/meetings/${id}/assign`, { label, name })

export const mixMeeting = (id: number): Promise<Job> =>
  request("POST", `/api/meetings/${id}/mix`)

export const startProcess = (body: ProcessRequest): Promise<Job> =>
  request("POST", "/api/process", body)

export const getJobs = (limit = 8): Promise<Job[]> =>
  request("GET", `/api/jobs?limit=${limit}`)

export const getJob = (id: string): Promise<Job> =>
  request("GET", `/api/jobs/${id}`)

/**
 * Open an EventSource for a job's SSE stream.
 * Calls onMessage on each Job update, onError on stream error.
 * Returns a cleanup function — call it to close the stream.
 */
export function jobEvents(
  id: string,
  onMessage: (job: Job) => void,
  onError?: (err: Event) => void,
): () => void {
  const es = new EventSource(`/api/jobs/${id}/events`)
  es.onmessage = (e) => {
    const job = JSON.parse(e.data) as Job
    onMessage(job)
    if (job.status === "done" || job.status === "error") es.close()
  }
  if (onError) es.onerror = onError
  return () => es.close()
}

export const browse = (path?: string): Promise<BrowseResult> =>
  request("GET", path ? `/api/browse?path=${encodeURIComponent(path)}` : "/api/browse")

export const probe = (path: string): Promise<ProbeResult> =>
  request("GET", `/api/probe?path=${encodeURIComponent(path)}`)

export const getSpeakers = (): Promise<Speaker[]> =>
  request("GET", "/api/speakers")

export const deleteSpeaker = (name: string): Promise<void> =>
  request("DELETE", `/api/speakers/${encodeURIComponent(name)}`)

export const getSettings = (): Promise<SettingsInfo> =>
  request("GET", "/api/settings")

export const setHfToken = (token: string): Promise<{ ok: true }> =>
  request("PUT", "/api/settings/hf-token", { token })

export const deleteHfToken = (): Promise<{ ok: true }> =>
  request("DELETE", "/api/settings/hf-token")

export const setLlm = (provider: string, model: string): Promise<{ ok: true }> =>
  request("PUT", "/api/settings/llm", { provider, model })

export const anthropicAuthorize = (): Promise<AuthorizeResult> =>
  request("POST", "/api/auth/anthropic/authorize")

export const anthropicExchange = (
  code: string,
  state: string,
): Promise<{ ok: true; email: string | null }> =>
  request("POST", "/api/auth/anthropic/exchange", { code, state })

export const anthropicDisconnect = (): Promise<{ ok: true }> =>
  request("DELETE", "/api/auth/anthropic")

export const updateActionItem = (
  id: number,
  patch: Partial<{
    what: string
    where: string | null
    details: string | null
    requested_by: string | null
    assigned_to: string[] | null
    priority: ActionItem["priority"]
    status: ActionItem["status"]
    due: string | null
  }>,
): Promise<{ ok: true }> =>
  request("PATCH", `/api/action-items/${id}`, patch)

export const addActionItem = (
  meetingId: number,
  item: {
    what: string
    where?: string | null
    details?: string | null
    requested_by?: string | null
    assigned_to?: string[] | null
    priority?: ActionItem["priority"]
  },
): Promise<{ id: number }> =>
  request("POST", `/api/meetings/${meetingId}/action-items`, item)

export const deleteActionItem = (id: number): Promise<void> =>
  request("DELETE", `/api/action-items/${id}`)

export const getTasks = (
  status = "aberto",
  projectId?: "none" | number,
  scope?: "personal" | "delegated" | "all",
): Promise<Task[]> => {
  const params = new URLSearchParams({ status })
  if (projectId !== undefined) params.set("project_id", String(projectId))
  if (scope) params.set("scope", scope)
  return request("GET", `/api/tasks?${params.toString()}`)
}

export const exportContext = (body: ContextExportRequest): Promise<ContextExportResponse> =>
  request("POST", "/api/context/export", body)

export const updateTurn = (
  meetingId: number,
  body: { seg_ids: number[]; text?: string; speaker?: string },
): Promise<{ ok: true }> =>
  request("PATCH", `/api/meetings/${meetingId}/turn`, body)

export const reprocessMeeting = (
  meetingId: number,
  body: { mic_track?: number; others_track?: number; no_llm?: boolean; num_speakers?: number },
): Promise<Job> =>
  request("POST", `/api/meetings/${meetingId}/reprocess`, body)

export const reextractMeeting = (meetingId: number): Promise<Job> =>
  request("POST", `/api/meetings/${meetingId}/reextract`)

export const renameVoice = (
  name: string,
  newName: string,
): Promise<{ ok: true }> =>
  request("PATCH", `/api/speakers/${encodeURIComponent(name)}`, { new_name: newName })

export const getVoiceUsage = (name: string): Promise<VoiceUsage[]> =>
  request("GET", `/api/speakers/${encodeURIComponent(name)}/usage`)

export const setTuning = (patch: {
  whisper_model?: string
  language?: string
  similarity_threshold?: number
  device?: string
  compute_type?: string
}): Promise<{ ok: true }> =>
  request("PUT", "/api/settings/tuning", patch)

export const testConnection = (
  target: string,
): Promise<{ ok: boolean; detail: string }> =>
  request("POST", "/api/settings/test", { target })

export const getProjects = (): Promise<Project[]> =>
  request("GET", "/api/projects")

export const getProject = (id: number): Promise<Project> =>
  request("GET", `/api/projects/${id}`)

export const createProject = (body: {
  name: string
  description?: string
  repo_path?: string
}): Promise<Project> =>
  request("POST", "/api/projects", body)

export const updateProject = (
  id: number,
  body: { name?: string; description?: string; repo_path?: string },
): Promise<Project> =>
  request("PATCH", `/api/projects/${id}`, body)

export const deleteProject = (id: number): Promise<void> =>
  request("DELETE", `/api/projects/${id}`)

export const setMeetingProject = (
  id: number,
  projectId: number | null,
): Promise<{ ok: true }> =>
  request("PATCH", `/api/meetings/${id}`, { project_id: projectId })

export const bulkMoveProject = (
  ids: number[],
  projectId: number | null,
): Promise<{ updated: number }> =>
  request("PATCH", "/api/meetings/bulk-project", { ids, project_id: projectId })
