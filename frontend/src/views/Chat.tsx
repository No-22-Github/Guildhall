import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import { useEventStream } from '../useEventStream'
import { useQuestStatusStream } from '../useStatusStream'
import AgentStatusPanel, { displayToolName } from '../components/AgentStatusPanel'

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

export default function Chat({ questId, onBack, onGotoReview, onDirty }: { onDirty?: (dirty: boolean) => void; questId: string; onBack: () => void; onGotoReview: (id: string) => void }) {
  const detail = useQuestStatusStream(questId)
  const [input, setInput] = useState('')
  const [questDraft, setQuestDraft] = useState<string | null>(null)
  useEffect(() => { onDirty?.(Boolean(input.trim()) || (questDraft !== null && questDraft !== detail?.quest_md)); return () => onDirty?.(false) }, [input, questDraft, detail?.quest_md, onDirty])
  const [error, setError] = useState('')
  const [actionBusy, setActionBusy] = useState(false)
  const [generationRequested, setGenerationRequested] = useState(false)
  const [followingLatest, setFollowingLatest] = useState(true)
  const events = useEventStream(questId, 'receptionist', true)
  const timeline = useMemo(() => buildTimeline(events), [events])
  const flowRef = useRef<HTMLDivElement>(null)
  const lastReadEventCountRef = useRef(0)

  // 历史工具链(A → B → C)与运行中工具分开;运行中的那个由面板从 status 呈现
  const completedTools = useMemo(() => {
    const seen = new Map<string, string>()
    const out: string[] = []
    for (const { env } of events) {
      const u = env.params?.update
      if (!u || (u.sessionUpdate !== 'tool_call' && u.sessionUpdate !== 'tool_call_update')) continue
      const id = u.toolCallId ?? ''
      if (seen.has(id)) continue
      const name = u._meta?.claudeCode?.toolName ?? u.title ?? '工具'
      seen.set(id, name)
      out.push(name)
    }
    return out
  }, [events])

  useEffect(() => {
    if (!detail || !generationRequested) return
    if (detail.quest_md) {
      setQuestDraft(detail.quest_md)
      setGenerationRequested(false)
    } else if (detail.state.error && !detail.runtime?.receptionist?.busy) {
      setGenerationRequested(false)
      setError(detail.state.error)
    }
  }, [detail, generationRequested])

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

  // §2.4:需求单生成前只渲染对话栏并居中;生成后切成两栏。
  const hasQuestPanel = questDraft != null || Boolean(detail?.quest_md)

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b p-3">
        <button className="text-sm text-blue-600" onClick={onBack}>← 大厅</button>
        <span className="font-mono text-xs text-gray-400">{questId}</span>
        {state && <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATE_COLOR[state]}`}>{STATE_LABEL[state]}</span>}
        {detail?.state.error && <span className="flex-1 truncate rounded bg-red-50 px-2 py-1 text-xs text-red-700" title={detail.state.error}>⚠ {detail.state.error}</span>}
        <div className="flex-1" />
        {drafting && <button className="rounded border px-3 py-2 text-sm" disabled={actionBusy} onClick={async () => { if (!window.confirm('放弃这张草稿委托？')) return; setActionBusy(true); try { await api.transition(questId, 'withdrawn'); setQuestDraft(null); setInput('') } catch (e) { setError(String(e)) } finally { setActionBusy(false) } }}>放弃草稿</button>}
        {drafting && (
          <button
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={actionBusy || generationRequested}
            onClick={generate}
            title={agentStatus?.busy ? '会先尝试收取已经完整输出的需求单；若仍在生成则提示稍后再试' : undefined}
          >{generationRequested ? '生成中…' : '生成需求单'}</button>
        )}
        {state === 'posted' && <button className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={actionBusy} onClick={dispatch}>派单</button>}
        {(state && state !== 'drafting' && state !== 'posted') && (
          <button className="rounded border px-4 py-2 text-sm" onClick={() => onGotoReview(questId)}>去 review →</button>
        )}
      </header>

      <div className={hasQuestPanel ? 'flex min-h-0 flex-1' : 'flex min-h-0 flex-1 justify-center'}>
        <div className={hasQuestPanel ? 'flex min-w-0 flex-1 flex-col border-r' : 'flex w-full max-w-3xl flex-col'}>
          {!hasQuestPanel && error && (
            <div className="m-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</div>
          )}
          <div className="border-b bg-gray-50 px-4 py-2"><AgentStatusPanel roleName="前台" status={agentStatus} completedTools={completedTools} /></div>
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

        {hasQuestPanel && (
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
            ) : (
              <div className="flex min-h-0 flex-1 flex-col p-3">
                <h3 className="mb-2 text-sm font-semibold">quest.md {state === 'drafting' ? '（已落盘，可编辑后张贴）' : '（已张贴）'}</h3>
                {state === 'drafting' ? (
                  <>
                    <textarea className="min-h-0 flex-1 rounded border p-2 font-mono text-xs" value={detail?.quest_md ?? ''} onChange={(event) => setQuestDraft(event.target.value)} />
                    <button className="mt-2 rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={actionBusy} onClick={post}>张贴</button>
                  </>
                ) : <pre className="min-h-0 flex-1 overflow-auto rounded border bg-gray-50 p-2 font-mono text-xs">{detail?.quest_md}</pre>}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
