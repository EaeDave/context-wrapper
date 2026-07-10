import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { UserCheck } from "lucide-react"
import type { MeetingDetail } from "@/lib/types"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import * as api from "@/lib/api"

interface SpeakerAssignProps {
  meeting: MeetingDetail
}

export function SpeakerAssign({ meeting }: SpeakerAssignProps) {
  const queryClient = useQueryClient()
  const [names, setNames] = useState<Record<string, string>>(
    () => Object.fromEntries(meeting.pending.map((l) => [l, ""]))
  )

  const mutation = useMutation({
    mutationFn: ({ label, name }: { label: string; name: string }) =>
      api.assignSpeaker(meeting.id, label, name),
    onSuccess: (_data, { name }) => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meeting.id] })
      toast.success(`Voz cadastrada: ${name}`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function submit(label: string) {
    const name = names[label]?.trim()
    if (name) mutation.mutate({ label, name })
  }

  return (
    <Card className="border-amber-500/60">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base text-amber-600 dark:text-amber-400">
          <UserCheck className="size-4" />
          Nomear falantes
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {meeting.pending.map((label) => (
          <div key={label} className="flex items-center gap-2">
            <span className="w-28 shrink-0 font-mono text-sm text-muted-foreground">
              {label}
            </span>
            <Input
              className="h-8"
              placeholder="Nome do falante"
              value={names[label] ?? ""}
              onChange={(e) =>
                setNames((prev) => ({ ...prev, [label]: e.target.value }))
              }
              onKeyDown={(e) => {
                if (e.key === "Enter") submit(label)
              }}
            />
            <Button
              size="sm"
              className="h-8 shrink-0"
              disabled={!names[label]?.trim() || mutation.isPending}
              onClick={() => submit(label)}
            >
              Nomear
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
