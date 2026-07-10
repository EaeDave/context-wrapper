import { useRef, useEffect, useState } from "react"
import { FileText, Pencil, UserCircle } from "lucide-react"
import { useQueryClient, useMutation } from "@tanstack/react-query"
import { toast } from "sonner"
import type { TranscriptGroup } from "@/lib/types"
import * as api from "@/lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
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
import { formatTs } from "@/lib/format"
import { cn } from "@/lib/utils"

/** CSS class for a speaker label — shared convention. */
function speakerClass(speaker: string): string {
  if (speaker === "me") return "text-sky-500 font-medium"
  if (/^SPEAKER_\d+$/.test(speaker)) return "text-amber-500 font-medium"
  return "text-emerald-600 font-medium dark:text-emerald-400"
}

type EditMode =
  | { kind: "speaker"; group: TranscriptGroup }
  | { kind: "text"; group: TranscriptGroup }

interface TranscriptProps {
  meetingId: number
  groups: TranscriptGroup[]
  participants: string[]
  currentTime: number
  seekTo: (seconds: number) => void
}

export function Transcript({
  meetingId,
  groups,
  participants,
  currentTime,
  seekTo,
}: TranscriptProps) {
  const [autoScroll, setAutoScroll] = useState(false)
  const activeRef = useRef<HTMLDivElement | null>(null)
  const queryClient = useQueryClient()

  // Edit state
  const [editMode, setEditMode] = useState<EditMode | null>(null)
  const [speakerSelect, setSpeakerSelect] = useState("")
  const [speakerFree, setSpeakerFree] = useState("")
  const [textValue, setTextValue] = useState("")

  // Turno ativo = último turno já iniciado (start <= currentTime). Contenção
  // [start,end) falha: mic ("me") e desktop são transcritos em separado e seus
  // tempos se sobrepõem — um turno "me" longo engloba vários turnos de outro
  // falante, prendendo o destaque no "me". Grupos vêm ordenados por start.
  let activeIndex = -1
  for (let i = 0; i < groups.length; i++) {
    if (groups[i].start <= currentTime) activeIndex = i
    else break
  }

  useEffect(() => {
    if (autoScroll && activeIndex >= 0 && activeRef.current) {
      activeRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" })
    }
  }, [autoScroll, activeIndex])

  const updateMutation = useMutation({
    mutationFn: (body: { seg_ids: number[]; text?: string; speaker?: string }) =>
      api.updateTurn(meetingId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      setEditMode(null)
      toast.success("Transcrição atualizada")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function openSpeaker(group: TranscriptGroup) {
    setEditMode({ kind: "speaker", group })
    // Pre-select current speaker if it exists in participants list
    setSpeakerSelect(participants.includes(group.speaker) ? group.speaker : "")
    setSpeakerFree(participants.includes(group.speaker) ? "" : group.speaker)
  }

  function openText(group: TranscriptGroup) {
    setEditMode({ kind: "text", group })
    setTextValue(group.text)
  }

  function submitSpeaker() {
    if (!editMode) return
    const speaker = speakerFree.trim() || speakerSelect
    if (!speaker) return
    updateMutation.mutate({ seg_ids: editMode.group.seg_ids, speaker })
  }

  function submitText() {
    if (!editMode) return
    const text = textValue.trim()
    if (!text) return
    updateMutation.mutate({ seg_ids: editMode.group.seg_ids, text })
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="size-4" />
            Transcrição
          </CardTitle>
          <Button
            variant={autoScroll ? "default" : "outline"}
            size="sm"
            className="h-7 text-xs"
            onClick={() => setAutoScroll((v) => !v)}
          >
            seguir áudio
          </Button>
        </div>
      </CardHeader>

      <CardContent className="pt-0">
        {groups.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            Sem transcrição disponível.
          </p>
        ) : (
          <div className="space-y-0.5">
            {groups.map((g, i) => {
              const isActive = i === activeIndex
              return (
                <div
                  key={i}
                  ref={isActive ? activeRef : null}
                  className={cn(
                    "group grid grid-cols-[5rem_1fr_auto] gap-3 rounded-md px-2 py-1.5 transition-colors",
                    isActive && "border-l-2 border-l-primary bg-primary/5",
                  )}
                >
                  {/* Timestamp */}
                  <button
                    type="button"
                    onClick={() => seekTo(g.start)}
                    className={cn(
                      "mt-0.5 text-left font-mono text-xs transition-colors",
                      isActive
                        ? "text-primary"
                        : "text-muted-foreground hover:text-primary",
                    )}
                  >
                    {formatTs(g.start)}
                  </button>

                  {/* Speaker + text */}
                  <div>
                    <span className={cn("text-xs", speakerClass(g.speaker))}>
                      {g.speaker}
                    </span>
                    <p className="mt-0.5 text-sm leading-relaxed">{g.text}</p>
                  </div>

                  {/* Hover actions */}
                  <div className="flex items-start gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-6"
                      title="Trocar falante"
                      onClick={() => openSpeaker(g)}
                    >
                      <UserCircle className="size-3.5" />
                      <span className="sr-only">Trocar falante</span>
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-6"
                      title="Editar texto"
                      onClick={() => openText(g)}
                    >
                      <Pencil className="size-3.5" />
                      <span className="sr-only">Editar texto</span>
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>

      {/* ── Trocar falante ─────────────────────────────────────────────────── */}
      <Dialog
        open={editMode?.kind === "speaker"}
        onOpenChange={(open) => !open && setEditMode(null)}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Trocar falante</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            {participants.length > 0 && (
              <div className="space-y-1.5">
                <Label>Falante existente</Label>
                <Select
                  value={speakerSelect}
                  onValueChange={(v) => {
                    setSpeakerSelect(v)
                    setSpeakerFree("")
                  }}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Selecionar..." />
                  </SelectTrigger>
                  <SelectContent>
                    {participants.map((p) => (
                      <SelectItem key={p} value={p}>
                        {p}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="space-y-1.5">
              <Label htmlFor="speaker-free">
                {participants.length > 0 ? "Ou escreva um nome" : "Nome do falante"}
              </Label>
              <Input
                id="speaker-free"
                value={speakerFree}
                onChange={(e) => {
                  setSpeakerFree(e.target.value)
                  if (e.target.value) setSpeakerSelect("")
                }}
                placeholder="Nome livre..."
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditMode(null)}
              disabled={updateMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={submitSpeaker}
              disabled={
                (!speakerSelect && !speakerFree.trim()) || updateMutation.isPending
              }
            >
              Trocar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Editar texto ──────────────────────────────────────────────────── */}
      <Dialog
        open={editMode?.kind === "text"}
        onOpenChange={(open) => !open && setEditMode(null)}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Editar texto</DialogTitle>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="text-edit">Texto</Label>
            <textarea
              id="text-edit"
              value={textValue}
              onChange={(e) => setTextValue(e.target.value)}
              rows={6}
              className="flex min-h-[80px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 resize-none"
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditMode(null)}
              disabled={updateMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={submitText}
              disabled={!textValue.trim() || updateMutation.isPending}
            >
              Salvar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
