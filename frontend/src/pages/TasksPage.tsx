import { useState, useEffect, useCallback, useMemo } from "react"
import { Link, useSearchParams } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  CheckSquare2,
  ListTodo,
  FolderKanban,
  Search,
  Download,
  Copy,
  Loader2,
  SquareCheck,
  Square,
  BrainCircuit,
  AlertTriangle,
} from "lucide-react"
import type { Task, Project, ContextExportRequest } from "@/lib/types"
import * as api from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { cn, copyToClipboard } from "@/lib/utils"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

// ── Constants ─────────────────────────────────────────────────────────────────

type FilterStatus = "aberto" | "feito" | "todos"
type FilterScope = "personal" | "delegated" | "all"
type FilterPriority = "todas" | "alta" | "media" | "baixa"

const STATUS_FILTERS: { value: FilterStatus; label: string }[] = [
  { value: "aberto", label: "Em aberto" },
  { value: "feito", label: "Concluídas" },
  { value: "todos", label: "Todas" },
]

const SCOPE_FILTERS: { value: FilterScope; label: string }[] = [
  { value: "personal", label: "Minhas" },
  { value: "delegated", label: "Delegadas" },
  { value: "all", label: "Todas" },
]

const PRIORITY_VARIANT = {
  alta: "destructive",
  media: "outline",
  baixa: "secondary",
} as const satisfies Record<Task["priority"], "destructive" | "outline" | "secondary">

const PRIORITY_LABEL: Record<FilterPriority, string> = {
  todas: "Todas prioridades",
  alta: "Alta",
  media: "Média",
  baixa: "Baixa",
}

const EMPTY_STATE: Record<FilterStatus, string> = {
  aberto: "Nenhuma tarefa em aberto.",
  feito: "Nenhuma tarefa concluída.",
  todos: "Nenhuma tarefa encontrada.",
}

// ── Inline toggle pill ────────────────────────────────────────────────────────

function FilterTabs<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="flex w-fit gap-0.5 rounded-lg border border-input bg-muted/40 p-0.5">
      {options.map(({ value: v, label }) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          className={cn(
            "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
            value === v
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

// ── Export dialog ─────────────────────────────────────────────────────────────

function ExportDialog({
  open,
  onOpenChange,
  taskIds,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  taskIds: number[]
}) {
  const [objective, setObjective] = useState("")
  const [format, setFormat] = useState<"markdown" | "json">("markdown")
  const [includeSummary, setIncludeSummary] = useState(true)
  const [includeFacts, setIncludeFacts] = useState(true)
  const [includeEvidence, setIncludeEvidence] = useState(true)
  const [includeTranscript, setIncludeTranscript] = useState(false)

  const [result, setResult] = useState<{ filename: string; content: string; task_count: number; meeting_count: number } | null>(null)
  const [loading, setLoading] = useState(false)

  // Reset state when dialog opens with new task selection
  useEffect(() => {
    if (open) {
      setResult(null)
      setObjective("")
    }
  }, [open])

  async function handleExport() {
    setLoading(true)
    setResult(null)
    try {
      const body: ContextExportRequest = {
        task_ids: taskIds,
        objective: objective || undefined,
        format,
        include_summary: includeSummary,
        include_facts: includeFacts,
        include_evidence: includeEvidence,
        include_transcript: includeTranscript,
      }
      const resp = await api.exportContext(body)
      setResult(resp)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Erro ao gerar contexto"
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  async function handleCopy() {
    if (!result) return
    const ok = await copyToClipboard(result.content)
    if (ok) toast.success("Copiado!")
    else toast.error("Não foi possível copiar")
  }

  function handleDownload() {
    if (!result) return
    const blob = new Blob([result.content], {
      type: format === "json" ? "application/json" : "text/markdown",
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = result.filename
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-full max-w-2xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle className="flex items-center gap-2">
            <BrainCircuit className="size-5 text-primary" />
            Contexto para LLM
          </DialogTitle>
        </DialogHeader>

        {!result ? (
          /* Config panel */
          <div className="flex flex-col gap-5 overflow-y-auto px-6 py-5">
            {/* Objective */}
            <div className="space-y-1.5">
              <Label htmlFor="export-objective">Objetivo (opcional)</Label>
              <textarea
                id="export-objective"
                value={objective}
                onChange={(e) => setObjective(e.target.value)}
                rows={3}
                placeholder="Ex: revisar pendências antes do sprint, delegar tarefas ao time..."
                className="flex w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              />
            </div>

            {/* Format */}
            <div className="space-y-1.5">
              <Label>Formato</Label>
              <FilterTabs
                options={[
                  { value: "markdown" as const, label: "Markdown" },
                  { value: "json" as const, label: "JSON" },
                ]}
                value={format}
                onChange={setFormat}
              />
            </div>

            {/* Includes */}
            <div className="space-y-3">
              <Label>Incluir</Label>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {(
                  [
                    { id: "summary", label: "Resumo", value: includeSummary, set: setIncludeSummary },
                    { id: "facts", label: "Fatos", value: includeFacts, set: setIncludeFacts },
                    { id: "evidence", label: "Evidências", value: includeEvidence, set: setIncludeEvidence },
                    { id: "transcript", label: "Transcrição", value: includeTranscript, set: setIncludeTranscript },
                  ] as const
                ).map(({ id, label, value: val, set }) => (
                  <label
                    key={id}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm transition-colors",
                      val
                        ? "border-primary/50 bg-primary/5 text-foreground"
                        : "border-input text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <Checkbox
                      checked={val}
                      onCheckedChange={(c) => set(!!c)}
                      id={`export-${id}`}
                    />
                    {label}
                  </label>
                ))}
              </div>
              {includeTranscript && (
                <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                  Transcrições podem ser longas e consumir muitos tokens de contexto.
                </div>
              )}
            </div>

            {/* Info */}
            <p className="text-xs text-muted-foreground">
              {taskIds.length} {taskIds.length === 1 ? "tarefa selecionada" : "tarefas selecionadas"}
            </p>

            {/* Actions */}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancelar
              </Button>
              <Button onClick={handleExport} disabled={loading}>
                {loading ? (
                  <>
                    <Loader2 className="mr-2 size-4 animate-spin" />
                    Gerando…
                  </>
                ) : (
                  "Gerar contexto"
                )}
              </Button>
            </div>
          </div>
        ) : (
          /* Preview panel */
          <div className="flex flex-1 flex-col overflow-hidden">
            {/* Stats */}
            <div className="flex items-center gap-3 border-b px-6 py-3 text-xs text-muted-foreground">
              <span>{result.task_count} tarefas · {result.meeting_count} reuniões</span>
              <span className="ml-auto font-mono">{result.filename}</span>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-auto">
              <pre className="px-6 py-4 text-xs leading-relaxed whitespace-pre-wrap break-words">
                {result.content}
              </pre>
            </div>

            {/* Footer */}
            <div className="flex items-center gap-2 border-t px-6 py-3">
              <Button variant="outline" size="sm" onClick={() => setResult(null)}>
                ← Voltar
              </Button>
              <div className="ml-auto flex gap-2">
                <Button variant="outline" size="sm" onClick={handleCopy}>
                  <Copy className="mr-1.5 size-3.5" />
                  Copiar
                </Button>
                <Button size="sm" onClick={handleDownload}>
                  <Download className="mr-1.5 size-3.5" />
                  Baixar
                </Button>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

// ── Task item ─────────────────────────────────────────────────────────────────

function TaskItem({
  task,
  selected,
  onSelect,
  onToggle,
}: {
  task: Task
  selected: boolean
  onSelect: (id: number, checked: boolean) => void
  onToggle: (id: number, newStatus: Task["status"]) => void
}) {
  const isDone = task.status === "feito"

  return (
    <li className="flex items-start gap-3 px-4 py-3">
      {/* Selection checkbox */}
      <div className="mt-0.5 shrink-0">
        <Checkbox
          checked={selected}
          onCheckedChange={(c) => onSelect(task.id, !!c)}
          aria-label={`Selecionar: ${task.what}`}
          className="data-[state=checked]:border-primary data-[state=checked]:bg-primary/10"
        />
      </div>

      {/* Completion checkbox */}
      <div className="mt-0.5 shrink-0">
        <Checkbox
          checked={isDone}
          onCheckedChange={(c) =>
            onToggle(task.id, c ? "feito" : "aberto")
          }
          aria-label={isDone ? "Marcar como aberta" : "Marcar como concluída"}
        />
      </div>

      {/* Content */}
      <div className="min-w-0 flex-1 space-y-1">
        {/* Primary text */}
        <p
          className={cn(
            "text-sm font-medium leading-snug",
            isDone && "line-through text-muted-foreground",
          )}
        >
          {task.what}
        </p>

        {/* Details */}
        {task.details && (
          <p className="text-xs text-muted-foreground line-clamp-2">{task.details}</p>
        )}

        {/* Meta row */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {/* Where */}
          {task.where && (
            <span className="text-xs text-muted-foreground">
              <span className="opacity-60">onde:</span> {task.where}
            </span>
          )}

          {/* Requested by */}
          {task.requested_by && (
            <span className="text-xs text-muted-foreground">
              <span className="opacity-60">de:</span> {task.requested_by}
            </span>
          )}

          {/* Assigned to */}
          {task.assigned_to && task.assigned_to.length > 0 && (
            <span className="text-xs text-muted-foreground">
              <span className="opacity-60">para:</span> {task.assigned_to.join(", ")}
            </span>
          )}

          {/* Due */}
          {task.due && (
            <span className="text-xs text-muted-foreground">
              <span className="opacity-60">prazo:</span> {task.due}
            </span>
          )}

          {/* Review flag */}
          {task.review_status === "needs_review" && (
            <Badge variant="outline" className="border-amber-500/50 text-amber-600 dark:text-amber-400 text-[10px] px-1.5 py-0">
              revisar
            </Badge>
          )}

          {/* Meeting link */}
          <Link
            to={`/meetings/${task.meeting_id}`}
            className="text-xs text-muted-foreground underline-offset-2 transition-colors hover:underline hover:text-foreground"
          >
            {task.meeting_title}
          </Link>

          {/* Project badge */}
          {task.project_name && (
            <Link to={`/projects/${task.project_id}`}>
              <Badge
                variant="secondary"
                className="gap-1 text-[10px] px-1.5 py-0 transition-colors hover:bg-secondary/80"
              >
                <FolderKanban className="size-2.5 shrink-0" />
                {task.project_name}
              </Badge>
            </Link>
          )}

          {/* Date */}
          <span className="text-[10px] text-muted-foreground/60">{task.date}</span>
        </div>
      </div>

      {/* Priority badge */}
      <Badge
        variant={PRIORITY_VARIANT[task.priority]}
        className={cn(
          "shrink-0 self-start text-[10px]",
          task.priority === "media" &&
            "border-amber-500 text-amber-600 dark:border-amber-400 dark:text-amber-400",
        )}
      >
        {task.priority}
      </Badge>
    </li>
  )
}

// ── TasksPage ─────────────────────────────────────────────────────────────────

export default function TasksPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()

  // URL-persisted: project_id
  const projectIdParam = searchParams.get("project_id")
  const apiProjectId: "none" | number | undefined =
    projectIdParam === null
      ? undefined
      : projectIdParam === "none"
        ? "none"
        : Number(projectIdParam)

  // Local state filters
  const [status, setStatus] = useState<FilterStatus>("aberto")
  const [scope, setScope] = useState<FilterScope>("personal")
  const [priority, setPriority] = useState<FilterPriority>("todas")
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")

  // Debounce search
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 250)
    return () => clearTimeout(t)
  }, [search])

  // Selection state
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [exportOpen, setExportOpen] = useState(false)

  // ── Queries ────────────────────────────────────────────────────────────────

  const { data: projects = [] } = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: api.getProjects,
    staleTime: 30_000,
  })

  const { data: tasks = [], isLoading } = useQuery<Task[]>({
    queryKey: ["tasks", status, projectIdParam ?? "all", scope],
    queryFn: () => api.getTasks(status, apiProjectId, scope),
  })

  // ── Mutations ──────────────────────────────────────────────────────────────

  const toggleMutation = useMutation({
    mutationFn: ({ id, newStatus }: { id: number; newStatus: Task["status"] }) =>
      api.updateActionItem(id, { status: newStatus }),
    onMutate: async ({ id, newStatus }) => {
      const key = ["tasks", status, projectIdParam ?? "all", scope]
      await queryClient.cancelQueries({ queryKey: key })
      const prev = queryClient.getQueryData<Task[]>(key)
      if (prev) {
        queryClient.setQueryData<Task[]>(key, (old = []) =>
          old.map((t) => (t.id === id ? { ...t, status: newStatus } : t)),
        )
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) {
        queryClient.setQueryData(
          ["tasks", status, projectIdParam ?? "all", scope],
          ctx.prev,
        )
      }
      toast.error("Erro ao atualizar tarefa")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  // ── Filtered tasks ─────────────────────────────────────────────────────────

  const filteredTasks = useMemo(() => {
    let list = tasks

    // Priority filter (local)
    if (priority !== "todas") {
      list = list.filter((t) => t.priority === priority)
    }

    // Search filter (local, across what/where/details/meeting title)
    if (debouncedSearch) {
      const q = debouncedSearch.toLowerCase()
      list = list.filter(
        (t) =>
          t.what.toLowerCase().includes(q) ||
          (t.where ?? "").toLowerCase().includes(q) ||
          (t.details ?? "").toLowerCase().includes(q) ||
          t.meeting_title.toLowerCase().includes(q),
      )
    }

    return list
  }, [tasks, priority, debouncedSearch])

  // ── Selection helpers ──────────────────────────────────────────────────────

  const handleSelect = useCallback((id: number, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])

  const handleToggle = useCallback(
    (id: number, newStatus: Task["status"]) => {
      toggleMutation.mutate({ id, newStatus })
    },
    [toggleMutation],
  )

  function selectVisible() {
    setSelected(new Set(filteredTasks.map((t) => t.id)))
  }

  function clearSelection() {
    setSelected(new Set())
  }

  // Keep selection in sync when tasks change (remove stale ids)
  useEffect(() => {
    const validIds = new Set(tasks.map((t) => t.id))
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => validIds.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [tasks])

  // ── Project filter helpers ─────────────────────────────────────────────────

  function setProjectFilter(value: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value === "__all__") {
        next.delete("project_id")
      } else {
        next.set("project_id", value)
      }
      return next
    })
  }

  const projectSelectValue = projectIdParam ?? "__all__"

  // ── Render ─────────────────────────────────────────────────────────────────

  const selectedIds = [...selected]
  const hasSelection = selectedIds.length > 0

  return (
    <div className="mx-auto max-w-4xl">
      {/* Header */}
      <div className="mb-5 flex items-center gap-3">
        <ListTodo className="size-6 text-primary" />
        <h1 className="text-2xl font-semibold tracking-tight">Task Studio</h1>
        {!isLoading && (
          <span className="ml-1 text-sm text-muted-foreground">
            {filteredTasks.length}{" "}
            {filteredTasks.length === tasks.length ? "" : `/ ${tasks.length} `}
            {filteredTasks.length === 1 ? "tarefa" : "tarefas"}
          </span>
        )}
      </div>

      {/* Filters row 1: status + scope */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <FilterTabs options={STATUS_FILTERS} value={status} onChange={setStatus} />
        <FilterTabs options={SCOPE_FILTERS} value={scope} onChange={setScope} />
      </div>

      {/* Filters row 2: project + priority + search */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {/* Project select */}
        <Select value={projectSelectValue} onValueChange={setProjectFilter}>
          <SelectTrigger className="h-9 w-48 text-sm">
            <SelectValue>
              {projectSelectValue === "__all__"
                ? "Todos projetos"
                : projectSelectValue === "none"
                  ? "Sem projeto"
                  : (projects.find((p) => String(p.id) === projectSelectValue)?.name ?? projectSelectValue)}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Todos projetos</SelectItem>
            <SelectItem value="none">Sem projeto</SelectItem>
            {projects.map((p) => (
              <SelectItem key={p.id} value={String(p.id)}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Priority select */}
        <Select value={priority} onValueChange={(v) => setPriority(v as FilterPriority)}>
          <SelectTrigger className="h-9 w-44 text-sm">
            <SelectValue>{PRIORITY_LABEL[priority]}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {(["todas", "alta", "media", "baixa"] as FilterPriority[]).map((p) => (
              <SelectItem key={p} value={p}>
                {PRIORITY_LABEL[p]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Search */}
        <div className="relative flex-1 min-w-48">
          <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            placeholder="Buscar tarefa, reunião…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-9 pl-8 text-sm"
          />
        </div>
      </div>

      {/* Selection action bar */}
      {filteredTasks.length > 0 && (
        <div
          className={cn(
            "mb-3 flex items-center gap-3 rounded-lg border px-4 py-2 transition-all",
            hasSelection
              ? "border-primary/30 bg-primary/5"
              : "border-transparent bg-transparent",
          )}
        >
          {/* Select-all visible toggle */}
          <button
            type="button"
            onClick={hasSelection ? clearSelection : selectVisible}
            className="flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
            title={hasSelection ? "Desmarcar todos" : "Selecionar visíveis"}
          >
            {hasSelection ? (
              <SquareCheck className="size-4 text-primary" />
            ) : (
              <Square className="size-4" />
            )}
            {hasSelection ? `${selectedIds.length} selecionadas` : "Selecionar visíveis"}
          </button>

          {hasSelection && (
            <>
              <div className="h-4 w-px bg-border" />
              <Button
                size="sm"
                onClick={() => setExportOpen(true)}
                className="gap-1.5"
              >
                <BrainCircuit className="size-3.5" />
                Preparar contexto para LLM
              </Button>
            </>
          )}
        </div>
      )}

      {/* Task list */}
      {isLoading ? (
        <div className="space-y-2">
          {[1, 2, 3].map((n) => (
            <div key={n} className="h-16 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      ) : filteredTasks.length === 0 ? (
        <div className="flex flex-col items-center gap-3 py-16 text-muted-foreground">
          <CheckSquare2 className="size-10 opacity-30" />
          <p className="text-sm">
            {search || priority !== "todas"
              ? "Nenhuma tarefa com esse filtro."
              : EMPTY_STATE[status]}
          </p>
          {(search || priority !== "todas") && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setSearch("")
                setPriority("todas")
              }}
            >
              Limpar filtros
            </Button>
          )}
        </div>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-input">
          {filteredTasks.map((task) => (
            <TaskItem
              key={task.id}
              task={task}
              selected={selected.has(task.id)}
              onSelect={handleSelect}
              onToggle={handleToggle}
            />
          ))}
        </ul>
      )}

      {/* Export dialog */}
      <ExportDialog
        open={exportOpen}
        onOpenChange={setExportOpen}
        taskIds={selectedIds}
      />
    </div>
  )
}
