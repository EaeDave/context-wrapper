import { Component, type ErrorInfo, type ReactNode } from "react"
import { Button } from "@/components/ui/button"

type Props = { children: ReactNode }
type State = { error: Error | null }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("UI error boundary:", error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto flex max-w-lg flex-col gap-4 p-8">
          <h1 className="text-xl font-semibold">Algo quebrou na interface</h1>
          <p className="text-muted-foreground text-sm break-words">
            {this.state.error.message || String(this.state.error)}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              onClick={() => this.setState({ error: null })}
            >
              Tentar de novo
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                window.location.href = "/"
              }}
            >
              Ir para início
            </Button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
