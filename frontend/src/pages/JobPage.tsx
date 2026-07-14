import { useEffect, useState } from "react"
import { useParams, useNavigate, Link } from "react-router"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, ExternalLink, ArrowLeft } from "lucide-react"

import * as api from "@/lib/api"
import type { Job } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

function StatusBadge({ status }: { status: Job["status"] }) {
  switch (status) {
    case "queued":
      return <Badge variant="secondary">Na fila</Badge>
    case "running":
      return (
        <Badge variant="default" className="gap-1.5">
          <span className="inline-block size-1.5 rounded-full bg-primary-foreground animate-pulse" />
          Processando
        </Badge>
      )
    case "done":
      return (
        <Badge
          variant="outline"
          className="border-green-500 text-green-600 dark:text-green-400"
        >
          Concluído
        </Badge>
      )
    case "error":
      return <Badge variant="destructive">Erro</Badge>
  }
}

export default function JobPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const {
    data: initialJob,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["job", id],
    queryFn: () => api.getJob(id!),
    enabled: !!id,
    refetchOnWindowFocus: false,
  })

  const [liveJob, setLiveJob] = useState<Job | null>(null)
  const job = liveJob ?? initialJob

  // Open SSE stream for active jobs once initial data loads
  useEffect(() => {
    if (!id || !initialJob) return
    if (initialJob.status !== "queued" && initialJob.status !== "running") return

    return api.jobEvents(id, (updated) => {
      setLiveJob(updated)
      queryClient.setQueryData(["job", id], updated)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, initialJob?.id])

  if (isLoading) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-8 space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-48 w-full rounded-lg" />
      </div>
    )
  }

  if (error || !job) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-8 space-y-4">
        <p className="text-destructive text-sm">Job não encontrado.</p>
        <Button variant="outline" size="sm" onClick={() => navigate(-1)}>
          <ArrowLeft className="size-4" />
          Voltar
        </Button>
      </div>
    )
  }

  const isActive = job.status === "queued" || job.status === "running"
  const needsAnthropicReconnect =
    job.status === "error" &&
    (job.error?.includes("invalid_grant") ||
      job.error?.includes("Reconecte sua conta"))

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3 mb-6">
        <h1 className="text-2xl font-semibold tracking-tight flex-1 min-w-0 truncate">
          {job.label}
        </h1>
        <Badge variant="outline" className="shrink-0">
          {{
            process: "Processamento",
            mix: "Mixagem",
            reprocess: "Reprocessamento",
            reextract: "Re-extração",
          }[job.kind] ?? job.kind}
        </Badge>
        <StatusBadge status={job.status} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium">Detalhes do job</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Stage — highlighted when active */}
          {job.stage && (
            <div className="flex items-center gap-2">
              {isActive && (
                <Loader2 className="size-4 text-primary animate-spin shrink-0" />
              )}
              <span
                className={
                  isActive
                    ? "text-primary font-medium"
                    : "text-muted-foreground text-sm"
                }
              >
                {job.stage}
              </span>
            </div>
          )}

          {/* Queued: spinner + message */}
          {job.status === "queued" && !job.stage && (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Loader2 className="size-4 animate-spin" />
              Aguardando na fila…
            </div>
          )}

          {/* Timestamps */}
          <div className="flex flex-wrap gap-6 text-xs text-muted-foreground">
            <span>Criado: {job.created_at.replace("T", " ").slice(0, 16)}</span>
            {job.finished_at && (
              <span>Finalizado: {job.finished_at.replace("T", " ").slice(0, 16)}</span>
            )}
          </div>

          {/* Done: navigation buttons */}
          {job.status === "done" && (
            <div className="flex flex-wrap gap-2 pt-1">
              {job.meeting_id != null && (
                <Button asChild>
                  <Link to={`/meetings/${job.meeting_id}`}>Ver reunião</Link>
                </Button>
              )}
              {job.result_path && (
                <Button variant="outline" asChild>
                  <a
                    href={`/files?path=${encodeURIComponent(job.result_path)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <ExternalLink className="size-4" />
                    {job.kind === "mix" ? "Abrir áudio" : "Abrir .md"}
                  </a>
                </Button>
              )}
            </div>
          )}

          {/* Error: traceback + retry */}
          {job.status === "error" && (
            <div className="space-y-3">
              {job.error && (
                <pre className="rounded-md bg-muted px-4 py-3 text-xs font-mono text-destructive overflow-x-auto whitespace-pre-wrap break-words">
                  {job.error}
                </pre>
              )}
              {needsAnthropicReconnect && (
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
                  Sua sessão Claude expirou. Reconecte a conta antes de tentar
                  processar a reunião novamente.
                </div>
              )}
              {needsAnthropicReconnect && (
                <Button asChild>
                  <Link to="/settings">Reconectar Claude</Link>
                </Button>
              )}
              <Button variant="outline" size="sm" asChild>
                <Link to="/new">
                  <ArrowLeft className="size-4" />
                  Tentar de novo
                </Link>
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
