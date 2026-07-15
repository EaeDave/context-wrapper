import { useState } from "react"
import { ClipboardList, Copy, Pencil, Plus, Trash2, Clock } from "lucide-react"
import { useQueryClient, useMutation } from "@tanstack/react-query"
import { toast } from "sonner"
import type { ActionItem, MeetingDetail } from "@/lib/types"
import * as api from "@/lib/api"
import { copyToClipboard } from "@/lib/utils"
import { formatTs } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

const PRIORITY_VARIANT = {
  alta: "destructive",
  media: "outline",
  baixa: "secondary",
} as const satisfies Record<ActionItem["priority"], "destructive" | "outline" | "secondary">

// ── Form state (editable fields only; traceable metadata is read-only) ─────────
interface ItemForm {
  what: string
  where: string
  details: string
  requested_by: string
  assigned_to: string   // comma-separated
  priority: ActionItem["priority"]
  due: string
}

function emptyForm(): ItemForm {
  return { what: "", where: "", details: "", requested_by: "", assigned_to: "", priority: "media", due: "" }
}

function itemToForm(item: ActionItem): ItemForm {
  return {
    what: item.what,
    where: item.where ?? "",
    details: item.details ?? "",
    requested_by: item.requested_by ?? "",
    assigned_to: (item.assigned_to ?? []).join(", "),
    priority: item.priority,
    due: item.due ?? "",
  }
}

function parseAssignedTo(raw: string): string[] | null {
  const arr = raw.split(",").map((s) => s.trim()).filter(Boolean)
  return arr.length > 0 ? arr : null
}

interface ActionItemsProps {
  meetingId: number
  items: ActionItem[]
  onSeek?: (seconds: number) => void
}

export function ActionItems({ meetingId, items, onSeek }: ActionItemsProps) {
  const queryClient = useQueryClient()

  const [editItem, setEditItem] = useState<ActionItem | null>(null)
  const [editForm, setEditForm] = useState<ItemForm>(emptyForm())
  const [deleteItemId, setDeleteItemId] = useState<number | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [addForm, setAddForm] = useState<ItemForm>(emptyForm())

  // ── Toggle status (optimistic) ─────────────────────────────────────────────
  const toggleMutation = useMutation({
    mutationFn: ({ id, status }: { id: number; status: ActionItem["status"] }) =>
      api.updateActionItem(id, { status }),
    onMutate: async ({ id, status }) => {
      await queryClient.cancelQueries({ queryKey: ["meeting", meetingId] })
      const prev = queryClient.getQueryData<MeetingDetail>(["meeting", meetingId])
      if (prev) {
        queryClient.setQueryData<MeetingDetail>(["meeting", meetingId], {
          ...prev,
          action_items: prev.action_items.map((it) =>
            it.id === id ? { ...it, status } : it,
          ),
        })
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(["meeting", meetingId], ctx.prev)
      toast.error("Erro ao atualizar status")
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] }),
  })

  // ── Update item ────────────────────────────────────────────────────────────
  const updateMutation = useMutation({
    mutationFn: ({ id, form }: { id: number; form: ItemForm }) =>
      api.updateActionItem(id, {
        what: form.what.trim(),
        where: form.where.trim() || null,
        details: form.details.trim() || null,
        requested_by: form.requested_by.trim() || null,
        assigned_to: parseAssignedTo(form.assigned_to),
        priority: form.priority,
        due: form.due.trim() || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      setEditItem(null)
      toast.success("Item atualizado")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Delete item ────────────────────────────────────────────────────────────
  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteActionItem(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      setDeleteItemId(null)
      toast.success("Item excluído")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Add item ───────────────────────────────────────────────────────────────
  const addMutation = useMutation({
    mutationFn: (form: ItemForm) =>
      api.addActionItem(meetingId, {
        what: form.what.trim(),
        where: form.where.trim() || null,
        details: form.details.trim() || null,
        requested_by: form.requested_by.trim() || null,
        assigned_to: parseAssignedTo(form.assigned_to),
        priority: form.priority,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      setAddOpen(false)
      setAddForm(emptyForm())
      toast.success("Tarefa adicionada")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function openEdit(item: ActionItem) {
    setEditItem(item)
    setEditForm(itemToForm(item))
  }

  async function handleCopy() {
    if (items.length === 0) return
    const lines = items.map((it) => {
      const check = it.status === "feito" ? "[x]" : "[ ]"
      let line = `- ${check} ${it.what}`
      if (it.where) line += ` (onde: ${it.where})`
      if (it.details) line += ` — ${it.details}`
      return line
    })
    const ok = await copyToClipboard(lines.join("\n"))
    if (ok) toast.success("Action items copiados")
    else toast.error("Não foi possível copiar")
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <ClipboardList className="size-4" />
            Action items
          </CardTitle>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="size-7 text-muted-foreground hover:text-foreground"
              onClick={handleCopy}
              disabled={items.length === 0}
              title="Copiar action items"
            >
              <Copy className="size-3.5" />
              <span className="sr-only">Copiar action items</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 text-xs"
              onClick={() => {
                setAddForm(emptyForm())
                setAddOpen(true)
              }}
            >
              <Plus className="size-3.5" />
              Adicionar tarefa
            </Button>
          </div>
        </div>
      </CardHeader>

      {items.length > 0 && (
        <CardContent className="px-0 pb-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10 pl-6" />
                <TableHead>O quê</TableHead>
                <TableHead>Responsáveis</TableHead>
                <TableHead>Onde</TableHead>
                <TableHead>Prioridade</TableHead>
                <TableHead>Prazo</TableHead>
                <TableHead className="w-24 pr-4" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item) => (
                <TableRow key={item.id} className={cn(item.status === "feito" && "opacity-60")}>
                  <TableCell className="pl-6">
                    <Checkbox
                      checked={item.status === "feito"}
                      onCheckedChange={(checked) =>
                        toggleMutation.mutate({
                          id: item.id,
                          status: checked ? "feito" : "aberto",
                        })
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <span
                      className={cn(
                        "font-medium",
                        item.status === "feito" && "line-through text-muted-foreground",
                      )}
                    >
                      {item.what}
                    </span>
                    {item.details && (
                      <p className="mt-0.5 text-xs text-muted-foreground">{item.details}</p>
                    )}
                    {/* Traceable metadata row */}
                    <div className="mt-1 flex flex-wrap items-center gap-1">
                      <Badge
                        variant={item.review_status === "confirmed" ? "secondary" : "outline"}
                        className={cn(
                          "h-4 px-1 text-[10px]",
                          item.review_status === "confirmed"
                            ? "border-green-500 text-green-700 dark:text-green-400"
                            : "border-amber-500 text-amber-600 dark:text-amber-400",
                        )}
                      >
                        {item.review_status === "confirmed" ? "confirmado" : "revisar"}
                      </Badge>
                      <Badge
                        variant="outline"
                        className="h-4 px-1 text-[10px] text-muted-foreground"
                      >
                        {item.explicitness === "explicit" ? "explícito" : "inferido"}
                      </Badge>
                      {item.source_start != null && (
                        <button
                          type="button"
                          onClick={() => onSeek?.(item.source_start!)}
                          className="inline-flex items-center gap-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground transition-colors"
                          title={item.evidence_quote ?? undefined}
                        >
                          <Clock className="size-2.5" />
                          {formatTs(item.source_start)}
                        </button>
                      )}
                    </div>
                    {item.evidence_quote && (
                      <p className="mt-1 text-[11px] italic text-muted-foreground line-clamp-2 border-l-2 border-muted pl-2">
                        {item.evidence_quote}
                      </p>
                    )}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {item.assigned_to && item.assigned_to.length > 0
                      ? item.assigned_to.join(", ")
                      : (item.requested_by ?? "—")}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {item.where ?? "—"}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={PRIORITY_VARIANT[item.priority]}
                      className={cn(
                        item.priority === "media" &&
                          "border-amber-500 text-amber-600 dark:border-amber-400 dark:text-amber-400",
                      )}
                    >
                      {item.priority}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {item.due ?? "—"}
                  </TableCell>
                  <TableCell className="pr-4">
                    <div className="flex gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        onClick={() => openEdit(item)}
                      >
                        <Pencil className="size-3.5" />
                        <span className="sr-only">Editar</span>
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 text-destructive hover:text-destructive"
                        onClick={() => setDeleteItemId(item.id)}
                      >
                        <Trash2 className="size-3.5" />
                        <span className="sr-only">Excluir</span>
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      )}

      {/* ── Edit Dialog ─────────────────────────────────────────────────────── */}
      <Dialog open={!!editItem} onOpenChange={(open) => !open && setEditItem(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Editar action item</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="edit-what">O quê *</Label>
              <Input
                id="edit-what"
                value={editForm.what}
                onChange={(e) => setEditForm((f) => ({ ...f, what: e.target.value }))}
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="edit-assigned-to">Responsáveis</Label>
              <Input
                id="edit-assigned-to"
                placeholder="Nome 1, Nome 2"
                value={editForm.assigned_to}
                onChange={(e) => setEditForm((f) => ({ ...f, assigned_to: e.target.value }))}
              />
              <p className="text-[11px] text-muted-foreground">Separe múltiplos nomes por vírgula</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="edit-where">Onde</Label>
              <Input
                id="edit-where"
                value={editForm.where}
                onChange={(e) => setEditForm((f) => ({ ...f, where: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="edit-details">Detalhes</Label>
              <Input
                id="edit-details"
                value={editForm.details}
                onChange={(e) => setEditForm((f) => ({ ...f, details: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="edit-requested-by">Pedido por</Label>
              <Input
                id="edit-requested-by"
                value={editForm.requested_by}
                onChange={(e) => setEditForm((f) => ({ ...f, requested_by: e.target.value }))}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label>Prioridade</Label>
                <Select
                  value={editForm.priority}
                  onValueChange={(v) =>
                    setEditForm((f) => ({ ...f, priority: v as ActionItem["priority"] }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="alta">Alta</SelectItem>
                    <SelectItem value="media">Média</SelectItem>
                    <SelectItem value="baixa">Baixa</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="edit-due">Prazo</Label>
                <Input
                  id="edit-due"
                  type="date"
                  value={editForm.due}
                  onChange={(e) => setEditForm((f) => ({ ...f, due: e.target.value }))}
                />
              </div>
            </div>
            {/* Read-only traceable metadata — shown when present */}
            {editItem && (editItem.evidence_quote || editItem.source_start != null) && (
              <div className="rounded-md border border-dashed p-3 space-y-1">
                <p className="text-xs font-medium text-muted-foreground">Evidência (só leitura)</p>
                {editItem.source_start != null && (
                  <p className="text-xs text-muted-foreground">
                    Origem: {formatTs(editItem.source_start)}
                    {editItem.source_end != null && ` — ${formatTs(editItem.source_end)}`}
                  </p>
                )}
                {editItem.evidence_quote && (
                  <p className="text-xs italic text-muted-foreground border-l-2 border-muted pl-2">
                    {editItem.evidence_quote}
                  </p>
                )}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditItem(null)}
              disabled={updateMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={() =>
                editItem && updateMutation.mutate({ id: editItem.id, form: editForm })
              }
              disabled={!editForm.what.trim() || updateMutation.isPending}
            >
              Salvar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete AlertDialog ───────────────────────────────────────────────── */}
      <AlertDialog
        open={deleteItemId !== null}
        onOpenChange={(open) => !open && setDeleteItemId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Excluir item?</AlertDialogTitle>
            <AlertDialogDescription>
              Esta ação não pode ser desfeita.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => deleteItemId !== null && deleteMutation.mutate(deleteItemId)}
              disabled={deleteMutation.isPending}
            >
              Excluir
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ── Add Dialog ──────────────────────────────────────────────────────── */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Adicionar tarefa</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="add-what">O quê *</Label>
              <Input
                id="add-what"
                value={addForm.what}
                onChange={(e) => setAddForm((f) => ({ ...f, what: e.target.value }))}
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="add-assigned-to">Responsáveis</Label>
              <Input
                id="add-assigned-to"
                placeholder="Nome 1, Nome 2"
                value={addForm.assigned_to}
                onChange={(e) => setAddForm((f) => ({ ...f, assigned_to: e.target.value }))}
              />
              <p className="text-[11px] text-muted-foreground">Separe múltiplos nomes por vírgula</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="add-where">Onde</Label>
              <Input
                id="add-where"
                value={addForm.where}
                onChange={(e) => setAddForm((f) => ({ ...f, where: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="add-details">Detalhes</Label>
              <Input
                id="add-details"
                value={addForm.details}
                onChange={(e) => setAddForm((f) => ({ ...f, details: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="add-requested-by">Pedido por</Label>
              <Input
                id="add-requested-by"
                value={addForm.requested_by}
                onChange={(e) => setAddForm((f) => ({ ...f, requested_by: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Prioridade</Label>
              <Select
                value={addForm.priority}
                onValueChange={(v) =>
                  setAddForm((f) => ({ ...f, priority: v as ActionItem["priority"] }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="alta">Alta</SelectItem>
                  <SelectItem value="media">Média</SelectItem>
                  <SelectItem value="baixa">Baixa</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setAddOpen(false)}
              disabled={addMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={() => addMutation.mutate(addForm)}
              disabled={!addForm.what.trim() || addMutation.isPending}
            >
              Adicionar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
