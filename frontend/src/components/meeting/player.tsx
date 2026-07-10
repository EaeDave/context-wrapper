/**
 * Player — native <video>/<audio> fallback path.
 *
 * @vidstack/react@0.6.15 (latest on npm 2026-07) is the old beta API with no
 * DefaultVideoLayout; the new 1.x API is not published under that package name.
 * Using approved fallback: native <video controls> + quality Select +
 * speed DropdownMenu, seekTo/currentTime via ref.
 */
import { useRef, useState, useCallback, useEffect, useImperativeHandle } from "react"
import type { Ref } from "react"
import { ChevronDown } from "lucide-react"
import type { MeetingDetail } from "@/lib/types"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/** Public handle exposed to the parent via ref. */
export interface PlayerHandle {
  seekTo(seconds: number): void
}

type Quality = "full" | "web"
type PlaybackSpeed = 0.75 | 1 | 1.25 | 1.5 | 2

const SPEEDS: PlaybackSpeed[] = [0.75, 1, 1.25, 1.5, 2]

interface PlayerProps {
  meeting: MeetingDetail
  onTimeUpdate?: (time: number) => void
  /** React 19: ref is a plain prop. */
  ref?: Ref<PlayerHandle>
}

export function Player({ meeting, onTimeUpdate, ref }: PlayerProps) {
  const mediaRef = useRef<HTMLMediaElement | null>(null)
  const [quality, setQuality] = useState<Quality>("web")
  const [speed, setSpeed] = useState<PlaybackSpeed>(1)
  const [isLoading, setIsLoading] = useState(true)
  const [usedFallback, setUsedFallback] = useState(false)

  // Saved across quality switches.
  const savedTimeRef = useRef(0)
  const wasPlayingRef = useRef(false)
  const stallTimerRef = useRef<number | null>(null)
  // Throttle timeupdate emissions to ~500 ms.
  const lastEmitRef = useRef(0)

  const seekTo = useCallback((seconds: number) => {
    if (mediaRef.current) mediaRef.current.currentTime = seconds
  }, [])

  useImperativeHandle(ref, () => ({ seekTo }), [seekTo])

  const clearStallTimer = useCallback(() => {
    if (stallTimerRef.current !== null) {
      window.clearTimeout(stallTimerRef.current)
      stallTimerRef.current = null
    }
  }, [])

  const fallbackToWeb = useCallback(() => {
    clearStallTimer()
    const el = mediaRef.current
    savedTimeRef.current = el?.currentTime ?? 0
    wasPlayingRef.current = el ? !el.paused : false
    setUsedFallback(true)
    setIsLoading(true)
    setQuality("web")
  }, [clearStallTimer])

  const handleTimeUpdate = useCallback(() => {
    const el = mediaRef.current
    if (!el) return
    const now = performance.now()
    if (now - lastEmitRef.current > 500) {
      lastEmitRef.current = now
      onTimeUpdate?.(el.currentTime)
    }
  }, [onTimeUpdate])

  const handleCanPlay = useCallback(() => setIsLoading(false), [])

  const handlePlaying = useCallback(() => {
    clearStallTimer()
    setIsLoading(false)
  }, [clearStallTimer])

  const handleError = useCallback(() => {
    if (quality === "full" && !usedFallback) fallbackToWeb()
  }, [quality, usedFallback, fallbackToWeb])

  const handleLoadedMetadata = useCallback(() => {
    const el = mediaRef.current
    if (!el) return
    if (savedTimeRef.current > 0) el.currentTime = savedTimeRef.current
    el.playbackRate = speed
    if (wasPlayingRef.current) el.play().catch(() => {})
  }, [speed])

  const handleChangeQuality = useCallback(
    (q: Quality) => {
      const el = mediaRef.current
      if (el) {
        savedTimeRef.current = el.currentTime
        wasPlayingRef.current = !el.paused
      }
      setIsLoading(true)
      setQuality(q)
    },
    []
  )

  // Sync playback rate when changed without a quality switch.
  useEffect(() => {
    if (mediaRef.current) mediaRef.current.playbackRate = speed
  }, [speed])

  // Stall detection for full quality: if readyState hasn't reached HAVE_FUTURE_DATA
  // within 2.5 s, auto-switch to web quality.
  useEffect(() => {
    if (quality === "full" && !usedFallback && meeting.source_exists) {
      stallTimerRef.current = window.setTimeout(() => {
        const el = mediaRef.current
        // readyState < 3 = HAVE_FUTURE_DATA — still buffering.
        if (el && el.readyState < 3) fallbackToWeb()
      }, 2500)
    }
    return clearStallTimer
  }, [quality, usedFallback, meeting.source_exists, fallbackToWeb, clearStallTimer])

  if (!meeting.source_exists) return null

  const src = meeting.has_video
    ? `/meetings/${meeting.id}/preview?q=${quality}`
    : `/meetings/${meeting.id}/audio`

  // Preview may be generated on first request by ffmpeg (on-demand).
  const previewPending =
    meeting.has_video && !meeting.preview_ready && !meeting.preview_full_ready

  return (
    <Card className="sticky top-16 z-10">
      <CardHeader className="pb-2 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          {meeting.has_video && (
            <Select
              value={quality}
              onValueChange={(v) => handleChangeQuality(v as Quality)}
            >
              <SelectTrigger className="h-7 w-auto gap-1 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="web">{meeting.quality_web_h}p leve</SelectItem>
                <SelectItem value="full">
                  {meeting.quality_full_h}p original
                </SelectItem>
              </SelectContent>
            </Select>
          )}

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="h-7 gap-1 text-xs">
                {speed}×<ChevronDown className="size-3" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {SPEEDS.map((s) => (
                <DropdownMenuItem
                  key={s}
                  onClick={() => setSpeed(s)}
                  className={speed === s ? "font-semibold" : ""}
                >
                  {s}×
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          {usedFallback && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              qualidade reduzida (fallback automático)
            </span>
          )}

          {previewPending && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="cursor-help text-xs text-muted-foreground">
                    gerando preview…
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  O ffmpeg está gerando o preview on-demand. O primeiro carregamento pode
                  demorar alguns segundos.
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
        </div>
      </CardHeader>

      <CardContent className="pb-4">
        <div className="relative">
          {isLoading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-black/50">
              <div className="flex flex-col items-center gap-2 text-white">
                <div className="size-6 animate-spin rounded-full border-2 border-white border-t-transparent" />
                <span className="text-xs">
                  {previewPending ? "gerando preview…" : "carregando…"}
                </span>
              </div>
            </div>
          )}

          {meeting.has_video ? (
            <video
              key={`${meeting.id}-${quality}`}
              ref={(el) => {
                mediaRef.current = el
              }}
              src={src}
              controls
              className="w-full rounded-lg border"
              onTimeUpdate={handleTimeUpdate}
              onCanPlay={handleCanPlay}
              onLoadedMetadata={handleLoadedMetadata}
              onError={handleError}
              onPlaying={handlePlaying}
            />
          ) : (
            <audio
              key={`${meeting.id}-audio`}
              ref={(el) => {
                mediaRef.current = el
              }}
              src={src}
              controls
              className="w-full"
              onTimeUpdate={handleTimeUpdate}
              onCanPlay={handleCanPlay}
              onLoadedMetadata={handleLoadedMetadata}
            />
          )}
        </div>
      </CardContent>
    </Card>
  )
}
