import { useState } from "react"
import { useNavigate } from "react-router"
import { useQuery, useMutation } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Home,
  Folder,
  FileVideo,
  FileAudio,
  ChevronRight,
  ArrowUp,
  Film,
  TriangleAlert,
} from "lucide-react"

import * as api from "@/lib/api"
import { formatSize } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

// Folder names that the backend puts as quick dirs (not the home dir itself)
const NAMED_DIRS: Record<string, true> = {
  Videos: true,
  "Vídeos": true,
  Downloads: true,
  reunioes: true,
}
const AUDIO_EXTS: Record<string, true> = {
  ".m4a": true,
  ".mp3": true,
  ".wav": true,
  ".flac": true,
  ".ogg": true,
  ".opus": true,
  ".aac": true,
}

function extOf(name: string): string {
  const dot = name.lastIndexOf(".")
  return dot >= 0 ? name.slice(dot).toLowerCase() : ""
}

function buildBreadcrumbs(path: string): { label: string; path: string }[] {
  const segments = path.split("/").filter(Boolean)
  return segments.map((seg, i) => ({
    label: seg,
    path: "/" + segments.slice(0, i + 1).join("/"),
  }))
}

const TRACK_OPTIONS = Array.from({ length: 8 }, (_, i) => String(i + 1))

export default function NewMeetingPage() {
  const navigate = useNavigate()

  // File browser state
  const [currentPath, setCurrentPath] = useState<string | undefined>(undefined)
  const [selectedFile, setSelectedFile] = useState("")

  // Form state
  const [title, setTitle] = useState("")
  const [micTrack, setMicTrack] = useState("1")
  const [othersTrack, setOthersTrack] = useState("2")
  const [numSpeakers, setNumSpeakers] = useState("")
  const [importMedia, setImportMedia] = useState(true)
  const [noLlm, setNoLlm] = useState(false)

  const { data: browse, isLoading: browseLoading } = useQuery({
    queryKey: ["browse", currentPath ?? ""],
    queryFn: () => api.browse(currentPath),
    staleTime: 10_000,
  })

  const probePath = selectedFile.trim()
  const { data: probeInfo } = useQuery({
    queryKey: ["probe", probePath],
    queryFn: () => api.probe(probePath),
    enabled: probePath.length > 0,
    staleTime: 60_000,
    retry: false,
  })
  const singleTrack = probeInfo != null && probeInfo.audio_streams < 2

  const { mutate: startProcess, isPending } = useMutation({
    mutationFn: () =>
      api.startProcess({
        video: selectedFile,
        title: title.trim() || undefined,
        mic_track: Number(micTrack),
        others_track: Number(othersTrack),
        num_speakers: numSpeakers.trim() ? Number(numSpeakers) : 0,
        import_media: importMedia,
        no_llm: noLlm,
      }),
    onSuccess: (job) => {
      toast.success("Processamento iniciado")
      navigate(`/jobs/${job.id}`)
    },
    onError: (err: Error) => {
      toast.error("Erro ao iniciar processamento", { description: err.message })
    },
  })

  const breadcrumbs = browse ? buildBreadcrumbs(browse.path) : []

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="text-2xl font-semibold tracking-tight mb-6">Nova reunião</h1>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
        {/* ── File Browser ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base font-medium">Selecionar arquivo</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {/* Quick dirs */}
            {browse?.quick.length ? (
              <div className="flex flex-wrap gap-1.5">
                {browse.quick.map((qPath) => {
                  const name = qPath.split("/").filter(Boolean).pop() ?? qPath
                  const isHome = !(name in NAMED_DIRS)
                  return (
                    <Button
                      key={qPath}
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs gap-1.5"
                      onClick={() => setCurrentPath(qPath)}
                    >
                      {isHome ? (
                        <Home className="size-3.5" />
                      ) : (
                        <Folder className="size-3.5" />
                      )}
                      {isHome ? "~" : name}
                    </Button>
                  )
                })}
              </div>
            ) : null}

            {/* Breadcrumb + parent button */}
            <div className="flex items-center gap-1 min-w-0">
              {browse?.parent != null && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 shrink-0"
                  onClick={() => setCurrentPath(browse.parent ?? undefined)}
                  title="Pasta pai"
                >
                  <ArrowUp className="size-4" />
                </Button>
              )}
              <div className="flex items-center gap-0.5 overflow-x-auto text-xs text-muted-foreground min-w-0 flex-1">
                <button
                  className="hover:text-foreground shrink-0"
                  onClick={() => setCurrentPath("/")}
                >
                  /
                </button>
                {breadcrumbs.map((crumb, i) => (
                  <span key={crumb.path} className="flex items-center gap-0.5 shrink-0">
                    <ChevronRight className="size-3 opacity-40" />
                    <button
                      className={cn(
                        "hover:text-foreground max-w-[120px] truncate",
                        i === breadcrumbs.length - 1 && "text-foreground font-medium",
                      )}
                      onClick={() => setCurrentPath(crumb.path)}
                    >
                      {crumb.label}
                    </button>
                  </span>
                ))}
              </div>
            </div>

            {/* Entry list */}
            {browseLoading ? (
              <div className="space-y-1">
                {[0, 1, 2, 3, 4].map((i) => (
                  <Skeleton key={i} className="h-9 w-full rounded" />
                ))}
              </div>
            ) : !browse?.entries.length ? (
              <div className="flex flex-col items-center gap-2 py-10 text-muted-foreground">
                <Film className="size-8 opacity-30" />
                <p className="text-xs">Nenhum arquivo de mídia aqui</p>
              </div>
            ) : (
              <ScrollArea className="h-[340px] pr-2">
                <div className="space-y-0.5">
                  {browse.entries.map((entry) => {
                    const isDir = entry.kind === "dir"
                    const isSelected = !isDir && entry.path === selectedFile
                    const ext = extOf(entry.name)
                    const FileIcon = ext in AUDIO_EXTS ? FileAudio : FileVideo

                    return (
                      <button
                        key={entry.path}
                        className={cn(
                          "w-full flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-left transition-colors",
                          isDir
                            ? "hover:bg-accent/60 text-foreground"
                            : "hover:bg-accent/40 text-foreground",
                          isSelected &&
                            "ring-1 ring-primary bg-primary/5 hover:bg-primary/8",
                        )}
                        onClick={() => {
                          if (isDir) {
                            setCurrentPath(entry.path)
                          } else {
                            setSelectedFile(entry.path)
                          }
                        }}
                      >
                        {isDir ? (
                          <Folder className="size-4 text-muted-foreground shrink-0" />
                        ) : (
                          <FileIcon className="size-4 text-muted-foreground shrink-0" />
                        )}
                        <span className="flex-1 truncate">{entry.name}</span>
                        {!isDir && entry.size != null && (
                          <span className="text-xs text-muted-foreground shrink-0">
                            {formatSize(entry.size)}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        {/* ── Form ── */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base font-medium">Processar</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            {/* Selected path (editable) */}
            <div className="space-y-1.5">
              <Label htmlFor="video-path">Arquivo</Label>
              <Input
                id="video-path"
                value={selectedFile}
                onChange={(e) => setSelectedFile(e.target.value)}
                placeholder="/caminho/para/arquivo.mp4"
                className="font-mono text-xs"
              />
            </div>
            {singleTrack && (
              <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                <TriangleAlert className="mt-0.5 size-4 shrink-0 text-amber-400" />
                <span>
                  Este arquivo tem{" "}
                  {probeInfo.audio_streams === 1
                    ? "só 1 faixa de áudio"
                    : "0 faixas de áudio"}
                  {" "}— a separação mic/outros não vai funcionar. Falantes
                  serão detectados só por voz (você pode virar um SPEAKER_XX).
                  Prefira o .mkv multi-track do OBS.
                </span>
              </div>
            )}

            {/* Title */}
            <div className="space-y-1.5">
              <Label htmlFor="meeting-title">Título</Label>
              <Input
                id="meeting-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="automático pelo nome do arquivo"
              />
            </div>

            {/* Tracks */}
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="mic-track">Track mic</Label>
                <Select value={micTrack} onValueChange={setMicTrack}>
                  <SelectTrigger id="mic-track">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TRACK_OPTIONS.map((n) => (
                      <SelectItem key={n} value={n}>
                        {n}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="others-track">Track outros</Label>
                <Select value={othersTrack} onValueChange={setOthersTrack}>
                  <SelectTrigger id="others-track">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TRACK_OPTIONS.map((n) => (
                      <SelectItem key={n} value={n}>
                        {n}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Num speakers */}
            <div className="space-y-1.5">
              <Label htmlFor="num-speakers">Falantes remotos (fora você)</Label>
              <Input
                id="num-speakers"
                type="number"
                min={0}
                placeholder="automático"
                value={numSpeakers}
                onChange={(e) => setNumSpeakers(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Nº de participantes remotos na track "outros". Deixe vazio para detecção automática.
              </p>
            </div>

            {/* Checkboxes */}
            <div className="space-y-3">
              <div className="flex items-center gap-2.5">
                <Checkbox
                  id="import-media"
                  checked={importMedia}
                  onCheckedChange={(v) => setImportMedia(v === true)}
                />
                <Label htmlFor="import-media" className="font-normal cursor-pointer">
                  Importar mídia p/ acervo
                </Label>
              </div>
              <div className="flex items-center gap-2.5">
                <Checkbox
                  id="no-llm"
                  checked={noLlm}
                  onCheckedChange={(v) => setNoLlm(v === true)}
                />
                <Label htmlFor="no-llm" className="font-normal cursor-pointer">
                  Sem LLM (só transcrição)
                </Label>
              </div>
            </div>

            {/* Submit */}
            <Button
              className="w-full"
              disabled={!selectedFile.trim() || isPending}
              onClick={() => startProcess()}
            >
              {isPending ? "Iniciando…" : "Processar gravação"}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
