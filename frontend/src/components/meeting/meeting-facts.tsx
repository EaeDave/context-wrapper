import { Clock, CheckCircle2, AlertCircle } from "lucide-react"
import type { MeetingFact } from "@/lib/types"
import { formatTs } from "@/lib/format"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"

// ── Kind labels & colours ──────────────────────────────────────────────────────
const KIND_META: Record<
  MeetingFact["kind"],
  { label: string; className: string }
> = {
  decision: {
    label: "Decisões",
    className: "text-blue-700 dark:text-blue-400",
  },
  requirement: {
    label: "Requisitos",
    className: "text-violet-700 dark:text-violet-400",
  },
  constraint: {
    label: "Restrições",
    className: "text-orange-700 dark:text-orange-400",
  },
  open_question: {
    label: "Questões em aberto",
    className: "text-rose-700 dark:text-rose-400",
  },
}

const KIND_ORDER: MeetingFact["kind"][] = [
  "decision",
  "requirement",
  "constraint",
  "open_question",
]

interface MeetingFactsProps {
  facts: MeetingFact[]
  onSeek?: (seconds: number) => void
}

export function MeetingFacts({ facts, onSeek }: MeetingFactsProps) {
  if (facts.length === 0) return null

  // Group by kind, preserving order
  const grouped = KIND_ORDER.reduce<Record<MeetingFact["kind"], MeetingFact[]>>(
    (acc, kind) => {
      acc[kind] = facts.filter((f) => f.kind === kind)
      return acc
    },
    { decision: [], requirement: [], constraint: [], open_question: [] },
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Fatos da reunião</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5 pb-4">
        {KIND_ORDER.filter((kind) => grouped[kind].length > 0).map((kind) => {
          const meta = KIND_META[kind]
          return (
            <section key={kind}>
              <h4 className={cn("mb-2 text-xs font-semibold uppercase tracking-wide", meta.className)}>
                {meta.label}
              </h4>
              <ul className="space-y-3">
                {grouped[kind].map((fact) => (
                  <li key={fact.id} className="flex flex-col gap-1">
                    <div className="flex items-start gap-2">
                      {/* Review status icon */}
                      {fact.review_status === "confirmed" ? (
                        <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-green-600 dark:text-green-400" />
                      ) : (
                        <AlertCircle className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                      )}

                      <p className="text-sm leading-snug">{fact.text}</p>
                    </div>

                    {/* Metadata badges + timestamp */}
                    <div className="ml-5 flex flex-wrap items-center gap-1.5">
                      <Badge
                        variant="outline"
                        className={cn(
                          "h-4 px-1 text-[10px]",
                          fact.review_status === "confirmed"
                            ? "border-green-500 text-green-700 dark:text-green-400"
                            : "border-amber-500 text-amber-600 dark:text-amber-400",
                        )}
                      >
                        {fact.review_status === "confirmed" ? "confirmado" : "revisar"}
                      </Badge>
                      <Badge
                        variant="outline"
                        className="h-4 px-1 text-[10px] text-muted-foreground"
                      >
                        {fact.explicitness === "explicit" ? "explícito" : "inferido"}
                      </Badge>
                      {fact.source_start != null && (
                        <button
                          type="button"
                          onClick={() => onSeek?.(fact.source_start!)}
                          className="inline-flex items-center gap-0.5 rounded text-[10px] text-muted-foreground hover:text-foreground transition-colors"
                          title="Ir para este momento no player"
                        >
                          <Clock className="size-2.5" />
                          {formatTs(fact.source_start)}
                        </button>
                      )}
                    </div>

                    {/* Evidence quote */}
                    {fact.evidence_quote && (
                      <p className="ml-5 text-[11px] italic text-muted-foreground border-l-2 border-muted pl-2 line-clamp-3">
                        {fact.evidence_quote}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )
        })}
      </CardContent>
    </Card>
  )
}
