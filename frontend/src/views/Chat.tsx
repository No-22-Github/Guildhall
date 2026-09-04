import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import type { AgentRuntimeStatus, QuestDetail } from '../api'
import { useEventStream } from '../useEventStream'

interface UserItem {
  kind: 'user'
  text: string
}

interface AgentItem {
  kind: 'agent'
  text: string
  messageId?: string
}

interface ToolItem {
  kind: 'tool'
  toolId: string
  name: string
  title: string
  status: string
  input?: unknown
  output?: string
}

interface DividerItem {
  kind: 'divider'
}

interface ActionItem {
  kind: 'action'
  text: string
  status: 'running' | 'success' | 'error'
}

type TimelineItem = UserItem | AgentItem | ToolItem | DividerItem | ActionItem

function contentText(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(contentText).filter(Boolean).join('\n')
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    if (typeof record.text === 'string') return record.text
    if ('content' in record) return contentText(record.content)
  }
  return ''
}

function hasValues(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && Object.keys(value as Record<string, unknown>).length)
}

function displayToolName(name: string): string {
  return ({ Bash: 'Terminal', Read: 'ReadFile', Write: 'WriteFile', Edit: 'EditFile' } as Record<string, string>)[name] ?? name
}

/** 将 ACP 事件恢复为“文本段 → 工具 → 分割线 → 后续文本段”，并保留工具参数与结果。 */
function buildTimeline(events: { seq: number; env: any }[]): TimelineItem[] {
  const items: TimelineItem[] = []
  const tools = new Map<string, ToolItem>()
  let currentAgent: AgentItem | null = null
  let generation = false
  let generationAction: ActionItem | null = null
  let agentSpoke = false
  let dividerBeforeNextAgent = false

  for (const { env } of events) {
    if (env.method === '_guildhall/user_message') {
      items.push({ kind: 'user', text: env.params?.text ?? '' })
      currentAgent = null
      agentSpoke = false
      dividerBeforeNextAgent = false
      continue
    }
    if (env.method === '_guildhall/generation_start') {
      generation = true
      currentAgent = null
      generationAction = { kind: 'action', text: '正在生成并写入 quest.md…', status: 'running' }
      items.push(generationAction)
      continue
    }
    if (env.method === '_guildhall/generation_end') {
      generation = false
      currentAgent = null
      if (generationAction) {
        generationAction.status = env.params?.ok ? 'success' : 'error'
        generationAction.text = env.params?.ok
          ? '需求单已生成并写入 quest.md'
          : `需求单生成失败：${env.params?.reason ?? '未知错误'}`
      }
      generationAction = null
      continue
    }
    if (env.method === '_guildhall/turn_start' || env.method === '_guildhall/turn_end' || env.method === '_guildhall/turn_error') {
      currentAgent = null
      continue
    }

    const update = env.params?.update
    if (!update) continue
    switch (update.sessionUpdate) {
      case 'agent_message_chunk': {
        if (generation) break
        const text = update.content?.text ?? ''
        if (!text) break
        const messageId = update.messageId
        if (!currentAgent || (messageId && currentAgent.messageId && messageId !== currentAgent.messageId)) {
          if (dividerBeforeNextAgent) items.push({ kind: 'divider' })
          currentAgent = { kind: 'agent', text: '', messageId }
          items.push(currentAgent)
          dividerBeforeNextAgent = false
        }
        currentAgent.text += text
        agentSpoke = true
        break
      }
      case 'tool_call':
      case 'tool_call_update': {
        const id = update.toolCallId ?? `tool-${items.length}`
        let tool = tools.get(id)
        if (!tool) {
          const claude = update._meta?.claudeCode ?? {}
          tool = {
            kind: 'tool',
            toolId: id,
            name: claude.toolName ?? update.title ?? '工具',
            title: claude.title ?? update.rawInput?.description ?? update.title ?? '工具调用',
            status: update.status ?? 'pending',
          }
          tools.set(id, tool)
          items.push(tool)
        }
        const claude = update._meta?.claudeCode ?? {}
        if (claude.toolName) tool.name = claude.toolName
        if (claude.title || update.rawInput?.description) tool.title = claude.title ?? update.rawInput.description
        else if (update.title && update.title !== 'Terminal') tool.title = update.title
        if (update.status) tool.status = update.status
        if (hasValues(update.rawInput)) tool.input = update.rawInput
        const toolResponse = claude.toolResponse
        const responseText = toolResponse ? [toolResponse.stdout, toolResponse.stderr].filter(Boolean).join('\n') : ''
        const output = update.rawOutput || responseText || contentText(update.content)
        if (output) tool.output = output
        currentAgent = null
        dividerBeforeNextAgent = agentSpoke
        break
      }
      default:
        break
    }
  }
  return items
}

const ACTIVITY_LABEL: Record<AgentRuntimeStatus['activity'], string> = {
  starting: '正在启动 Claude Code',
  thinking: '正在思考',
  responding: '正在回复',
  tool: '正在使用工具',
  steering: '正在处理你的补充',
  waiting: '等待你的消息',
  error: '运行异常',
}

function AgentStatus({ status }: { status?: AgentRuntimeStatus }) {
  if (!status) return <span className="text-xs text-gray-400">Claude Code 会话未连接，发送消息时会重建</span>
  const busy = status.busy
  const label = status.active_tool
    ? `正在运行 ${displayToolName(status.active_tool.name)}${status.active_tool.title ? `：${status.active_tool.title}` : ''}`
    : ACTIVITY_LABEL[status.activity] ?? '状态未知'
  const quiet = busy && status.seconds_since_event >= 60
    ? ` · 已 ${Math.round(status.seconds_since_event)} 秒无新事件，可能卡住`
    : busy && status.seconds_since_event >= 15
      ? ` · ${Math.round(status.seconds_since_event)} 秒无新事件`
      : ''
  const context = status.context_size && status.context_used != null
    ? ` · 上下文 ${Math.round((status.context_used / status.context_size) * 100)}%`
    : ''
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-gray-600" title={status.error ?? status.detail ?? undefined}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${status.activity === 'error' ? 'bg-red-500' : busy ? 'animate-pulse bg-emerald-500' : 'bg-gray-300'}`} />
      <span className="truncate">{label}{status.detail && !status.active_tool ? ` · ${status.detail}` : ''}{quiet}{context}</span>
    </div>
  )
}

function ToolCard({ tool }: { tool: ToolItem }) {
  const running = tool.status !== 'completed' && tool.status !== 'failed' && tool.status !== 'cancelled'
  const statusLabel = tool.status === 'completed' ? '完成' : tool.status === 'failed' ? '失败' : tool.status === 'cancelled' ? '已取消' : '运行中'
  const input = tool.input as Record<string, unknown> | undefined
  const command = typeof input?.command === 'string' ? input.command : null
  const file = typeof input?.file_path === 'string' ? input.file_path : typeof input?.path === 'string' ? input.path : null
  return (
    <div className="flex justify-start">
      <details className="tool-card max-w-[88%]" open={running}>
        <summary>
          <span className={`tool-dot ${running ? 'animate-pulse bg-amber-500' : tool.status === 'failed' ? 'bg-red-500' : 'bg-emerald-500'}`} />
          <span className="font-semibold">{displayToolName(tool.name)}</span>
          <span className="min-w-0 flex-1 truncate text-gray-500">{tool.title}</span>
          <span className="text-gray-400">{statusLabel}</span>
        </summary>
        <div className="space-y-2 border-t border-gray-200 p-3">
          {command && <ToolSection title="执行命令" text={command} />}
          {file && !command && <ToolSection title="文件" text={file} />}
          {input && !command && <ToolSection title="参数" text={JSON.stringify(input, null, 2)} />}
          {tool.output && <ToolSection title="输出" text={tool.output} />}
          {!input && !tool.output && <p className="text-xs text-gray-400">等待 Claude Code 返回工具详情…</p>}
        </div>
      </details>
    </div>
  )
}

function ToolSection({ title, text }: { title: string; text: string }) {
  return (
    <section>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">{title}</div>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-950 p-2 text-xs text-gray-100">{text}</pre>
    </section>
  )
}

function MarkdownMessage({ text }: { text: string }) {
  return <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>
}

export default function Chat({ questId, onBack, onGotoReview }: { questId: string; onBack: () => void; onGotoReview: (id: string) => void }) {
  const [detail, setDetail] = useState<QuestDetail | null>(null)
  const [input, setInput] = useState('')
  const [questDraft, setQuestDraft] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [actionBusy, setActionBusy] = useState(false)
  const [generationRequested, setGenerationRequested] = useState(false)
  const [followingLatest, setFollowingLatest] = useState(true)
  const events = useEventStream(questId, 'receptionist', true)
  const timeline = useMemo(() => buildTimeline(events), [events])
  const flowRef = useRef<HTMLDivElement>(null)
  const lastReadEventCountRef = useRef(0)

  async function refresh() {
    const result = await api.getQuest(questId)
    setDetail(result)
    if (result.quest_md && generationRequested) {
      setQuestDraft(result.quest_md)
      setGenerationRequested(false)
    } else if (generationRequested && result.state.error && !result.runtime?.receptionist?.busy) {
      setGenerationRequested(false)
      setError(result.state.error)
    }
    return result
  }

  useEffect(() => {
    refresh().catch((e) => setError(String(e)))
    const timer = setInterval(() => refresh().catch(() => {}), 2000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questId, generationRequested])

  useEffect(() => {
    if (!followingLatest) return
    const flow = flowRef.current
    if (flow) flow.scrollTo({ top: flow.scrollHeight })
    lastReadEventCountRef.current = events.length
  }, [events.length, followingLatest])

  function handleFlowScroll() {
    const flow = flowRef.current
    if (!flow) return
    const nearBottom = flow.scrollHeight - flow.scrollTop - flow.clientHeight < 64
    if (!nearBottom && followingLatest) lastReadEventCountRef.current = events.length
    if (nearBottom) lastReadEventCountRef.current = events.length
    setFollowingLatest(nearBottom)
  }

  function scrollToLatest() {
    const flow = flowRef.current
    if (!flow) return
    lastReadEventCountRef.current = events.length
    setFollowingLatest(true)
    flow.scrollTo({ top: flow.scrollHeight, behavior: 'smooth' })
  }

  const hasUnreadEvents = !followingLatest && events.length > lastReadEventCountRef.current

  const state = detail?.state.state
  const drafting = state === 'drafting'
  const agentStatus = detail?.runtime?.receptionist

  useEffect(() => {
    document.title = `${questId.slice(-12)} · ${state ? STATE_LABEL[state] : ''} · Guildhall`
  }, [questId, state])

  async function send() {
    const text = input.trim()
    if (!text) return
    setInput('')
    setError('')
    try {
      await api.postMessage(questId, text)
      await refresh()
    } catch (e) {
      setInput((current) => (current ? `${text}\n${current}` : text))
      setError(String(e))
    }
  }

  async function generate() {
    setActionBusy(true)
    setError('')
    try {
      const result = await api.generate(questId)
      if (!result.ok) setError(result.reason ?? '生成失败')
      else if (result.quest_md) setQuestDraft(result.quest_md)
      else setGenerationRequested(true)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setActionBusy(false)
    }
  }

  async function post() {
    const text = questDraft ?? detail?.quest_md
    if (!text) return
    setActionBusy(true)
    setError('')
    try {
      await api.putQuest(questId, text)
      await api.transition(questId, 'posted')
      setQuestDraft(null)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setActionBusy(false)
    }
  }

  async function dispatch() {
    setActionBusy(true)
    setError('')
    try {
      await api.transition(questId, 'in_progress')
      onGotoReview(questId)
    } catch (e) {
      setError(String(e))
    } finally {
      setActionBusy(false)
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b p-3">
        <button className="text-sm text-blue-600" onClick={onBack}>← 大厅</button>
        <span className="font-mono text-xs text-gray-400">{questId}</span>
        {state && <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATE_COLOR[state]}`}>{STATE_LABEL[state]}</span>}
        {detail?.state.error && <span className="flex-1 truncate rounded bg-red-50 px-2 py-1 text-xs text-red-700" title={detail.state.error}>⚠ {detail.state.error}</span>}
        <div className="flex-1" />
        {drafting && (
          <button
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={actionBusy || generationRequested}
            onClick={generate}
            title={agentStatus?.busy ? '会先尝试收取已经完整输出的需求单；若仍在生成则提示稍后再试' : undefined}
          >{generationRequested ? '生成中…' : '生成需求单'}</button>
        )}
        {state === 'posted' && <button className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={actionBusy} onClick={dispatch}>派单</button>}
        {(state === 'in_progress' || state === 'appraising' || state === 'appraised' || state === 'disputed' || state === 'failed') && (
          <button className="rounded border px-4 py-2 text-sm" onClick={() => onGotoReview(questId)}>去 review →</button>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col border-r">
          <div className="border-b bg-gray-50 px-4 py-2"><AgentStatus status={agentStatus} /></div>
          <div className="relative min-h-0 flex-1">
            <div ref={flowRef} className="h-full space-y-3 overflow-y-auto p-4" onScroll={handleFlowScroll}>
              {timeline.map((item, index) => {
                if (item.kind === 'tool') return <ToolCard key={`tool-${item.toolId}`} tool={item} />
                if (item.kind === 'divider') return <div key={`divider-${index}`} className="reply-divider"><span>工具执行后继续回复</span></div>
                if (item.kind === 'action') {
                  return <div key={`action-${index}`} className={`rounded border px-3 py-2 text-xs ${item.status === 'error' ? 'border-red-200 bg-red-50 text-red-700' : item.status === 'success' ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-blue-200 bg-blue-50 text-blue-700'}`}>{item.text}</div>
                }
                return (
                  <div key={`${item.kind}-${index}`} className={`flex ${item.kind === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <div className={`max-w-[78%] rounded-lg px-3 py-2 text-sm ${item.kind === 'user' ? 'whitespace-pre-wrap bg-blue-600 text-white' : 'bg-gray-100 text-gray-900'}`}>
                      {item.kind === 'agent' ? <MarkdownMessage text={item.text} /> : item.text}
                    </div>
                  </div>
                )
              })}
            </div>
            {!followingLatest && (
              <button
                className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-md hover:bg-gray-50"
                onClick={scrollToLatest}
              >
                {hasUnreadEvents ? '↓ 有新消息，回到底部' : '↓ 回到底部'}
              </button>
            )}
          </div>
          {drafting && (
            <div className="border-t p-3">
              <div className="flex items-end gap-2">
                <textarea
                  className="max-h-48 min-h-20 flex-1 resize-y rounded border p-2 text-sm"
                  rows={3}
                  placeholder="回答前台的问题；Agent 工作时也可以直接插话"
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                      event.preventDefault()
                      void send()
                    }
                  }}
                />
                <button className="h-10 rounded bg-blue-600 px-4 text-sm text-white disabled:opacity-50" disabled={!input.trim()} onClick={() => void send()}>发送</button>
              </div>
              <div className="mt-1 text-[11px] text-gray-400">Enter 发送 · Shift + Enter 换行 · 工作中发送会直接注入当前 Claude Code turn</div>
            </div>
          )}
        </div>

        <div className="flex w-[42%] min-w-[360px] flex-col">
          {error && <div className="m-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</div>}
          {questDraft != null ? (
            <div className="flex min-h-0 flex-1 flex-col p-3">
              <h3 className="mb-2 text-sm font-semibold">需求单（已写入 quest.md，可直接手改）</h3>
              <textarea className="min-h-0 flex-1 rounded border p-2 font-mono text-xs" value={questDraft} onChange={(event) => setQuestDraft(event.target.value)} />
              <div className="mt-2 flex gap-2">
                <button className="rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={actionBusy} onClick={post}>张贴</button>
                <button className="rounded border px-4 py-2 text-sm" onClick={() => setQuestDraft(null)}>收起编辑</button>
              </div>
            </div>
          ) : detail?.quest_md ? (
            <div className="flex min-h-0 flex-1 flex-col p-3">
              <h3 className="mb-2 text-sm font-semibold">quest.md {state === 'drafting' ? '（已落盘，可编辑后张贴）' : '（已张贴）'}</h3>
              {state === 'drafting' ? (
                <>
                  <textarea className="min-h-0 flex-1 rounded border p-2 font-mono text-xs" value={detail.quest_md} onChange={(event) => setQuestDraft(event.target.value)} />
                  <button className="mt-2 rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={actionBusy} onClick={post}>张贴</button>
                </>
              ) : <pre className="min-h-0 flex-1 overflow-auto rounded border bg-gray-50 p-2 font-mono text-xs">{detail.quest_md}</pre>}
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
              {generationRequested ? 'Claude Code 正在生成，完成后会自动写入 quest.md…' : drafting ? '和前台聊清楚后，点「生成需求单」' : '…'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
