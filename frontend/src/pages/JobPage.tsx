import { useEffect, useState } from "react"
import { useParams, useNavigate, Link } from "react-router"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, ExternalLink, ArrowLeft, Check, X, Circle } from "lucide-react"

import * as api from "@/lib/api"
import type { Job, ProgressStep } from "@/lib/types"
import { formatDuration } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"

function formatElapsed(seconds: number): string {
  if (seconds < 10) return `${seconds.toFixed(1).replace(".", ",")}s`
  return formatDuration(seconds)
}

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

function ProgressStepIcon({ state }: { state: ProgressStep["state"] }) {
  switch (state) {
    case "done":
      return (
        <span className="flex size-[18px] items-center justify-center rounded-full bg-green-500/15 text-green-600 dark:text-green-400">
          <Check className="size-3" strokeWidth={3} />
        </span>
      )
    case "running":
      return (
        <span className="flex size-[18px] items-center justify-center rounded-full bg-primary/15 text-primary">
          <Loader2 className="size-3 animate-spin" strokeWidth={3} />
        </span>
      )
    case "error":
      return (
        <span className="flex size-[18px] items-center justify-center rounded-full bg-destructive/15 text-destructive">
          <X className="size-3" strokeWidth={3} />
        </span>
      )
    default:
      return (
        <span className="flex size-[18px] items-center justify-center rounded-full border border-border text-muted-foreground">
          <Circle className="size-1.5 fill-current" />
        </span>
      )
  }
}

// Stepper vertical: done/running/pending/error a partir do contrato estruturado.
// Nunca interpreta `stage` — apenas `steps[].state` e `step_percent`.
function ProgressStepper({
  steps,
  activeStepPercent,
}: {
  steps: ProgressStep[]
  activeStepPercent: number | null
}) {
  return (
    <ol className="flex flex-col">
      {steps.map((step, i) => {
        const isLast = i === steps.length - 1
        return (
          <li key={step.key} className="relative flex gap-3">
            {!isLast && (
              <span
                aria-hidden="true"
                className={cn(
                  "absolute left-[8.5px] top-[18px] h-[calc(100%-2px)] w-px",
                  step.state === "done" ? "bg-green-500/40" : "bg-border"
                )}
              />
            )}
            <div className="relative z-10 shrink-0 pt-0.5">
              <ProgressStepIcon state={step.state} />
            </div>
            <div className={cn("min-w-0 flex-1 pb-4", isLast && "pb-0")}>
              <div className="flex items-center justify-between gap-3">
                <p
                  className={cn(
                    "text-sm leading-[18px]",
                    step.state === "done" && "text-foreground",
                    step.state === "running" && "font-medium text-primary",
                    step.state === "pending" && "text-muted-foreground",
                    step.state === "error" && "font-medium text-destructive"
                  )}
                >
                  {step.label}
                </p>
                {typeof step.elapsed_seconds === "number" && (
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                    {formatElapsed(step.elapsed_seconds)}
                  </span>
                )}
              </div>
              {step.state === "running" && (
                <div className="mt-1.5 max-w-xs">
                  {typeof activeStepPercent === "number" ? (
                    <Progress
                      value={activeStepPercent}
                      className="h-1"
                      aria-label={`Progresso de ${step.label}`}
                    />
                  ) : (
                    <div
                      className="h-1 w-full overflow-hidden rounded-full bg-secondary"
                      role="progressbar"
                      aria-label={`Progresso de ${step.label}`}
                      aria-valuetext="Em andamento, duração indeterminada"
                    >
                      <div className="h-full w-2/5 animate-pulse rounded-full bg-primary" />
                    </div>
                  )}
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
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
  const needsOpenAIReconnect =
    job.status === "error" && job.error?.includes("Sessão OpenAI")
  const needsAnthropicReconnect =
    job.status === "error" &&
    (job.error?.includes("invalid_grant") ||
      job.error?.includes("Sessão Claude"))
  const needsLlmReconnect = needsAnthropicReconnect || needsOpenAIReconnect
  const reconnectProvider = needsOpenAIReconnect ? "OpenAI" : "Claude"
  // Jobs terminados com sucesso sempre exibem 100% — nunca inventar número
  // além do que o backend já garante para status "done".
  const displayPercent = job.progress
    ? job.status === "done"
      ? 100
      : job.progress.percent
    : 0

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
          {/* Progresso estruturado — nunca interpreta `stage`, só o contrato */}
          {job.progress ? (
            <div className="space-y-4">
              <div>
                <div className="mb-1.5 flex items-center justify-between gap-2 text-sm">
                  <span className="font-medium">Progresso geral</span>
                  <span className="tabular-nums text-muted-foreground">
                    {Math.round(displayPercent)}%
                  </span>
                </div>
                <Progress
                  value={displayPercent}
                  aria-label="Progresso geral do job"
                />
                {(job.progress.detail || job.progress.step_label) && (
                  <p
                    className={cn(
                      "mt-2 text-sm",
                      job.status === "error"
                        ? "text-destructive"
                        : "text-muted-foreground"
                    )}
                  >
                    {job.progress.detail || job.progress.step_label}
                  </p>
                )}
                {job.progress.elapsed_seconds > 0 && (
                  <p className="mt-1 text-xs tabular-nums text-muted-foreground">
                    Tempo decorrido: {formatDuration(job.progress.elapsed_seconds)}
                  </p>
                )}
              </div>
              {job.progress.steps.length > 0 && (
                <ProgressStepper
                  steps={job.progress.steps}
                  activeStepPercent={job.progress.step_percent}
                />
              )}
            </div>
          ) : (
            <>
              {/* Stage — legado, sem progresso estruturado */}
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
            </>
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
              {needsLlmReconnect && (
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
                  Sua sessão {reconnectProvider} expirou. Reconecte a conta antes de
                  tentar processar a reunião novamente.
                </div>
              )}
              {needsLlmReconnect && (
                <Button asChild>
                  <Link to="/settings">Reconectar {reconnectProvider}</Link>
                </Button>
              )}
              <div className="flex flex-wrap gap-2">
                {!needsLlmReconnect && (
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => {
                      api
                        .retryJob(job.id)
                        .then((next) => {
                          navigate(`/jobs/${next.id}`)
                        })
                        .catch((e: Error) => {
                          console.error(e)
                        })
                    }}
                  >
                    Tentar de novo
                  </Button>
                )}
                <Button variant="outline" size="sm" asChild>
                  <Link to="/new">
                    <ArrowLeft className="size-4" />
                    Nova gravação
                  </Link>
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
