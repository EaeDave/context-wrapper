import { useState, useEffect } from "react"
import { NavLink, Outlet } from "react-router"
import { Search } from "lucide-react"
import { Toaster } from "@/components/ui/sonner"
import { cn } from "@/lib/utils"
import SearchCommand from "@/components/search-command"

const NAV: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "Reuniões", end: true },
  { to: "/new", label: "Nova reunião" },
  { to: "/speakers", label: "Vozes" },
]

export default function Layout() {
  const [commandOpen, setCommandOpen] = useState(false)

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault()
        setCommandOpen((prev) => !prev)
      }
    }
    window.addEventListener("keydown", handleKey)
    return () => window.removeEventListener("keydown", handleKey)
  }, [])

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4">
          <span className="font-mono text-lg font-semibold tracking-tight text-primary">
            meet
          </span>
          <nav className="flex items-center gap-1">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-primary/10 text-primary"
                      : "text-muted-foreground hover:bg-accent/10 hover:text-foreground",
                  )
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
          {/* Command palette trigger */}
          <button
            onClick={() => setCommandOpen(true)}
            className="ml-auto flex items-center gap-2 rounded-md border border-input bg-muted/40 px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/10 hover:text-foreground"
          >
            <Search className="size-4" />
            <span className="hidden sm:inline">Buscar</span>
            <kbd className="ml-1 hidden rounded border bg-background px-1.5 py-0.5 font-mono text-xs sm:inline">
              ⌘K
            </kbd>
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-8">
        <Outlet />
      </main>
      <Toaster />
      <SearchCommand open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  )
}
