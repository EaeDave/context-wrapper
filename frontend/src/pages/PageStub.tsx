interface PageStubProps {
  name: string
}

export default function PageStub({ name }: PageStubProps) {
  return (
    <div className="flex items-center justify-center py-24">
      <div className="rounded-lg border border-border bg-card p-8 text-center shadow-sm">
        <p className="font-mono text-sm text-muted-foreground">
          página: <span className="text-primary">{name}</span>
        </p>
        <p className="mt-2 text-xs text-muted-foreground">em construção</p>
      </div>
    </div>
  )
}
