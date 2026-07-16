import { Link } from "react-router"
import { Button } from "@/components/ui/button"

export default function NotFoundPage() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-start gap-4 p-8">
      <h1 className="text-2xl font-semibold">Página não encontrada</h1>
      <p className="text-muted-foreground text-sm">
        Esse caminho não existe no meet. Volte para a lista de reuniões.
      </p>
      <Button asChild>
        <Link to="/">Reuniões</Link>
      </Button>
    </div>
  )
}
