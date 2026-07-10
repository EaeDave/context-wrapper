import { useState, useEffect, useMemo } from "react"
import { useNavigate } from "react-router"
import { useQuery } from "@tanstack/react-query"
import { Plus, Mic, Search } from "lucide-react"

import { Dialog, DialogContent } from "@/components/ui/dialog"
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandSeparator,
} from "@/components/ui/command"
import { Badge } from "@/components/ui/badge"
import * as api from "@/lib/api"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export default function SearchCommand({ open, onOpenChange }: Props) {
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const navigate = useNavigate()

  // Reset query when dialog closes
  useEffect(() => {
    if (!open) {
      setQuery("")
      setDebouncedQuery("")
    }
  }, [open])

  // Debounce 250ms
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(t)
  }, [query])

  const { data: results } = useQuery({
    queryKey: ["search", debouncedQuery],
    queryFn: () => api.search(debouncedQuery),
    enabled: debouncedQuery.trim().length > 0,
  })

  // Unique meetings from results
  const meetingHits = useMemo(() => {
    if (!results) return []
    const seen = new Set<number>()
    return results.filter((r) => {
      if (seen.has(r.meeting_id)) return false
      seen.add(r.meeting_id)
      return true
    })
  }, [results])

  const snippets = results ?? []

  function go(path: string) {
    onOpenChange(false)
    navigate(path)
  }

  const isSearching = debouncedQuery.trim().length > 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-hidden p-0 shadow-lg sm:max-w-xl">
        <Command shouldFilter={false} className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground">
          <CommandInput
            placeholder="Buscar reuniões ou digitar comando..."
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            <CommandEmpty>Nenhum resultado.</CommandEmpty>

            {/* Static shortcuts */}
            <CommandGroup heading="Ações">
              <CommandItem onSelect={() => go("/new")}>
                <Plus className="size-4 mr-2 shrink-0" />
                Nova reunião
              </CommandItem>
              <CommandItem onSelect={() => go("/speakers")}>
                <Mic className="size-4 mr-2 shrink-0" />
                Vozes
              </CommandItem>
            </CommandGroup>

            {/* Search results */}
            {isSearching && meetingHits.length > 0 && (
              <>
                <CommandSeparator />
                <CommandGroup heading="Reuniões">
                  {meetingHits.map((r) => (
                    <CommandItem
                      key={r.meeting_id}
                      value={String(r.meeting_id)}
                      onSelect={() => go(`/meetings/${r.meeting_id}`)}
                    >
                      <Search className="size-4 mr-2 shrink-0 text-muted-foreground" />
                      <span className="flex-1 truncate">{r.title}</span>
                      <span className="ml-2 text-xs text-muted-foreground shrink-0">
                        {r.date}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}

            {isSearching && snippets.length > 0 && (
              <>
                <CommandSeparator />
                <CommandGroup heading="Trechos">
                  {snippets.map((r, i) => (
                    <CommandItem
                      key={`${r.meeting_id}-${i}`}
                      value={`snippet-${r.meeting_id}-${i}`}
                      onSelect={() => go(`/meetings/${r.meeting_id}`)}
                    >
                      <Badge variant="outline" className="mr-2 shrink-0 text-xs">
                        {r.kind === "action_item" ? "action item" : "transcrição"}
                      </Badge>
                      <span
                        className="flex-1 truncate text-sm [&_mark]:bg-primary/20 [&_mark]:text-primary [&_mark]:rounded [&_mark]:px-0.5"
                        dangerouslySetInnerHTML={{ __html: r.snippet }}
                      />
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
