import { useState, useEffect } from "react"
import { Link, useNavigate, useSearchParams } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Plus,
  Search,
  Trash2,
  Calendar,
  FolderKanban,
  FolderX,
  MoveRight,
} from "lucide-react"

import * as api from "@/lib/api"
import { formatDuration } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Job, MeetingRow } from "@/lib/types"

import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

// ── Job status badge ─────────────────────────────────────────────────────────

function JobStatusBadge({ status }: { status: Job["status"] }) {
  if (status === "queued")
    return <Badge variant="secondary">na fila</Badge>
  if (status === "running")
    return (
      <Badge variant="default" className="gap-1.5">
        <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-primary-foreground" />
        executando
      </Badge>
    )
  if (status === "done")
    return (
      <Badge variant="outline" className="border-green-600 text-green-600">
        concluído
      </Badge>
    )
  return <Badge variant="destructive">erro</Badge>
}

// ── Media badges ─────────────────────────────────────────────────────────────

function MediaBadges({ row }: { row: MeetingRow }) {
  return (
    <div className="flex flex-wrap gap-1">
      {row.media_ok ? (
        <Badge variant="outline" className="border-green-600 text-green-600">
          vídeo
        </Badge>
      ) : (
        <Badge variant="outline" className="border-destructive text-destructive">
          sem mídia
        </Badge>
      )}
      {row.media_managed ? (
        <Badge variant="secondary">gerido</Badge>
      ) : (
        <Badge variant="outline">link</Badge>
      )}
    </div>
  )
}

// ── MeetingsPage ─────────────────────────────────────────────────────────────

export default function MeetingsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const qc = useQueryClient()

  // URL-based project filter: null = all, "none" = sem projeto, "123" = project id
  const projectIdParam = searchParams.get("project_id")

  // Inline search (local state)
  const [searchInput, setSearchInput] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(searchInput), 300)
    return () => clearTimeout(t)
  }, [searchInput])

  // Row selection
  const [selected, setSelected] = useState<Set<number>>(new Set())

  // Bulk move dialog
  const [bulkMoveOpen, setBulkMoveOpen] = useState(false)
  const [bulkMoveProjectId, setBulkMoveProjectId] = useState<string>("none")

  // ── Queries ─────────────────────────────────────────────────────────────

  // Resolve URL param → api filter value
  const apiFilter =
    projectIdParam === null
      ? undefined
      : projectIdParam === "none"
        ? ("none" as const)
        : Number(projectIdParam)

  const meetingsQuery = useQuery({
    queryKey: ["meetings", projectIdParam ?? "all"],
    queryFn: () => api.getMeetings(apiFilter),
  })

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.getJobs(8),
    refetchInterval: 3000,
  })

  const searchResultsQuery = useQuery({
    queryKey: ["search", debouncedSearch, projectIdParam ?? "all"],
    queryFn: () => api.search(debouncedSearch, apiFilter),
    enabled: debouncedSearch.trim().length > 0,
  })

  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: api.getProjects,
  })

  // ── Mutations ────────────────────────────────────────────────────────────

  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: number[]) => api.bulkDelete(ids),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["meetings"] })
      setSelected(new Set())
      toast.success(`${data.deleted} reunião(ões) excluída(s)`)
    },
    onError: (err: Error) => toast.error(err.message || "Erro ao excluir reuniões"),
  })

  const bulkMoveMutation = useMutation({
    mutationFn: ({ ids, projectId }: { ids: number[]; projectId: number | null }) =>
      api.bulkMoveProject(ids, projectId),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["meetings"] })
      qc.invalidateQueries({ queryKey: ["projects"] })
      setSelected(new Set())
      setBulkMoveOpen(false)
      toast.success(`${data.updated} reunião(ões) atualizada(s)`)
    },
    onError: (err: Error) => toast.error(err.message || "Erro ao mover reuniões"),
  })

  // ── Derived state ────────────────────────────────────────────────────────

  const meetings = meetingsQuery.data ?? []
  const jobs = jobsQuery.data ?? []
  const projects = projectsQuery.data ?? []
  const searchActive = debouncedSearch.trim().length > 0

  const allIds = meetings.map((m) => m.id)
  const allSelected = allIds.length > 0 && allIds.every((id) => selected.has(id))
  const someSelected = allIds.some((id) => selected.has(id))
  const indeterminate = someSelected && !allSelected
  const selectedIds = Array.from(selected)

  function toggleAll() {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(allIds))
  }

  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function setProjectFilter(value: string | null) {
    setSelected(new Set())
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value !== null) next.set("project_id", value)
        else next.delete("project_id")
        return next
      },
      { replace: true },
    )
  }

  function handleBulkMove() {
    const projectId =
      bulkMoveProjectId === "none" ? null : Number(bulkMoveProjectId)
    bulkMoveMutation.mutate({ ids: selectedIds, projectId })
  }

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      {/* Header */}
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Reuniões</h1>
        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
            <Input
              placeholder="Buscar..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="w-52 pl-8"
            />
          </div>
          <Button asChild>
            <Link to="/new">
              <Plus className="size-4" />
              Nova reunião
            </Link>
          </Button>
        </div>
      </div>

      {/* Project filter tabs */}
      <div className="mb-6 flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={() => setProjectFilter(null)}
          className={cn(
            "rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
            projectIdParam === null
              ? "border-primary/30 bg-primary/10 text-primary"
              : "border-input bg-background text-muted-foreground hover:bg-accent/10 hover:text-foreground",
          )}
        >
          Todos
        </button>
        <button
          type="button"
          onClick={() => setProjectFilter("none")}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
            projectIdParam === "none"
              ? "border-primary/30 bg-primary/10 text-primary"
              : "border-input bg-background text-muted-foreground hover:bg-accent/10 hover:text-foreground",
          )}
        >
          <FolderX className="size-3.5" />
          Sem projeto
        </button>
        {projects.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setProjectFilter(String(p.id))}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
              projectIdParam === String(p.id)
                ? "border-primary/30 bg-primary/10 text-primary"
                : "border-input bg-background text-muted-foreground hover:bg-accent/10 hover:text-foreground",
            )}
          >
            <FolderKanban className="size-3.5" />
            {p.name}
          </button>
        ))}
      </div>

      {/* Jobs strip */}
      {jobs.length > 0 && (
        <div className="mb-6">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Jobs recentes
          </p>
          <div className="flex flex-wrap gap-2">
            {jobs.map((job) => {
              const percent =
                job.status === "done" ? 100 : job.progress?.percent ?? 0
              return (
                <button
                  key={job.id}
                  onClick={() => navigate(`/jobs/${job.id}`)}
                  className="flex w-full flex-col gap-1.5 rounded-lg border bg-card px-3 py-2 text-left text-sm transition-colors hover:bg-accent/10 sm:w-56"
                >
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-medium">
                      {job.label}
                    </span>
                    <Badge variant="outline" className="shrink-0 text-xs">
                      {job.kind}
                    </Badge>
                    <JobStatusBadge status={job.status} />
                  </div>
                  {job.progress ? (
                    <div className="flex items-center gap-2">
                      <Progress
                        value={percent}
                        className="h-1.5 flex-1"
                        aria-label={`Progresso: ${job.progress.step_label || job.label}`}
                      />
                      <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                        {Math.round(percent)}%
                      </span>
                    </div>
                  ) : (
                    job.stage && (
                      <span className="truncate text-xs text-muted-foreground">
                        {job.stage}
                      </span>
                    )
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Inline search results */}
      {searchActive && (
        <Card className="mb-6">
          <CardContent className="p-4">
            {searchResultsQuery.isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-3/4" />
              </div>
            ) : !searchResultsQuery.data?.length ? (
              <p className="py-4 text-center text-sm text-muted-foreground">
                Nenhum resultado para &ldquo;{debouncedSearch}&rdquo;
              </p>
            ) : (
              <ul className="divide-y">
                {searchResultsQuery.data.map((r, i) => (
                  <li key={`${r.meeting_id}-${i}`}>
                    <Link
                      to={`/meetings/${r.meeting_id}`}
                      className="-mx-1 flex items-start gap-3 rounded px-1 py-2.5 transition-colors hover:bg-accent/5"
                    >
                      <Badge
                        variant="outline"
                        className="mt-0.5 shrink-0 text-xs"
                      >
                        {r.kind === "action_item" ? "action item" : "transcrição"}
                      </Badge>
                      <div className="min-w-0">
                        <p className="mb-0.5 text-xs text-muted-foreground">
                          {r.title} &middot; {r.date}
                        </p>
                        <p
                          className="text-sm [&_mark]:rounded [&_mark]:bg-primary/20 [&_mark]:px-0.5 [&_mark]:text-primary"
                          dangerouslySetInnerHTML={{ __html: r.snippet }}
                        />
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {/* Meetings table */}
      {meetingsQuery.isLoading ? (
        <Card>
          <CardContent className="space-y-2 p-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </CardContent>
        </Card>
      ) : meetings.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-4 py-16">
            <Calendar className="size-12 text-muted-foreground" />
            <p className="text-muted-foreground">
              {projectIdParam === "none"
                ? "Nenhuma reunião sem projeto."
                : projectIdParam
                  ? "Nenhuma reunião neste projeto."
                  : "Nenhuma reunião ainda."}
            </p>
            {!projectIdParam && (
              <Button asChild>
                <Link to="/new">
                  <Plus className="size-4" />
                  Nova reunião
                </Link>
              </Button>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10 px-4">
                    <Checkbox
                      checked={indeterminate ? "indeterminate" : allSelected}
                      onCheckedChange={toggleAll}
                      aria-label="Selecionar todas"
                    />
                  </TableHead>
                  <TableHead>Título</TableHead>
                  <TableHead className="w-36">Projeto</TableHead>
                  <TableHead className="w-40">Data</TableHead>
                  <TableHead className="w-24">Duração</TableHead>
                  <TableHead className="w-40">Mídia</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {meetings.map((meeting) => (
                  <TableRow
                    key={meeting.id}
                    data-state={selected.has(meeting.id) ? "selected" : ""}
                  >
                    <TableCell className="px-4">
                      <Checkbox
                        checked={selected.has(meeting.id)}
                        onCheckedChange={() => toggleOne(meeting.id)}
                        aria-label={`Selecionar ${meeting.title}`}
                      />
                    </TableCell>
                    <TableCell>
                      <Link
                        to={`/meetings/${meeting.id}`}
                        className="font-medium hover:underline"
                      >
                        {meeting.title}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {meeting.project_name ? (
                        <Link to={`/projects/${meeting.project_id}`}>
                          <Badge
                            variant="secondary"
                            className="gap-1 transition-colors hover:bg-secondary/80"
                          >
                            <FolderKanban className="size-3 shrink-0" />
                            <span className="max-w-[6rem] truncate">
                              {meeting.project_name}
                            </span>
                          </Badge>
                        </Link>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {meeting.date}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {meeting.duration > 0
                        ? formatDuration(meeting.duration)
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <MediaBadges row={meeting} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* Bulk action floating bar */}
      {selectedIds.length > 0 && (
        <div className="fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-lg border bg-background px-4 py-3 shadow-lg">
          <span className="text-sm font-medium">
            {selectedIds.length}{" "}
            {selectedIds.length === 1 ? "selecionada" : "selecionadas"}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setBulkMoveProjectId("none")
              setBulkMoveOpen(true)
            }}
          >
            <MoveRight className="size-4" />
            Mover para projeto
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="destructive" size="sm">
                <Trash2 className="size-4" />
                Excluir
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Excluir reuniões</AlertDialogTitle>
                <AlertDialogDescription>
                  Tem certeza que deseja excluir{" "}
                  {selectedIds.length === 1
                    ? "1 reunião"
                    : `${selectedIds.length} reuniões`}
                  ? Esta ação não pode ser desfeita.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancelar</AlertDialogCancel>
                <AlertDialogAction
                  onClick={() => bulkDeleteMutation.mutate(selectedIds)}
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                >
                  Excluir
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      )}

      {/* Bulk move project dialog */}
      <Dialog open={bulkMoveOpen} onOpenChange={setBulkMoveOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>
              Mover {selectedIds.length}{" "}
              {selectedIds.length === 1 ? "reunião" : "reuniões"} para…
            </DialogTitle>
          </DialogHeader>
          <Select
            value={bulkMoveProjectId}
            onValueChange={setBulkMoveProjectId}
          >
            <SelectTrigger>
              <SelectValue placeholder="Escolher projeto…" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">
                <span className="flex items-center gap-1.5">
                  <FolderX className="size-4" />
                  Sem projeto (desassociar)
                </span>
              </SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={String(p.id)}>
                  <span className="flex items-center gap-1.5">
                    <FolderKanban className="size-4" />
                    {p.name}
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setBulkMoveOpen(false)}
              disabled={bulkMoveMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={handleBulkMove}
              disabled={bulkMoveMutation.isPending}
            >
              {bulkMoveMutation.isPending ? "Movendo…" : "Mover"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
