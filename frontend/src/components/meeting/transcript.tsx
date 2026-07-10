import { useRef, useEffect, useState } from "react"
import { FileText } from "lucide-react"
import type { TranscriptGroup } from "@/lib/types"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { formatTs } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Returns the CSS class for a speaker label per the shared convention. */
function speakerClass(speaker: string): string {
  if (speaker === "me") return "text-sky-500 font-medium"
  if (/^SPEAKER_\d+$/.test(speaker)) return "text-amber-500 font-medium"
  return "text-emerald-600 font-medium dark:text-emerald-400"
}

interface TranscriptProps {
  groups: TranscriptGroup[]
  currentTime: number
  seekTo: (seconds: number) => void
}

export function Transcript({ groups, currentTime, seekTo }: TranscriptProps) {
  const [autoScroll, setAutoScroll] = useState(false)
  const activeRef = useRef<HTMLDivElement | null>(null)

  const activeIndex = groups.findIndex(
    (g) => currentTime >= g.start && currentTime < g.end
  )

  useEffect(() => {
    if (autoScroll && activeIndex >= 0 && activeRef.current) {
      activeRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" })
    }
  }, [autoScroll, activeIndex])

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
                    "grid grid-cols-[5rem_1fr] gap-3 rounded-md px-2 py-1.5 transition-colors",
                    isActive && "border-l-2 border-l-primary bg-primary/5"
                  )}
                >
                  <button
                    type="button"
                    onClick={() => seekTo(g.start)}
                    className={cn(
                      "mt-0.5 text-left font-mono text-xs transition-colors",
                      isActive
                        ? "text-primary"
                        : "text-muted-foreground hover:text-primary"
                    )}
                  >
                    {formatTs(g.start)}
                  </button>
                  <div>
                    <span className={cn("text-xs", speakerClass(g.speaker))}>
                      {g.speaker}
                    </span>
                    <p className="mt-0.5 text-sm leading-relaxed">{g.text}</p>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
