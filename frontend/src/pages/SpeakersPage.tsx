import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Trash2, Mic } from "lucide-react"

import * as api from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Skeleton } from "@/components/ui/skeleton"

export default function SpeakersPage() {
  const queryClient = useQueryClient()

  const { data: speakers, isLoading } = useQuery({
    queryKey: ["speakers"],
    queryFn: api.getSpeakers,
  })

  const { mutate: removeSpeaker, isPending } = useMutation({
    mutationFn: (name: string) => api.deleteSpeaker(name),
    onSuccess: (_data, name) => {
      queryClient.invalidateQueries({ queryKey: ["speakers"] })
      toast.success(`Voz "${name}" removida`)
    },
    onError: (err: Error, name) => {
      toast.error(`Erro ao remover "${name}"`, { description: err.message })
    },
  })

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="text-2xl font-semibold tracking-tight mb-6">Vozes</h1>

      {isLoading ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : !speakers?.length ? (
        <div className="flex flex-col items-center gap-3 py-20 text-muted-foreground">
          <Mic className="size-10 opacity-30" />
          <p className="font-medium text-sm">Nenhuma voz cadastrada</p>
          <p className="text-xs text-center max-w-xs leading-relaxed">
            Vozes entram no banco quando você nomeia um falante em uma reunião.
            Abra uma reunião, identifique os participantes na transcrição e elas
            aparecerão aqui automaticamente.
          </p>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Nome</TableHead>
              <TableHead className="text-muted-foreground">Dimensões</TableHead>
              <TableHead className="w-14" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {speakers.map((speaker) => (
              <TableRow key={speaker.name}>
                <TableCell className="font-medium">{speaker.name}</TableCell>
                <TableCell className="text-muted-foreground text-xs font-mono">
                  {speaker.dims.toLocaleString("pt-BR")} dims
                </TableCell>
                <TableCell>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-8 text-muted-foreground hover:text-destructive"
                        disabled={isPending}
                      >
                        <Trash2 className="size-4" />
                        <span className="sr-only">Remover</span>
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>
                          Remover voz "{speaker.name}"?
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                          Remove a voz do banco de vozes. Reuniões existentes não
                          são alteradas — os nomes já atribuídos continuam
                          presentes nas transcrições.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancelar</AlertDialogCancel>
                        <AlertDialogAction
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          onClick={() => removeSpeaker(speaker.name)}
                        >
                          Remover
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
