import { ClipboardList } from "lucide-react"
import type { ActionItem } from "@/lib/types"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

const PRIORITY_VARIANT = {
  alta: "destructive",
  media: "outline",
  baixa: "secondary",
} as const satisfies Record<ActionItem["priority"], "destructive" | "outline" | "secondary">

interface ActionItemsProps {
  items: ActionItem[]
}

export function ActionItems({ items }: ActionItemsProps) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <ClipboardList className="size-4" />
          Action items
        </CardTitle>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-6">O quê</TableHead>
              <TableHead>Onde</TableHead>
              <TableHead>Pedido por</TableHead>
              <TableHead className="pr-6">Prioridade</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item, i) => (
              <TableRow key={i}>
                <TableCell className="pl-6">
                  <span className="font-medium">{item.what}</span>
                  {item.details && (
                    <p className="mt-0.5 text-xs text-muted-foreground">{item.details}</p>
                  )}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {item.where ?? "—"}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {item.requested_by ?? "—"}
                </TableCell>
                <TableCell className="pr-6">
                  <Badge
                    variant={PRIORITY_VARIANT[item.priority]}
                    className={cn(
                      item.priority === "media" &&
                        "border-amber-500 text-amber-600 dark:border-amber-400 dark:text-amber-400"
                    )}
                  >
                    {item.priority}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
