import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { FolderOpen } from "lucide-react"
import type { MeetingDetail } from "@/lib/types"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import * as api from "@/lib/api"

interface ManageCardProps {
  meeting: MeetingDetail
}

export function ManageCard({ meeting }: ManageCardProps) {
  const queryClient = useQueryClient()
  const [path, setPath] = useState("")
  const [importMedia, setImportMedia] = useState(false)

  const mutation = useMutation({
    mutationFn: () => api.relink(meeting.id, path.trim(), importMedia),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meeting.id] })
      toast.success("Vídeo vinculado com sucesso")
    },
    onError: (e: Error) => toast.error(e.message),
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <FolderOpen className="size-4" />
          Localizar vídeo
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Arquivo original não encontrado em{" "}
          <span className="font-mono text-xs">{meeting.source}</span>. Informe o novo caminho:
        </p>
        <Input
          placeholder="/caminho/para/reuniao.mp4"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && path.trim()) mutation.mutate()
          }}
        />
        <div className="flex items-center gap-2">
          <Checkbox
            id="import-media"
            checked={importMedia}
            onCheckedChange={(v) => setImportMedia(Boolean(v))}
          />
          <Label htmlFor="import-media" className="cursor-pointer text-sm">
            Importar (copiar para a biblioteca)
          </Label>
        </div>
        <Button
          className="w-full"
          disabled={!path.trim() || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          Vincular
        </Button>
      </CardContent>
    </Card>
  )
}
