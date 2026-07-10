import { useEffect, useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Bot, Cpu, ExternalLink, Key, SlidersHorizontal } from "lucide-react"

import * as api from "@/lib/api"
import type { SettingsInfo } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const SOURCE_LABEL: Record<string, string> = {
  local: "UI",
  config: "config.toml",
  env: "variável de ambiente",
}

const PROVIDERS = [
  { value: "claude-code", label: "Claude Code (via claude CLI)" },
  { value: "anthropic", label: "Anthropic (API / OAuth)" },
  { value: "openai", label: "OpenAI" },
  { value: "ollama", label: "Ollama (local)" },
]

export default function SettingsPage() {
  const queryClient = useQueryClient()

  const { data: settings, isLoading } = useQuery<SettingsInfo>({
    queryKey: ["settings"],
    queryFn: api.getSettings,
  })

  // ── Hugging Face ──────────────────────────────────────────────────────────
  const [hfInput, setHfInput] = useState("")

  const saveHf = useMutation({
    mutationFn: () => api.setHfToken(hfInput.trim()),
    onSuccess: () => {
      toast.success("Token Hugging Face salvo")
      setHfInput("")
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Erro ao salvar token", { description: err.message }),
  })

  const deleteHf = useMutation({
    mutationFn: api.deleteHfToken,
    onSuccess: () => {
      toast.success("Token Hugging Face removido")
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Não foi possível remover", { description: err.message }),
  })

  // ── Anthropic OAuth ───────────────────────────────────────────────────────
  const [wizardOpen, setWizardOpen] = useState(false)
  const [wizardStep, setWizardStep] = useState<1 | 2>(1)
  const [oauthState, setOauthState] = useState<string | null>(null)
  const [codeInput, setCodeInput] = useState("")

  const authorize = useMutation({
    mutationFn: api.anthropicAuthorize,
    onSuccess: ({ url, state }) => {
      window.open(url, "_blank", "noopener,noreferrer")
      setOauthState(state)
      setWizardStep(2)
    },
    onError: (err: Error) =>
      toast.error("Erro ao iniciar autorização", { description: err.message }),
  })

  const exchange = useMutation({
    mutationFn: () =>
      api.anthropicExchange(codeInput.trim(), oauthState ?? ""),
    onSuccess: (data) => {
      toast.success("Conta Claude conectada", {
        description: data.email ? `Conectado como ${data.email}` : undefined,
      })
      setWizardOpen(false)
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Falha na conexão", { description: err.message }),
  })

  const disconnect = useMutation({
    mutationFn: api.anthropicDisconnect,
    onSuccess: () => {
      toast.success("Conta Claude desconectada")
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Erro ao desconectar", { description: err.message }),
  })

  function openWizard() {
    setWizardStep(1)
    setOauthState(null)
    setCodeInput("")
    setWizardOpen(true)
  }

  // ── LLM Provider ──────────────────────────────────────────────────────────
  const [llmProvider, setLlmProvider] = useState("")
  const [llmModel, setLlmModel] = useState("")
  const [llmInitialized, setLlmInitialized] = useState(false)

  useEffect(() => {
    if (settings && !llmInitialized) {
      setLlmProvider(settings.llm.provider)
      setLlmModel(settings.llm.model)
      setLlmInitialized(true)
    }
  }, [settings, llmInitialized])

  const saveLlm = useMutation({
    mutationFn: () => api.setLlm(llmProvider, llmModel),
    onSuccess: () => {
      toast.success("Configuração de LLM salva")
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Erro ao salvar LLM", { description: err.message }),
  })

  // ── Transcrição & diarização (tuning) ────────────────────────────────────
  const [whisperModel, setWhisperModel] = useState("")
  const [language, setLanguage] = useState("")
  const [simThreshold, setSimThreshold] = useState(0.5)
  const [device, setDevice] = useState("")
  const [computeType, setComputeType] = useState("")
  const [tuningInitialized, setTuningInitialized] = useState(false)

  useEffect(() => {
    if (settings?.tuning && !tuningInitialized) {
      setWhisperModel(settings.tuning.whisper_model)
      setLanguage(settings.tuning.language)
      setSimThreshold(settings.tuning.similarity_threshold)
      setDevice(settings.tuning.device)
      setComputeType(settings.tuning.compute_type)
      setTuningInitialized(true)
    }
  }, [settings, tuningInitialized])

  const saveTuning = useMutation({
    mutationFn: () =>
      api.setTuning({
        whisper_model: whisperModel,
        language,
        similarity_threshold: simThreshold,
        device,
        compute_type: computeType,
      }),
    onSuccess: () => {
      toast.success("Configurações de transcrição salvas")
      queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (err: Error) =>
      toast.error("Erro ao salvar", { description: err.message }),
  })

  // ── Render ────────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-8 space-y-6">
        <h1 className="text-2xl font-semibold tracking-tight">Configurações</h1>
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-40 rounded-lg border animate-pulse bg-muted/40"
            />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-8 space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Configurações</h1>

      {/* ── Hugging Face ──────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <Key className="size-4" />
            Hugging Face
          </CardTitle>
          <CardDescription>
            Token para diarização (pyannote). Necessário para separação de vozes.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {settings?.hf_token.configured && (
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2 min-w-0">
                <code className="font-mono text-sm truncate">
                  {settings.hf_token.masked}
                </code>
                <Badge variant="outline" className="shrink-0 text-xs">
                  {SOURCE_LABEL[settings.hf_token.source ?? ""] ??
                    settings.hf_token.source}
                </Badge>
              </div>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={deleteHf.isPending}
                  >
                    Remover
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Remover token Hugging Face?</AlertDialogTitle>
                    <AlertDialogDescription>
                      O token será removido das configurações locais. A diarização
                      ficará indisponível até que um novo token seja fornecido.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancelar</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() => deleteHf.mutate()}
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    >
                      Remover
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </div>
          )}

          <div className="flex gap-2">
            <Input
              type="password"
              placeholder={
                settings?.hf_token.configured
                  ? "Novo token (substitui atual)"
                  : "hf_…"
              }
              value={hfInput}
              onChange={(e) => setHfInput(e.target.value)}
              className="flex-1"
            />
            <Button
              onClick={() => saveHf.mutate()}
              disabled={!hfInput.trim() || saveHf.isPending}
            >
              Salvar
            </Button>
          </div>

          <a
            href="https://huggingface.co/settings/tokens"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <ExternalLink className="size-3" />
            Obter token em huggingface.co
          </a>
        </CardContent>
      </Card>

      {/* ── Claude / Anthropic ────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <Bot className="size-4" />
            Claude (Anthropic)
          </CardTitle>
          <CardDescription>
            Autenticação via assinatura Claude Pro/Max — sem necessidade de chave de API.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {settings?.anthropic.connected ? (
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2 flex-wrap">
                <Badge className="bg-green-600 text-white hover:bg-green-700">
                  conectado
                </Badge>
                {settings.anthropic.email && (
                  <span className="text-sm text-muted-foreground">
                    {settings.anthropic.email}
                  </span>
                )}
              </div>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={disconnect.isPending}
                  >
                    Desconectar
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Desconectar conta Claude?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Os tokens OAuth serão removidos. Você precisará reconectar
                      para continuar usando o Claude via assinatura.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancelar</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() => disconnect.mutate()}
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    >
                      Desconectar
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </div>
          ) : (
            <Button onClick={openWizard}>
              Conectar com Claude Pro/Max
            </Button>
          )}

          {settings?.anthropic.api_key_configured && (
            <p className="text-xs text-muted-foreground">
              Chave de API configurada — usada como fallback quando OAuth não está ativo.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Anthropic OAuth wizard dialog */}
      <Dialog open={wizardOpen} onOpenChange={setWizardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Conectar com Claude Pro/Max</DialogTitle>
            <DialogDescription>
              {wizardStep === 1
                ? "Passo 1 de 2 — Abra o claude.ai para autorizar o acesso."
                : "Passo 2 de 2 — Cole o código exibido pela página do claude.ai."}
            </DialogDescription>
          </DialogHeader>

          {wizardStep === 1 ? (
            <div className="space-y-4">
              <p className="text-sm text-muted-foreground">
                Ao clicar no botão abaixo, o claude.ai será aberto em nova aba.
                Após autorizar, a página exibirá um <strong>código</strong> — copie-o
                e volte aqui para colar no próximo passo.
              </p>
              <DialogFooter>
                <Button
                  onClick={() => authorize.mutate()}
                  disabled={authorize.isPending}
                >
                  {authorize.isPending ? "Aguarde…" : "Abrir claude.ai"}
                </Button>
              </DialogFooter>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="oauth-code">Código de autorização</Label>
                <Input
                  id="oauth-code"
                  placeholder="Cole o código aqui"
                  value={codeInput}
                  onChange={(e) => setCodeInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && codeInput.trim() && !exchange.isPending)
                      exchange.mutate()
                  }}
                  autoFocus
                />
              </div>
              <p className="text-xs text-muted-foreground">
                Usa sua assinatura Pro/Max. Ao conectar, o provider LLM muda para{" "}
                <strong>anthropic</strong> automaticamente.
              </p>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => setWizardStep(1)}
                  disabled={exchange.isPending}
                >
                  Voltar
                </Button>
                <Button
                  onClick={() => exchange.mutate()}
                  disabled={!codeInput.trim() || exchange.isPending}
                >
                  {exchange.isPending ? "Conectando…" : "Conectar"}
                </Button>
              </DialogFooter>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* ── LLM Provider ──────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <Cpu className="size-4" />
            Provider LLM
          </CardTitle>
          <CardDescription>
            Modelo de linguagem usado para resumos e análise das reuniões.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3">
            <div className="space-y-1.5">
              <Label>Provider</Label>
              <Select value={llmProvider} onValueChange={setLlmProvider}>
                <SelectTrigger>
                  <SelectValue placeholder="Selecionar provider" />
                </SelectTrigger>
                <SelectContent>
                  {PROVIDERS.map(({ value, label }) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Modelo</Label>
              <Input
                placeholder={
                  llmProvider
                    ? `padrão do ${llmProvider}`
                    : "padrão do provider"
                }
                value={llmModel}
                onChange={(e) => setLlmModel(e.target.value)}
              />
            </div>
          </div>
          <Button
            onClick={() => saveLlm.mutate()}
            disabled={!llmProvider || saveLlm.isPending}
          >
            Salvar
          </Button>
        </CardContent>
      </Card>

      {/* ── Transcrição & diarização ──────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <SlidersHorizontal className="size-4" />
            Transcrição &amp; diarização
          </CardTitle>
          <CardDescription>
            Parâmetros do pipeline de transcrição e reconhecimento de voz.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label>Modelo Whisper</Label>
              <Select value={whisperModel} onValueChange={setWhisperModel}>
                <SelectTrigger>
                  <SelectValue placeholder="Selecionar modelo" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="large-v3">large-v3</SelectItem>
                  <SelectItem value="turbo">turbo</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="language-input">Idioma</Label>
              <Input
                id="language-input"
                placeholder="pt"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Similaridade mínima de voz</Label>
              <span className="text-sm font-mono tabular-nums text-muted-foreground">
                {simThreshold.toFixed(2)}
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={simThreshold}
              onChange={(e) => setSimThreshold(Number(e.target.value))}
              className="w-full accent-primary h-1.5 rounded-lg cursor-pointer"
            />
            <p className="text-xs text-muted-foreground">
              Cosseno mínimo para reconhecer uma voz conhecida. Valores menores
              aceitam correspondências mais distantes.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label>Dispositivo</Label>
              <Select value={device} onValueChange={setDevice}>
                <SelectTrigger>
                  <SelectValue placeholder="Selecionar" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cuda">cuda (GPU)</SelectItem>
                  <SelectItem value="cpu">cpu</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="compute-type-input">Tipo de computação</Label>
              <Input
                id="compute-type-input"
                placeholder="float16"
                value={computeType}
                onChange={(e) => setComputeType(e.target.value)}
              />
            </div>
          </div>

          <Button
            onClick={() => saveTuning.mutate()}
            disabled={!whisperModel || !device || saveTuning.isPending}
          >
            {saveTuning.isPending ? "Salvando…" : "Salvar"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
