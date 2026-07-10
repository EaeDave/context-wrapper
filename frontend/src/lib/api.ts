import type {
  Job,
  MeetingRow,
  MeetingDetail,
  SearchResult,
  BrowseResult,
  Speaker,
  ProcessRequest,
  ProbeResult,
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

export const getMeetings = (): Promise<MeetingRow[]> =>
  request("GET", "/api/meetings")

export const search = (q: string): Promise<SearchResult[]> =>
  request("GET", `/api/search?q=${encodeURIComponent(q)}`)

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
