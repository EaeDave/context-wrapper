import { Clock, MonitorPlay } from "lucide-react"
import type { VisualEvidence } from "@/lib/types"
import { formatTs } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"

const RELEVANCE_CLASS: Record<VisualEvidence["relevance"], string> = {
  high: "ring-1 ring-primary/40",
  medium: "ring-1 ring-border",
  low: "ring-1 ring-border opacity-75",
}

interface VisualEvidenceCardProps {
  items: VisualEvidence[]
  onSeek?: (seconds: number) => void
}

export function VisualEvidenceCard({ items, onSeek }: VisualEvidenceCardProps) {
  if (items.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <MonitorPlay className="size-4" />
          Evidências visuais
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
          {items.map((ev) => (
            <button
              key={ev.id}
              type="button"
              onClick={() => onSeek?.(ev.timestamp)}
              title={`Ir para ${formatTs(ev.timestamp)} — ${ev.description}`}
              className={cn(
                "group flex flex-col overflow-hidden rounded-lg bg-muted text-left transition-shadow hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                RELEVANCE_CLASS[ev.relevance],
              )}
            >
              <div className="relative aspect-video w-full overflow-hidden bg-black">
                <img
                  src={ev.thumbnail_url}
                  alt={ev.description}
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform group-hover:scale-105"
                />
                {/* Timestamp overlay */}
                <span className="absolute bottom-1 right-1 inline-flex items-center gap-0.5 rounded bg-black/70 px-1 py-0.5 font-mono text-[10px] text-white">
                  <Clock className="size-2.5" />
                  {formatTs(ev.timestamp)}
                </span>
              </div>

              <div className="flex flex-col gap-1 p-2">
                <p className="line-clamp-2 text-xs font-medium leading-snug">
                  {ev.description}
                </p>
                {ev.visible_text.length > 0 && (
                  <p className="line-clamp-2 text-[10px] leading-snug text-muted-foreground">
                    {ev.visible_text.join(" · ")}
                  </p>
                )}
              </div>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

/** Compact inline thumbnail strip used inside action-item and fact rows */
interface EvidenceThumbnailsProps {
  items: VisualEvidence[]
  onSeek?: (seconds: number) => void
}

export function EvidenceThumbnails({ items, onSeek }: EvidenceThumbnailsProps) {
  if (!items || items.length === 0) return null

  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {items.map((ev) => (
        <button
          key={ev.id}
          type="button"
          onClick={() => onSeek?.(ev.timestamp)}
          title={`Evidência visual — ${ev.description} (${formatTs(ev.timestamp)})`}
          className="group relative overflow-hidden rounded border border-border bg-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <img
            src={ev.thumbnail_url}
            alt={`Evidência visual em ${formatTs(ev.timestamp)}: ${ev.description}`}
            loading="lazy"
            className="h-10 w-16 object-cover transition-opacity group-hover:opacity-80"
          />
          <span className="absolute bottom-0 left-0 right-0 bg-black/70 text-center font-mono text-[9px] leading-tight text-white">
            {formatTs(ev.timestamp)}
          </span>
        </button>
      ))}
    </div>
  )
}
