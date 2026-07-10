import { useState } from "react"
import { Link } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { CheckSquare2, ListTodo } from "lucide-react"
import type { Task } from "@/lib/types"
import * as api from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { cn } from "@/lib/utils"

type FilterStatus = "aberto" | "feito" | "todos"

const PRIORITY_VARIANT = {
  alta: "destructive",
  media: "outline",
  baixa: "secondary",
} as const satisfies Record<Task["priority"], "destructive" | "outline" | "secondary">

const FILTERS: { value: FilterStatus; label: string }[] = [
  { value: "aberto", label: "Em aberto" },
  { value: "feito", label: "Concluídas" },
  { value: "todos", label: "Todas" },
]

const EMPTY_STATE: Record<FilterStatus, string> = {
  aberto: "Nenhuma tarefa em aberto.",
  feito: "Nenhuma tarefa concluída.",
  todos: "Nenhuma tarefa encontrada.",
}

export default function TasksPage() {
  const [status, setStatus] = useState<FilterStatus>("aberto")
  const queryClient = useQueryClient()

  const { data: tasks = [], isLoading } = useQuery<Task[]>({
    queryKey: ["tasks", status],
    queryFn: () => api.getTasks(status),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, newStatus }: { id: number; newStatus: Task["status"] }) =>
      api.updateActionItem(id, { status: newStatus }),
    onMutate: async ({ id, newStatus }) => {
      await queryClient.cancelQueries({ queryKey: ["tasks", status] })
      const prev = queryClient.getQueryData<Task[]>(["tasks", status])
      if (prev) {
        queryClient.setQueryData<Task[]>(["tasks", status], (old = []) =>
          old.map((t) => (t.id === id ? { ...t, status: newStatus } : t)),
        )
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(["tasks", status], ctx.prev)
      toast.error("Erro ao atualizar tarefa")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  return (
    <div className="mx-auto max-w-3xl">
      {/* Header */}
      <div className="mb-6 flex items-center gap-3">
        <ListTodo className="size-6 text-primary" />
        <h1 className="text-2xl font-semibold tracking-tight">Tarefas</h1>
      </div>

      {/* Filter tabs */}
      <div className="mb-4 flex gap-1 rounded-lg border border-input bg-muted/40 p-1 w-fit">
        {FILTERS.map(({ value, label }) => (
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

      {/* List */}
      {isLoading ? (
        <div className="space-y-2">
          {[1, 2, 3].map((n) => (
            <div key={n} className="h-14 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      ) : tasks.length === 0 ? (
        <div className="flex flex-col items-center gap-3 py-16 text-muted-foreground">
          <CheckSquare2 className="size-10 opacity-30" />
          <p className="text-sm">{EMPTY_STATE[status]}</p>
        </div>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-input">
          {tasks.map((task) => (
            <li key={task.id} className="flex items-start gap-3 px-4 py-3">
              {/* Checkbox */}
              <div className="mt-0.5">
                <Checkbox
                  checked={task.status === "feito"}
                  onCheckedChange={(checked) =>
                    toggleMutation.mutate({
                      id: task.id,
                      newStatus: checked ? "feito" : "aberto",
                    })
                  }
                />
              </div>

              {/* Content */}
              <div className="min-w-0 flex-1 space-y-0.5">
                <p
                  className={cn(
                    "text-sm font-medium leading-snug",
                    task.status === "feito" && "line-through text-muted-foreground",
                  )}
                >
                  {task.what}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  {/* Meeting link */}
                  <Link
                    to={`/meetings/${task.meeting_id}`}
                    className="text-xs text-muted-foreground underline-offset-2 hover:underline hover:text-foreground transition-colors"
                  >
                    {task.meeting_title}
                  </Link>
                  {task.due && (
                    <span className="text-xs text-muted-foreground">· {task.due}</span>
                  )}
                </div>
              </div>

              {/* Priority badge */}
              <Badge
                variant={PRIORITY_VARIANT[task.priority]}
                className={cn(
                  "shrink-0 self-start",
                  task.priority === "media" &&
                    "border-amber-500 text-amber-600 dark:border-amber-400 dark:text-amber-400",
                )}
              >
                {task.priority}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
