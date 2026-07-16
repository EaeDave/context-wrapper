import { useState, useRef, useCallback } from "react"
import { useParams, useNavigate } from "react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  MoreHorizontal,
  Download,
  FileAudio,
  FileText,
  Shuffle,
  Trash2,
  Pencil,
  Video,
  VideoOff,
  Users,
  HardDrive,
  RefreshCcw,
  Wand2,
  Fingerprint,
  Copy,
  FolderKanban,
} from "lucide-react"
import type { MeetingDetail, Project } from "@/lib/types"
import * as api from "@/lib/api"
import { formatDuration } from "@/lib/format"
import { copyToClipboard } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Player } from "@/components/meeting/player"
import type { PlayerHandle } from "@/components/meeting/player"
import { Transcript } from "@/components/meeting/transcript"
import type { TranscriptJumpTarget } from "@/components/meeting/transcript"
import { ActionItems } from "@/components/meeting/action-items"
import { SpeakerAssign } from "@/components/meeting/speaker-assign"
import { ManageCard } from "@/components/meeting/manage"
import { MeetingFacts } from "@/components/meeting/meeting-facts"
import { VisualEvidenceCard } from "@/components/meeting/visual-evidence"
import { cn } from "@/lib/utils"

// ─── Speaker colour convention (shared with Transcript) ───────────────────────
function speakerBadgeClass(name: string): string {
  if (name === "me") return "bg-sky-500/20 text-sky-700 border-sky-500/40 dark:text-sky-300"
  if (/^SPEAKER_\d+$/.test(name))
    return "bg-amber-500/20 text-amber-700 border-amber-500/40 dark:text-amber-300"
  return "bg-emerald-500/20 text-emerald-700 border-emerald-500/40 dark:text-emerald-300"
}

// ─── Download helper ──────────────────────────────────────────────────────────
function triggerDownload(url: string) {
  const a = document.createElement("a")
  a.href = url
  a.download = ""
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

// ─── Loading skeleton ─────────────────────────────────────────────────────────
function PageSkeleton() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-8 space-y-6">
      <div className="flex items-start justify-between">
        <div className="space-y-2">
          <Skeleton className="h-7 w-64" />
          <div className="flex gap-2">
            <Skeleton className="h-5 w-24" />
            <Skeleton className="h-5 w-20" />
            <Skeleton className="h-5 w-16" />
          </div>
        </div>
        <Skeleton className="h-9 w-9 rounded-md" />
      </div>
      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <div className="space-y-6">
          <Skeleton className="h-40 w-full rounded-lg" />
          <Skeleton className="h-64 w-full rounded-lg" />
        </div>
        <Skeleton className="h-72 w-full rounded-lg" />
      </div>
    </div>
  )
}

// ─── Header (title + pills + menu) ───────────────────────────────────────────
interface HeaderProps {
  meeting: MeetingDetail
  onRename: () => void
  onDelete: () => void
  onRemix: () => void
  onReextract: () => void
  onReprocess: () => void
}

function MeetingHeader({ meeting, onRename, onDelete, onRemix, onReextract, onReprocess }: HeaderProps) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0 space-y-2">
        <h1 className="truncate text-2xl font-semibold tracking-tight">
          {meeting.title}
        </h1>
        <div className="flex flex-wrap items-center gap-1.5">
          {/* Date */}
          <Badge variant="secondary">{meeting.date}</Badge>

          {/* Duration */}
          <Badge variant="secondary">{formatDuration(meeting.duration)}</Badge>

          {/* Video/audio status */}
          {meeting.has_video ? (
            <Badge variant="outline" className="gap-1 border-emerald-500/50 text-emerald-700 dark:text-emerald-400">
              <Video className="size-3" /> vídeo
            </Badge>
          ) : (
            <Badge variant="outline" className="gap-1">
              <VideoOff className="size-3" /> só áudio
            </Badge>
          )}

          {/* Media managed / source */}
          {meeting.media_managed ? (
            <Badge variant="outline" className="gap-1">
              <HardDrive className="size-3" /> gerido
            </Badge>
          ) : (
            <Badge
              variant="outline"
              className="max-w-48 truncate font-mono text-xs"
              title={meeting.source_origin}
            >
              {meeting.source_origin}
            </Badge>
          )}

          {/* Participants */}
          {meeting.participants.length > 0 && (
            <div className="flex flex-wrap items-center gap-1">
              <Users className="size-3 text-muted-foreground" />
              {meeting.participants.map((p) => {
                const sim = meeting.speaker_matches[p]
                return (
                  <Badge
                    key={p}
                    variant="outline"
                    className={cn("text-xs gap-1", speakerBadgeClass(p))}
                  >
                    {p}
                    {sim != null && (
                      <span
                        title={`reconhecido por voz — similaridade ${sim.toFixed(2)}`}
                        className="inline-flex"
                      >
                        <Fingerprint className="size-3 opacity-60" />
                      </span>
                    )}
                  </Badge>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {/* ⋯ menu */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="icon" className="shrink-0">
            <MoreHorizontal className="size-4" />
            <span className="sr-only">Mais ações</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={onRename}>
            <Pencil className="size-4" />
            Renomear
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => triggerDownload(`/files?path=${encodeURIComponent(meeting.source)}`)}
            disabled={!meeting.source_exists}
          >
            <Download className="size-4" />
            Baixar fonte
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() =>
              triggerDownload(`/meetings/${meeting.id}/preview?q=web`)
            }
            disabled={!meeting.source_exists}
          >
            <Download className="size-4" />
            Baixar preview
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => triggerDownload(`/meetings/${meeting.id}/audio`)}
            disabled={!meeting.source_exists}
          >
            <FileAudio className="size-4" />
            Só áudio
          </DropdownMenuItem>
          {meeting.md_path && (
            <DropdownMenuItem
              onClick={() =>
                triggerDownload(`/api/meetings/${meeting.id}/markdown`)
              }
            >
              <FileText className="size-4" />
              Markdown
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={onReextract}>
            <Wand2 className="size-4" />
            Re-extrair resumo
          </DropdownMenuItem>
          <DropdownMenuItem onClick={onReprocess} disabled={!meeting.source_exists}>
            <RefreshCcw className="size-4" />
            Reprocessar (completo)
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={onRemix} disabled={!meeting.source_exists}>
            <Shuffle className="size-4" />
            Remixar
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="text-destructive focus:text-destructive"
            onClick={onDelete}
          >
            <Trash2 className="size-4" />
            Excluir
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

// ─── Summary card ─────────────────────────────────────────────────────────────
function SummaryCard({ summary }: { summary: string }) {
  async function handleCopy() {
    const ok = await copyToClipboard(summary)
    if (ok) toast.success("Resumo copiado")
    else toast.error("Não foi possível copiar")
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">Resumo</CardTitle>
          <Button
            variant="ghost"
            size="icon"
            className="size-7 text-muted-foreground hover:text-foreground"
            onClick={handleCopy}
            title="Copiar resumo"
          >
            <Copy className="size-3.5" />
            <span className="sr-only">Copiar resumo</span>
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
          {summary}
        </p>
      </CardContent>
    </Card>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────
export default function MeetingDetailPage() {
  const { id } = useParams<{ id: string }>()
  const meetingId = Number(id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const playerRef = useRef<PlayerHandle>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const jumpRequestId = useRef(0)
  const [transcriptJump, setTranscriptJump] = useState<TranscriptJumpTarget | null>(null)

  // Rename dialog
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameValue, setRenameValue] = useState("")

  // Delete confirm
  const [deleteOpen, setDeleteOpen] = useState(false)

  // Reprocess dialog
  const [reprocessOpen, setReprocessOpen] = useState(false)
  const [micTrack, setMicTrack] = useState(1)
  const [othersTrack, setOthersTrack] = useState(2)
  const [numSpeakers, setNumSpeakers] = useState(0)
  const [noLlm, setNoLlm] = useState(false)
  const [analyzeVisual, setAnalyzeVisual] = useState(false)

  const { data: meeting, isLoading, error } = useQuery<MeetingDetail>({
    queryKey: ["meeting", meetingId],
    queryFn: () => api.getMeeting(meetingId),
    enabled: !Number.isNaN(meetingId),
  })

  const projectsQuery = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: api.getProjects,
  })

  // ── Rename mutation ──────────────────────────────────────────────────────
  const renameMutation = useMutation({
    mutationFn: (title: string) => api.updateTitle(meetingId, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      queryClient.invalidateQueries({ queryKey: ["meetings"] })
      setRenameOpen(false)
      toast.success("Título atualizado")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Delete mutation ──────────────────────────────────────────────────────
  const deleteMutation = useMutation({
    mutationFn: () => api.deleteMeeting(meetingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] })
      toast.success("Reunião excluída")
      void navigate("/")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Remix mutation ───────────────────────────────────────────────────────
  const remixMutation = useMutation({
    mutationFn: () => api.mixMeeting(meetingId),
    onSuccess: (job) => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] })
      toast.success("Remix iniciado")
      void navigate(`/jobs/${job.id}`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Reextract mutation ───────────────────────────────────────────────────
  const reextractMutation = useMutation({
    mutationFn: () => api.reextractMeeting(meetingId),
    onSuccess: (job) => {
      toast.success("Re-extração iniciada")
      void navigate(`/jobs/${job.id}`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Reprocess mutation ───────────────────────────────────────────────────
  const reprocessMutation = useMutation({
    mutationFn: () =>
      api.reprocessMeeting(meetingId, {
        mic_track: micTrack,
        others_track: othersTrack,
        no_llm: noLlm,
        analyze_visual: analyzeVisual && !noLlm && Boolean(meeting?.has_video),
        num_speakers: numSpeakers || undefined,
      }),
    onSuccess: (job) => {
      setReprocessOpen(false)
      toast.success("Reprocessamento iniciado")
      void navigate(`/jobs/${job.id}`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  // ── Set project mutation ──────────────────────────────────────────────────
  const setMeetingProjectMutation = useMutation({
    mutationFn: (projectId: number | null) =>
      api.setMeetingProject(meetingId, projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] })
      queryClient.invalidateQueries({ queryKey: ["meetings"] })
      queryClient.invalidateQueries({ queryKey: ["projects"] })
      toast.success("Projeto atualizado")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const handleRenameOpen = useCallback(() => {
    setRenameValue(meeting?.title ?? "")
    setRenameOpen(true)
  }, [meeting?.title])

  const seekTo = useCallback((seconds: number) => {
    playerRef.current?.seekTo(seconds)
  }, [])

  const navigateToEvidence = useCallback((seconds: number, quote?: string | null) => {
    playerRef.current?.seekTo(seconds)
    jumpRequestId.current += 1
    setTranscriptJump({ seconds, quote, requestId: jumpRequestId.current })
  }, [])

  // ── Render states ────────────────────────────────────────────────────────
  if (isLoading) return <PageSkeleton />

  if (error || !meeting) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-8">
        <p className="text-destructive">
          {error instanceof Error ? error.message : "Reunião não encontrada."}
        </p>
      </div>
    )
  }

  // ── Layout ───────────────────────────────────────────────────────────────
  const hasMedia = meeting.source_exists

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      <MeetingHeader
        meeting={meeting}
        onRename={handleRenameOpen}
        onDelete={() => setDeleteOpen(true)}
        onRemix={() => remixMutation.mutate()}
        onReextract={() => reextractMutation.mutate()}
        onReprocess={() => setReprocessOpen(true)}
      />

      {/* Project selector */}
      <div className="mt-3 flex items-center gap-2">
        <FolderKanban className="size-4 shrink-0 text-muted-foreground" />
        <Select
          value={String(meeting.project_id ?? "none")}
          onValueChange={(v) =>
            setMeetingProjectMutation.mutate(v === "none" ? null : Number(v))
          }
        >
          <SelectTrigger className="h-8 w-52 text-sm">
            <SelectValue placeholder="Sem projeto" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">Sem projeto</SelectItem>
            {(projectsQuery.data ?? []).map((p) => (
              <SelectItem key={p.id} value={String(p.id)}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {setMeetingProjectMutation.isPending && (
          <span className="text-xs text-muted-foreground">Salvando…</span>
        )}
      </div>

      {/*
       * Two-column layout on lg+:
       *   - Player/ManageCard on the right (sticky, appears first in DOM → top on mobile)
       *   - Content sections on the left
       * CSS grid with explicit col-start makes the right column sticky-scroll smoothly.
       */}
      <div className="mt-6 lg:grid lg:grid-cols-[1fr_360px] lg:items-start lg:gap-6">
        {/* Right column — player (first in DOM = top on mobile) */}
        <aside className="mb-6 lg:col-start-2 lg:row-start-1 lg:mb-0">
          {hasMedia ? (
            <Player
              ref={playerRef}
              meeting={meeting}
              onTimeUpdate={setCurrentTime}
            />
          ) : (
            <ManageCard meeting={meeting} />
          )}
        </aside>

        {/* Left column — content */}
        <main className="space-y-6 lg:col-start-1 lg:row-start-1">
          {meeting.summary && <SummaryCard summary={meeting.summary} />}

          {meeting.visual_evidence && meeting.visual_evidence.length > 0 && (
            <VisualEvidenceCard
              items={meeting.visual_evidence}
              onSeek={seekTo}
            />
          )}

          {meeting.pending.length > 0 && <SpeakerAssign meeting={meeting} />}

          <ActionItems
            meetingId={meetingId}
            items={meeting.action_items}
            onSeek={navigateToEvidence}
          />

          <MeetingFacts facts={meeting.facts ?? []} onSeek={navigateToEvidence} />

          <Transcript
            meetingId={meetingId}
            groups={meeting.groups}
            participants={meeting.participants}
            currentTime={currentTime}
            seekTo={seekTo}
            jumpTarget={transcriptJump}
            speakerMatches={meeting.speaker_matches}
          />
        </main>
      </div>

      {/* ── Rename dialog ─────────────────────────────────────────────────── */}
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Renomear reunião</DialogTitle>
          </DialogHeader>
          <Input
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && renameValue.trim()) {
                renameMutation.mutate(renameValue.trim())
              }
            }}
            autoFocus
          />
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRenameOpen(false)}
              disabled={renameMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={() => renameMutation.mutate(renameValue.trim())}
              disabled={!renameValue.trim() || renameMutation.isPending}
            >
              Salvar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete confirmation ───────────────────────────────────────────── */}
      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Excluir reunião?</AlertDialogTitle>
            <AlertDialogDescription>
              Esta ação não pode ser desfeita. A reunião{" "}
              <strong>{meeting.title}</strong> e todos os seus dados serão
              excluídos permanentemente.
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

      {/* ── Reprocess dialog ─────────────────────────────────────────────── */}
      <Dialog open={reprocessOpen} onOpenChange={setReprocessOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Reprocessar reunião</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="mic-track">Track mic</Label>
                <Input
                  id="mic-track"
                  type="number"
                  min={1}
                  value={micTrack}
                  onChange={(e) => setMicTrack(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="others-track">Track outros</Label>
                <Input
                  id="others-track"
                  type="number"
                  min={1}
                  value={othersTrack}
                  onChange={(e) => setOthersTrack(Number(e.target.value))}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="num-speakers">Falantes remotos (0 = automático)</Label>
              <Input
                id="num-speakers"
                type="number"
                min={0}
                placeholder="automático"
                value={numSpeakers || ""}
                onChange={(e) => setNumSpeakers(Number(e.target.value))}
              />
            </div>
            <div className="flex items-center gap-2">
              <Checkbox
                id="no-llm"
                checked={noLlm}
                onCheckedChange={(v) => setNoLlm(!!v)}
              />
              <Label htmlFor="no-llm" className="cursor-pointer font-normal">
                Sem LLM (apenas transcrição)
              </Label>
            </div>
            <div className="flex items-start gap-2">
              <Checkbox
                id="reprocess-analyze-visual"
                checked={analyzeVisual && !noLlm && Boolean(meeting?.has_video)}
                disabled={noLlm || !meeting?.has_video}
                onCheckedChange={(v) => setAnalyzeVisual(!!v)}
              />
              <div className="space-y-1">
                <Label
                  htmlFor="reprocess-analyze-visual"
                  className="cursor-pointer font-normal"
                >
                  Analisar conteúdo da tela
                </Label>
                <p className="text-xs text-muted-foreground">
                  Frames relevantes são enviados ao provider configurado.
                </p>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setReprocessOpen(false)}
              disabled={reprocessMutation.isPending}
            >
              Cancelar
            </Button>
            <Button
              onClick={() => reprocessMutation.mutate()}
              disabled={reprocessMutation.isPending}
            >
              Reprocessar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
