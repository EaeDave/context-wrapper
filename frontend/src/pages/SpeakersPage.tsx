import { Fragment, useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Trash2, Mic, Pencil, ChevronDown, ChevronUp } from "lucide-react"
import { Link } from "react-router"

import * as api from "@/lib/api"
import type { VoiceUsage } from "@/lib/types"
import { Button } from "@/components/ui/button"
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
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"

function UsageList({ name }: { name: string }) {
  const { data, isLoading } = useQuery<VoiceUsage[]>({
    queryKey: ["voice-usage", name],
    queryFn: () => api.getVoiceUsage(name),
  })

  if (isLoading) {
    return <Skeleton className="h-4 w-48" />
  }
  if (!data?.length) {
    return <p className="text-xs text-muted-foreground">Sem reuniões registradas.</p>
  }
  return (
    <ul className="space-y-0.5">
      {data.map((u) => (
        <li key={u.meeting_id} className="text-xs">
          <Link
            to={`/meetings/${u.meeting_id}`}
            className="text-primary hover:underline font-medium"
          >
            {u.title}
          </Link>{" "}
          <span className="text-muted-foreground">
            — {u.date.slice(0, 10)} · {u.count} {u.count === 1 ? "fala" : "falas"}
          </span>
        </li>
      ))}
    </ul>
  )
}

export default function SpeakersPage() {
  const queryClient = useQueryClient()

  const [expandedVoice, setExpandedVoice] = useState<string | null>(null)
  const [renameTarget, setRenameTarget] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState("")

  const { data: speakers, isLoading } = useQuery({
    queryKey: ["speakers"],
    queryFn: api.getSpeakers,
  })

  const { mutate: removeSpeaker, isPending: removeIsPending } = useMutation({
    mutationFn: (name: string) => api.deleteSpeaker(name),
    onSuccess: (_data, name) => {
      queryClient.invalidateQueries({ queryKey: ["speakers"] })
      if (expandedVoice === name) setExpandedVoice(null)
      toast.success(`Voz "${name}" removida`)
    },
    onError: (err: Error, name) => {
      toast.error(`Erro ao remover "${name}"`, { description: err.message })
    },
  })

  const renameMutation = useMutation({
    mutationFn: ({ name, newName }: { name: string; newName: string }) =>
      api.renameVoice(name, newName),
    onSuccess: (_data, { name, newName }) => {
      queryClient.invalidateQueries({ queryKey: ["speakers"] })
      queryClient.invalidateQueries({ queryKey: ["voice-usage"] })
      if (expandedVoice === name) setExpandedVoice(null)
      toast.success(`Voz "${name}" renomeada para "${newName}"`)
      setRenameTarget(null)
    },
    onError: (err: Error) =>
      toast.error("Erro ao renomear voz", { description: err.message }),
  })

  function openRename(name: string) {
    setRenameTarget(name)
    setRenameValue(name)
  }

  function toggleExpand(name: string) {
    setExpandedVoice((prev) => (prev === name ? null : name))
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="text-2xl font-semibold tracking-tight mb-6">Vozes</h1>

      {isLoading ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : !speakers?.length ? (
        <div className="flex flex-col items-center gap-3 py-20 text-muted-foreground">
          <Mic className="size-10 opacity-30" />
          <p className="font-medium text-sm">Nenhuma voz cadastrada</p>
          <p className="text-xs text-center max-w-xs leading-relaxed">
            Vozes entram no banco quando você nomeia um falante em uma reunião.
            Abra uma reunião, identifique os participantes na transcrição e elas
            aparecerão aqui automaticamente.
          </p>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Nome</TableHead>
              <TableHead className="text-muted-foreground">Dimensões</TableHead>
              <TableHead>Uso</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {speakers.map((speaker) => {
              const isExpanded = expandedVoice === speaker.name
              return (
                <Fragment key={speaker.name}>
                  <TableRow>
                    <TableCell className="font-medium">{speaker.name}</TableCell>
                    <TableCell className="text-muted-foreground text-xs font-mono">
                      {speaker.dims.toLocaleString("pt-BR")} dims
                    </TableCell>
                    <TableCell>
                      <button
                        type="button"
                        onClick={() => toggleExpand(speaker.name)}
                        className="inline-flex items-center gap-1"
                      >
                        <Badge
                          variant={isExpanded ? "default" : "secondary"}
                          className="cursor-pointer select-none"
                        >
                          {speaker.meetings}{" "}
                          {speaker.meetings === 1 ? "reunião" : "reuniões"}
                          {isExpanded ? (
                            <ChevronUp className="ml-1 size-3" />
                          ) : (
                            <ChevronDown className="ml-1 size-3" />
                          )}
                        </Badge>
                      </button>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1 justify-end">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-8 text-muted-foreground hover:text-foreground"
                          title="Renomear / Fundir"
                          onClick={() => openRename(speaker.name)}
                          disabled={removeIsPending || renameMutation.isPending}
                        >
                          <Pencil className="size-4" />
                          <span className="sr-only">Renomear</span>
                        </Button>
                        <AlertDialog>
                          <AlertDialogTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-8 text-muted-foreground hover:text-destructive"
                              disabled={removeIsPending || renameMutation.isPending}
                            >
                              <Trash2 className="size-4" />
                              <span className="sr-only">Remover</span>
                            </Button>
                          </AlertDialogTrigger>
                          <AlertDialogContent>
                            <AlertDialogHeader>
                              <AlertDialogTitle>
                                Remover voz "{speaker.name}"?
                              </AlertDialogTitle>
                              <AlertDialogDescription>
                                Remove a voz do banco de vozes. Reuniões existentes não
                                são alteradas — os nomes já atribuídos continuam
                                presentes nas transcrições.
                              </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                              <AlertDialogCancel>Cancelar</AlertDialogCancel>
                              <AlertDialogAction
                                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                                onClick={() => removeSpeaker(speaker.name)}
                              >
                                Remover
                              </AlertDialogAction>
                            </AlertDialogFooter>
                          </AlertDialogContent>
                        </AlertDialog>
                      </div>
                    </TableCell>
                  </TableRow>
                  {isExpanded && (
                    <TableRow>
                      <TableCell
                        colSpan={4}
                        className="pt-1 pb-3 pl-4 bg-muted/20 border-t-0"
                      >
                        <UsageList name={speaker.name} />
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              )
            })}
          </TableBody>
        </Table>
      )}

      {/* ── Rename / Merge Dialog ───────────────────────────────────────────── */}
      <Dialog
        open={renameTarget !== null}
        onOpenChange={(open) => !open && setRenameTarget(null)}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Renomear / Fundir voz</DialogTitle>
            <DialogDescription>
              Se o nome já existir no banco, as vozes serão fundidas (média dos
              embeddings). Também atualiza os falantes nas reuniões existentes.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="rename-input">Novo nome</Label>
            <Input
              id="rename-input"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && renameValue.trim() && renameTarget) {
                  renameMutation.mutate({ name: renameTarget, newName: renameValue.trim() })
                }
              }}
              list="speakers-datalist"
              autoFocus
            />
            <datalist id="speakers-datalist">
              {speakers
                ?.filter((s) => s.name !== renameTarget)
                .map((s) => (
                  <option key={s.name} value={s.name} />
                ))}
            </datalist>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRenameTarget(null)}
              disabled={renameMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={() => {
                if (renameTarget && renameValue.trim()) {
                  renameMutation.mutate({ name: renameTarget, newName: renameValue.trim() })
                }
              }}
              disabled={
                !renameValue.trim() ||
                renameValue.trim() === renameTarget ||
                renameMutation.isPending
              }
            >
              {renameMutation.isPending ? "Salvando…" : "Renomear"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
