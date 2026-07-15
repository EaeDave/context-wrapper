import { useState } from "react"
import { Link } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Plus,
  FolderKanban,
  FolderX,
  Pencil,
  Trash2,
  Calendar,
  CheckSquare2,
  GitBranch,
  ArrowRight,
} from "lucide-react"

import * as api from "@/lib/api"
import type { Project } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
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

// ── Form dialog ───────────────────────────────────────────────────────────────

interface ProjectFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  initial?: Pick<Project, "name" | "description" | "repo_path">
  onSave: (data: { name: string; description: string; repo_path: string }) => void
  isPending: boolean
  title: string
}

function ProjectFormDialog({
  open,
  onOpenChange,
  initial,
  onSave,
  isPending,
  title,
}: ProjectFormProps) {
  const [name, setName] = useState(initial?.name ?? "")
  const [description, setDescription] = useState(initial?.description ?? "")
  const [repoPath, setRepoPath] = useState(initial?.repo_path ?? "")

  function handleOpenChange(v: boolean) {
    if (v) {
      setName(initial?.name ?? "")
      setDescription(initial?.description ?? "")
      setRepoPath(initial?.repo_path ?? "")
    }
    onOpenChange(v)
  }

  function handleSave() {
    if (!name.trim()) return
    onSave({
      name: name.trim(),
      description: description.trim(),
      repo_path: repoPath.trim(),
    })
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="proj-name">Nome *</Label>
            <Input
              id="proj-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && name.trim()) handleSave()
              }}
              placeholder="Nome do projeto"
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="proj-desc">Descrição</Label>
            <textarea
              id="proj-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Descrição opcional"
              rows={3}
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 resize-none"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="proj-repo">Repositório</Label>
            <Input
              id="proj-repo"
              value={repoPath}
              onChange={(e) => setRepoPath(e.target.value)}
              placeholder="/caminho/para/repo ou URL"
              className="font-mono text-xs"
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancelar
          </Button>
          <Button onClick={handleSave} disabled={!name.trim() || isPending}>
            {isPending ? "Salvando…" : "Salvar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── ProjectsPage ──────────────────────────────────────────────────────────────

export default function ProjectsPage() {
  const qc = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [editProject, setEditProject] = useState<Project | null>(null)
  const [deleteProject, setDeleteProject] = useState<Project | null>(null)

  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: api.getProjects,
  })

  const createMutation = useMutation({
    mutationFn: api.createProject,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] })
      setCreateOpen(false)
      toast.success("Projeto criado")
    },
    onError: (e: Error) => toast.error(e.message || "Erro ao criar projeto"),
  })

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: number
      name: string
      description: string
      repo_path: string
    }) => api.updateProject(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] })
      setEditProject(null)
      toast.success("Projeto atualizado")
    },
    onError: (e: Error) => toast.error(e.message || "Erro ao atualizar projeto"),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteProject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] })
      qc.invalidateQueries({ queryKey: ["meetings"] })
      setDeleteProject(null)
      toast.success("Projeto excluído")
    },
    onError: (e: Error) => toast.error(e.message || "Erro ao excluir projeto"),
  })

  const projects = projectsQuery.data ?? []

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      {/* Header */}
      <div className="mb-6 flex items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Projetos</h1>
        <Button className="ml-auto" onClick={() => setCreateOpen(true)}>
          <Plus className="size-4" />
          Criar projeto
        </Button>
      </div>

      {/* Grid */}
      {projectsQuery.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((n) => (
            <Skeleton key={n} className="h-48 w-full rounded-xl" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {/* Sem projeto card */}
          <Card className="flex flex-col border-dashed">
            <CardHeader className="pb-2">
              <div className="flex items-center gap-2">
                <FolderX className="size-5 text-muted-foreground" />
                <span className="font-medium">Sem projeto</span>
              </div>
            </CardHeader>
            <CardContent className="flex flex-1 flex-col gap-3">
              <p className="text-sm text-muted-foreground">
                Reuniões não atribuídas a nenhum projeto
              </p>
              <div className="mt-auto">
                <Button variant="outline" size="sm" asChild className="gap-1.5">
                  <Link to="/?project_id=none">
                    Ver reuniões
                    <ArrowRight className="size-3.5" />
                  </Link>
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* Project cards */}
          {projects.map((project) => (
            <Card key={project.id} className="flex flex-col">
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <FolderKanban className="size-5 shrink-0 text-primary" />
                    <span className="truncate font-medium">{project.name}</span>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-7 text-muted-foreground hover:text-foreground"
                      onClick={() => setEditProject(project)}
                      title="Editar"
                    >
                      <Pencil className="size-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-7 text-muted-foreground hover:text-destructive"
                      onClick={() => setDeleteProject(project)}
                      title="Excluir"
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="flex flex-1 flex-col gap-3">
                {project.description && (
                  <p className="line-clamp-2 text-sm text-muted-foreground">
                    {project.description}
                  </p>
                )}
                {project.repo_path && (
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <GitBranch className="size-3.5 shrink-0" />
                    <span className="truncate font-mono">{project.repo_path}</span>
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
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
                </div>
                {project.last_meeting_date && (
                  <p className="text-xs text-muted-foreground">
                    Última: {project.last_meeting_date}
                  </p>
                )}
                <div className="mt-auto pt-1">
                  <Button variant="outline" size="sm" asChild className="gap-1.5">
                    <Link to={`/projects/${project.id}`}>
                      Ver detalhes
                      <ArrowRight className="size-3.5" />
                    </Link>
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}

          {/* Empty projects */}
          {projects.length === 0 && (
            <div className="col-span-full flex flex-col items-center gap-4 py-16 text-muted-foreground">
              <FolderKanban className="size-12 opacity-30" />
              <p className="text-sm">Nenhum projeto criado ainda.</p>
              <Button onClick={() => setCreateOpen(true)}>
                <Plus className="size-4" />
                Criar primeiro projeto
              </Button>
            </div>
          )}
        </div>
      )}

      {/* Create dialog */}
      <ProjectFormDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        title="Criar projeto"
        isPending={createMutation.isPending}
        onSave={(data) => createMutation.mutate(data)}
      />

      {/* Edit dialog */}
      {editProject && (
        <ProjectFormDialog
          open
          onOpenChange={(v) => {
            if (!v) setEditProject(null)
          }}
          initial={editProject}
          title="Editar projeto"
          isPending={updateMutation.isPending}
          onSave={(data) =>
            updateMutation.mutate({ id: editProject.id, ...data })
          }
        />
      )}

      {/* Delete confirm */}
      <AlertDialog
        open={!!deleteProject}
        onOpenChange={(v) => {
          if (!v) setDeleteProject(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Excluir projeto?</AlertDialogTitle>
            <AlertDialogDescription>
              O projeto <strong>{deleteProject?.name}</strong> será excluído. As
              reuniões associadas <em>não serão excluídas</em> — apenas ficarão
              sem projeto atribuído.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancelar
            </AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() =>
                deleteProject && deleteMutation.mutate(deleteProject.id)
              }
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
