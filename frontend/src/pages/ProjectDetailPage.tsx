import { useState } from "react"
import { Link, useParams, useNavigate } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  ArrowLeft,
  FolderKanban,
  Pencil,
  Trash2,
  Calendar,
  CheckSquare2,
  GitBranch,
  Users,
} from "lucide-react"

import * as api from "@/lib/api"
import type { Project, MeetingRow, Task } from "@/lib/types"
import { formatDuration } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"

// ── Tab ───────────────────────────────────────────────────────────────────────

type TabValue = "meetings" | "tasks"

// ── Meetings sub-list ─────────────────────────────────────────────────────────

function MeetingsList({ projectId }: { projectId: number }) {
  const { data: meetings = [], isLoading } = useQuery<MeetingRow[]>({
    queryKey: ["meetings", String(projectId)],
    queryFn: () => api.getMeetings(projectId),
  })

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((n) => (
          <div key={n} className="h-14 animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
    )
  }

  if (meetings.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 py-16 text-muted-foreground">
        <Calendar className="size-10 opacity-30" />
        <p className="text-sm">Nenhuma reunião neste projeto.</p>
        <p className="max-w-sm text-center text-xs">
          Atribua reuniões via seletor no detalhe de cada reunião ou em lote na
          lista de reuniões.
        </p>
        <Button variant="outline" size="sm" asChild>
          <Link to="/?project_id=none">Ver reuniões sem projeto</Link>
        </Button>
      </div>
    )
  }

  return (
    <ul className="divide-y divide-border rounded-lg border border-input">
      {meetings.map((m) => (
        <li key={m.id}>
          <Link
            to={`/meetings/${m.id}`}
            className="flex items-center gap-4 px-4 py-3 transition-colors hover:bg-accent/5"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{m.title}</p>
              <p className="text-xs text-muted-foreground">{m.date}</p>
            </div>
            <Badge variant="secondary" className="shrink-0 text-xs">
              {m.duration > 0 ? formatDuration(m.duration) : "—"}
            </Badge>
          </Link>
        </li>
      ))}
    </ul>
  )
}

// ── Tasks sub-list ────────────────────────────────────────────────────────────

function TasksList({ projectId }: { projectId: number }) {
  const qc = useQueryClient()
  const [status, setStatus] = useState<"aberto" | "feito" | "todos">("aberto")

  const { data: tasks = [], isLoading } = useQuery<Task[]>({
    queryKey: ["tasks", status, String(projectId)],
    queryFn: () => api.getTasks(status, projectId),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, newStatus }: { id: number; newStatus: Task["status"] }) =>
      api.updateActionItem(id, { status: newStatus }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
    onError: () => toast.error("Erro ao atualizar tarefa"),
  })

  const STATUS_FILTERS: { value: "aberto" | "feito" | "todos"; label: string }[] = [
    { value: "aberto", label: "Em aberto" },
    { value: "feito", label: "Concluídas" },
    { value: "todos", label: "Todas" },
  ]

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((n) => (
          <div key={n} className="h-14 animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
    )
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex w-fit gap-1 rounded-lg border border-input bg-muted/40 p-1">
          {STATUS_FILTERS.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              onClick={() => setStatus(value)}
              className={cn(
                "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                status === value
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <Link
          to={`/tasks?project_id=${projectId}`}
          className="ml-auto text-xs text-muted-foreground underline-offset-2 transition-colors hover:underline hover:text-foreground"
        >
          Abrir no Task Studio →
        </Link>
      </div>
      {tasks.length === 0 ? (
        <div className="flex flex-col items-center gap-3 py-16 text-muted-foreground">
          <CheckSquare2 className="size-10 opacity-30" />
          <p className="text-sm">Nenhuma tarefa neste projeto.</p>
        </div>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-input">
          {tasks.map((task) => (
            <li key={task.id} className="flex items-start gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p
                  className={cn(
                    "text-sm font-medium",
                    task.status === "feito" && "line-through text-muted-foreground",
                  )}
                >
                  {task.what}
                </p>
                <Link
                  to={`/meetings/${task.meeting_id}`}
                  className="text-xs text-muted-foreground underline-offset-2 hover:underline hover:text-foreground"
                >
                  {task.meeting_title}
                </Link>
              </div>
              <button
                className={cn(
                  "shrink-0 text-xs transition-colors",
                  task.status === "feito"
                    ? "text-green-600"
                    : "text-muted-foreground hover:text-foreground",
                )}
                onClick={() =>
                  toggleMutation.mutate({
                    id: task.id,
                    newStatus: task.status === "feito" ? "aberto" : "feito",
                  })
                }
              >
                {task.status === "feito" ? "✓ feita" : "marcar feita"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// ── Edit dialog ───────────────────────────────────────────────────────────────

function EditDialog({
  project,
  open,
  onOpenChange,
}: {
  project: Project
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [name, setName] = useState(project.name)
  const [description, setDescription] = useState(project.description ?? "")
  const [repoPath, setRepoPath] = useState(project.repo_path ?? "")
  const qc = useQueryClient()

  const updateMutation = useMutation({
    mutationFn: (body: {
      name: string
      description: string
      repo_path: string
    }) => api.updateProject(project.id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] })
      qc.invalidateQueries({ queryKey: ["project", project.id] })
      onOpenChange(false)
      toast.success("Projeto atualizado")
    },
    onError: (e: Error) => toast.error(e.message || "Erro ao atualizar projeto"),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Editar projeto</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="edit-name">Nome *</Label>
            <Input
              id="edit-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-desc">Descrição</Label>
            <textarea
              id="edit-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 resize-none"
              placeholder="Descrição opcional"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-repo">Repositório</Label>
            <Input
              id="edit-repo"
              value={repoPath}
              onChange={(e) => setRepoPath(e.target.value)}
              className="font-mono text-xs"
              placeholder="/caminho/para/repo ou URL"
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={updateMutation.isPending}
          >
            Cancelar
          </Button>
          <Button
            onClick={() =>
              updateMutation.mutate({
                name: name.trim(),
                description: description.trim(),
                repo_path: repoPath.trim(),
              })
            }
            disabled={!name.trim() || updateMutation.isPending}
          >
            {updateMutation.isPending ? "Salvando…" : "Salvar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── ProjectDetailPage ─────────────────────────────────────────────────────────

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [activeTab, setActiveTab] = useState<TabValue>("meetings")
  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const { data: project, isLoading, error } = useQuery<Project>({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    enabled: !Number.isNaN(projectId),
  })

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteProject(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] })
      qc.invalidateQueries({ queryKey: ["meetings"] })
      toast.success("Projeto excluído")
      void navigate("/projects")
    },
    onError: (e: Error) => toast.error(e.message || "Erro ao excluir projeto"),
  })

  if (isLoading) {
    return (
      <div className="mx-auto max-w-5xl space-y-4 px-4 py-8">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    )
  }

  if (error || !project) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-8">
        <p className="text-destructive">Projeto não encontrado.</p>
        <Button variant="outline" asChild className="mt-4">
          <Link to="/projects">← Voltar para projetos</Link>
        </Button>
      </div>
    )
  }

  const TABS: { value: TabValue; label: string }[] = [
    { value: "meetings", label: `Reuniões (${project.meeting_count})` },
    {
      value: "tasks",
      label: `Tarefas (${project.open_task_count + project.done_task_count})`,
    },
  ]

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      {/* Back */}
      <Link
        to="/projects"
        className="mb-6 inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        Projetos
      </Link>

      {/* Header */}
      <div className="mb-6 mt-4 flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <div className="flex items-center gap-2">
            <FolderKanban className="size-6 shrink-0 text-primary" />
            <h1 className="truncate text-2xl font-semibold tracking-tight">
              {project.name}
            </h1>
          </div>
          {project.description && (
            <p className="text-sm text-muted-foreground">{project.description}</p>
          )}
          {project.repo_path && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <GitBranch className="size-3.5 shrink-0" />
              <span className="font-mono">{project.repo_path}</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <Badge variant="secondary" className="gap-1">
              <Calendar className="size-3" />
              {project.meeting_count}{" "}
              {project.meeting_count === 1 ? "reunião" : "reuniões"}
            </Badge>
            {project.open_task_count > 0 && (
              <Badge
                variant="outline"
                className="gap-1 border-amber-500/50 text-amber-600 dark:text-amber-400"
              >
                <CheckSquare2 className="size-3" />
                {project.open_task_count} em aberto
              </Badge>
            )}
            {project.done_task_count > 0 && (
              <Badge
                variant="outline"
                className="gap-1 border-green-600/50 text-green-600 dark:text-green-400"
              >
                <CheckSquare2 className="size-3" />
                {project.done_task_count} feitas
              </Badge>
            )}
            {project.last_meeting_date && (
              <span className="text-xs text-muted-foreground">
                · Última: {project.last_meeting_date}
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
            <Pencil className="size-4" />
            Editar
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={() => setDeleteOpen(true)}
          >
            <Trash2 className="size-4" />
            Excluir
          </Button>
        </div>
      </div>

      {/* CTA to organise unassigned meetings */}
      <Card className="mb-6 border-dashed bg-muted/20">
        <CardContent className="flex items-center gap-3 py-3">
          <Users className="size-5 shrink-0 text-muted-foreground" />
          <p className="flex-1 text-sm text-muted-foreground">
            Reuniões sem projeto podem ser atribuídas em lote na lista de reuniões
            ou individualmente no detalhe de cada reunião.
          </p>
          <Button variant="outline" size="sm" asChild>
            <Link to="/?project_id=none">Ver sem projeto</Link>
          </Button>
        </CardContent>
      </Card>

      {/* Tabs */}
      <div className="mb-4 flex w-fit gap-1 rounded-lg border border-input bg-muted/40 p-1">
        {TABS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            onClick={() => setActiveTab(value)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === value
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {activeTab === "meetings" ? (
        <MeetingsList projectId={projectId} />
      ) : (
        <TasksList projectId={projectId} />
      )}

      {/* Edit dialog */}
      {editOpen && (
        <EditDialog project={project} open={editOpen} onOpenChange={setEditOpen} />
      )}

      {/* Delete dialog */}
      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Excluir projeto?</AlertDialogTitle>
            <AlertDialogDescription>
              O projeto <strong>{project.name}</strong> será excluído
              permanentemente. As reuniões associadas{" "}
              <em>não serão excluídas</em> — apenas ficarão sem projeto.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancelar
            </AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
            >
              Excluir
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
