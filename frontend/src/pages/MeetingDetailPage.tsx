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
} from "lucide-react"
import type { MeetingDetail } from "@/lib/types"
import * as api from "@/lib/api"
import { formatDuration } from "@/lib/format"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
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
import { Player } from "@/components/meeting/player"
import type { PlayerHandle } from "@/components/meeting/player"
import { Transcript } from "@/components/meeting/transcript"
import { ActionItems } from "@/components/meeting/action-items"
import { SpeakerAssign } from "@/components/meeting/speaker-assign"
import { ManageCard } from "@/components/meeting/manage"
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
}

function MeetingHeader({ meeting, onRename, onDelete, onRemix }: HeaderProps) {
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
              {meeting.participants.map((p) => (
                <Badge
                  key={p}
                  variant="outline"
                  className={cn("text-xs", speakerBadgeClass(p))}
                >
                  {p}
                </Badge>
              ))}
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
                triggerDownload(`/files?path=${encodeURIComponent(meeting.md_path!)}`)
              }
            >
              <FileText className="size-4" />
              Markdown
            </DropdownMenuItem>
          )}
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
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Resumo</CardTitle>
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

  // Rename dialog
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameValue, setRenameValue] = useState("")

  // Delete confirm
  const [deleteOpen, setDeleteOpen] = useState(false)

  const { data: meeting, isLoading, error } = useQuery<MeetingDetail>({
    queryKey: ["meeting", meetingId],
    queryFn: () => api.getMeeting(meetingId),
    enabled: !Number.isNaN(meetingId),
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

  const handleRenameOpen = useCallback(() => {
    setRenameValue(meeting?.title ?? "")
    setRenameOpen(true)
  }, [meeting?.title])

  const seekTo = useCallback((seconds: number) => {
    playerRef.current?.seekTo(seconds)
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
      />

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

          {meeting.pending.length > 0 && <SpeakerAssign meeting={meeting} />}

          {meeting.action_items.length > 0 && (
            <ActionItems items={meeting.action_items} />
          )}

          <Transcript
            groups={meeting.groups}
            currentTime={currentTime}
            seekTo={seekTo}
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
    </div>
  )
}
