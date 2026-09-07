import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, STATE_LABEL } from '../api'
import { useEventStream } from '../useEventStream'
import { useQuestStatusStream } from '../useStatusStream'
import ReceptionScene from '../components/ReceptionScene'
import QuestDocument from '../components/QuestDocument'
import '../reception.css'
import AgentStatusPanel, { displayToolName, ACTIVITY_LABEL } from '../components/AgentStatusPanel'

interface UserItem {
  kind: 'user'
  text: string
  system?: boolean
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

interface ToolGroupItem {
  kind: 'toolGroup'
  tools: ToolItem[]
}

interface DividerItem {
  kind: 'divider'
}

interface ActionItem {
  kind: 'action'
  text: string
  status: 'running' | 'success' | 'error'
}

type TimelineItem = UserItem | AgentItem | ToolGroupItem | DividerItem | ActionItem

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

/** 将 ACP 事件恢复为“文本段 → 工具组 → 分割线 → 后续文本段”；连续工具收进同一组以便折叠，并保留工具参数与结果。 */
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
      items.push({ kind: 'user', text: env.params?.text ?? '', system: Boolean(env.params?.system) })
      currentAgent = null
      agentSpoke = false
      dividerBeforeNextAgent = false
      continue
    }
    if (env.method === '_guildhall/quest_md_updated') {
      currentAgent = null
      items.push({ kind: 'action', text: '需求单已写入 quest.md（右侧可查看、修改并张贴）', status: 'success' })
      continue
    }
    if (env.method === '_guildhall/quest_md_rejected') {
      currentAgent = null
      items.push({ kind: 'action', text: `需求单校验未通过：${env.params?.reason ?? '未知原因'}`, status: 'error' })
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
          const last = items[items.length - 1]
          if (last && last.kind === 'toolGroup') last.tools.push(tool)
          else items.push({ kind: 'toolGroup', tools: [tool] })
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

function isToolTerminal(tool: ToolItem): boolean {
  return tool.status === 'completed' || tool.status === 'failed' || tool.status === 'cancelled'
}

function ToolCard({ tool }: { tool: ToolItem }) {
  const running = !isToolTerminal(tool)
  const statusLabel = tool.status === 'completed' ? '完成' : tool.status === 'failed' ? '失败' : tool.status === 'cancelled' ? '已取消' : '运行中'
  const input = tool.input as Record<string, unknown> | undefined
  const command = typeof input?.command === 'string' ? input.command : null
  const file = typeof input?.file_path === 'string' ? input.file_path : typeof input?.path === 'string' ? input.path : null
  const state = running ? 'is-running' : tool.status === 'failed' ? 'is-failed' : tool.status === 'cancelled' ? 'is-cancelled' : ''
  return (
    <div className="flex justify-start">
      <details className={`tool-card ${state}`}>
        <summary>
          <span className="tool-dot" />
          <span className="tool-name">{displayToolName(tool.name)}</span>
          <span className="tool-title">{tool.title}</span>
          <span className="tool-status">{statusLabel}</span>
        </summary>
        <div className="tool-body">
          {command && <ToolSection title="执行命令" text={command} />}
          {file && !command && <ToolSection title="文件" text={file} />}
          {input && <ToolSection title="参数" text={JSON.stringify(input, null, 2)} />}
          {tool.output && <ToolSection title="输出" text={tool.output} />}
          {!input && !tool.output && <p className="tool-waiting">等待 Claude Code 返回工具详情…</p>}
        </div>
      </details>
    </div>
  )
}

function toolSummary(tools: ToolItem[]) {
  const counts = new Map<string, number>()
  for (const tool of tools) {
    const label = ({ Read: '读取文件', ReadFile: '读取文件', Grep: '搜索', Glob: '查找文件', Bash: '执行命令', Terminal: '执行命令', Edit: '编辑文件', Write: '写入文件' } as Record<string, string>)[tool.name] ?? displayToolName(tool.name)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts].map(([name, count]) => `${name} ${count} 次`).join(' · ')
}

function ToolGroup({ group }: { group: ToolGroupItem }) {
  const running = group.tools.filter(tool => !isToolTerminal(tool))
  const failed = group.tools.filter(tool => tool.status === 'failed').length
  const cancelled = group.tools.filter(tool => tool.status === 'cancelled').length
  const completed = group.tools.filter(tool => tool.status === 'completed').length
  return (
    <details className={`work-record ${running.length ? 'is-running' : ''} ${failed ? 'is-failed' : ''}`}>
      <summary>
        <span className="work-indicator" aria-hidden="true">{failed ? '!' : running.length ? '◌' : cancelled ? '−' : '✓'}</span>
        <span className="work-summary" title={running.length ? running.map(t => t.title).join(' · ') : toolSummary(group.tools)}>
          {running.length ? `${running.length > 1 ? `正在执行 ${running.length} 项` : running[0].title} · 已完成 ${completed} 项` : toolSummary(group.tools)}
        </span>
        {failed > 0 && <span className="work-failure">{failed} 项失败</span>}
        {cancelled > 0 && <span>{cancelled} 项取消</span>}
        <span className="work-disclosure">详情</span>
      </summary>
      <div className="work-items">{group.tools.map(tool => <ToolCard key={tool.toolId} tool={tool} />)}</div>
    </details>
  )
}

function ToolSection({ title, text }: { title: string; text: string }) {
  const [copied, setCopied] = useState('')
  return <section>
    <div className="tool-section-title">{title}<button onClick={async () => {
      try { await navigator.clipboard.writeText(text); setCopied('已复制') }
      catch { setCopied('复制失败，请选择文本复制') }
    }}>{copied || '复制'}</button></div>
    <pre tabIndex={0}>{text}</pre>
  </section>
}

function MarkdownMessage({ text }: { text: string }) {
  return <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>
}

export default function Chat({ questId, onBack, onGotoReview, onDirty, project, sceneFilter }: { sceneFilter?: string; project?: string; onDirty?: (dirty: boolean) => void; questId: string; onBack: () => void; onGotoReview: (id: string) => void }) {
  const detail = useQuestStatusStream(questId)
  const [input, setInput] = useState('')
  const [showQuest, setShowQuest] = useState(false)
  const [focusQuest, setFocusQuest] = useState(false)
  const [editing, setEditing] = useState(false)
  const [savedQuest, setSavedQuest] = useState<string | null>(null)
  const [split, setSplit] = useState(() => {
    try { const n = Number(localStorage.getItem('guildhall.chatSplit')); return n >= 35 && n <= 65 ? n : 45 } catch { return 45 }
  })
  const columnsRef = useRef<HTMLDivElement>(null)
  const readingAnchor = useRef<{ element: Element; offset: number; atBottom: boolean } | null>(null)
  const layoutChanging = useRef(false)
  function rememberReading() {
    const flow = flowRef.current
    if (!flow || !flow.clientHeight) return
    const top = flow.getBoundingClientRect().top
    const candidates = [...flow.querySelectorAll('.markdown-body p, .markdown-body li, .markdown-body pre, .markdown-body h1, .markdown-body h2, .markdown-body h3, .user-message, .work-record')]
    const visible = candidates.filter(el => el.getBoundingClientRect().bottom > top)
    const element = visible.find(el => el.getBoundingClientRect().top >= top) ?? visible[0]
    if (element) readingAnchor.current = { element, offset: element.getBoundingClientRect().top - top, atBottom: followingLatest }
    layoutChanging.current = true
  }
  function toggleQuest() { rememberReading(); setShowQuest(v => !v); setFocusQuest(false) }
  useLayoutEffect(() => {
    const flow = flowRef.current
    const anchor = readingAnchor.current
    if (flow && flow.clientHeight && anchor) {
      if (anchor.atBottom) flow.scrollTop = flow.scrollHeight
      else flow.scrollTop += anchor.element.getBoundingClientRect().top - flow.getBoundingClientRect().top - anchor.offset
    }
    const frame = requestAnimationFrame(() => { layoutChanging.current = false })
    return () => cancelAnimationFrame(frame)
  }, [showQuest, focusQuest, split])
  useEffect(() => { try { localStorage.setItem('guildhall.chatSplit', String(split)) } catch { /* optional preference */ } }, [split])
  const [questDraft, setQuestDraft] = useState<string | null>(null)
  useEffect(() => { onDirty?.(Boolean(input.trim()) || (questDraft !== null && questDraft !== (savedQuest ?? detail?.quest_md))); return () => onDirty?.(false) }, [input, questDraft, detail?.quest_md, savedQuest, onDirty])
  const [error, setError] = useState('')
  const [actionBusy, setActionBusy] = useState(false)
  const [generationRequested, setGenerationRequested] = useState(false)
  const [followingLatest, setFollowingLatest] = useState(true)
  const events = useEventStream(questId, 'receptionist', true)
  const timeline = useMemo(() => buildTimeline(events), [events])
  const flowRef = useRef<HTMLDivElement>(null)
  const lastReadEventCountRef = useRef(0)

  const completedTools = useMemo(() => timeline.flatMap(item => item.kind === 'toolGroup'
    ? item.tools.filter(tool => tool.status === 'completed').map(tool => tool.name) : []), [timeline])

  // A successful local response bridges SSE latency, then live server updates own the document again.
  useEffect(() => { if (savedQuest !== null && detail?.quest_md === savedQuest) setSavedQuest(null) }, [detail?.quest_md, savedQuest])

  useEffect(() => {
    if (!detail || !generationRequested) return
    if (detail.quest_md) {
      setSavedQuest(detail.quest_md)
      setGenerationRequested(false)
    } else if (detail.state.error && !detail.runtime?.receptionist?.busy) {
      setGenerationRequested(false)
      setError(detail.state.error)
    }
  }, [detail, generationRequested])

  useEffect(() => {
    if (!followingLatest || layoutChanging.current || focusQuest) return
    const flow = flowRef.current
    if (flow) flow.scrollTo({ top: flow.scrollHeight })
    lastReadEventCountRef.current = events.length
  }, [events.length, followingLatest, focusQuest])

  function handleFlowScroll() {
    const flow = flowRef.current
    if (!flow || layoutChanging.current) return
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
      else if (result.quest_md) setSavedQuest(result.quest_md)
      else setGenerationRequested(true)
    } catch (e) {
      setError(String(e))
    } finally {
      setActionBusy(false)
    }
  }

  async function post() {
    const text = questDraft ?? savedQuest ?? detail?.quest_md
    if (!text) return
    setActionBusy(true)
    setError('')
    try {
      await api.putQuest(questId, text)
      setSavedQuest(text)
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

  async function saveDocument() {
    const text = questDraft ?? savedQuest ?? detail?.quest_md
    if (!text) return
    setActionBusy(true); setError('')
    try { await api.putQuest(questId, text); setSavedQuest(text); setQuestDraft(null); setEditing(false) }
    catch (e) { setError(String(e)) }
    finally { setActionBusy(false) }
  }
  // Keep the drawer mounted so scroll positions and editor selection survive hiding.
  const documentText = questDraft ?? savedQuest ?? detail?.quest_md ?? ''
  const documentDirty = questDraft !== null && questDraft !== (savedQuest ?? detail?.quest_md)

  const hasQuestDocument = Boolean(documentText)
  const hasQuestPanel = hasQuestDocument && showQuest

  return (
    <div className="chat-workspace flex h-screen flex-col">
      <header className="chat-toolbar flex items-center gap-3 border-b p-3">
        <button className="text-sm text-blue-600" onClick={onBack}>← 大厅</button>
        <div className="chat-heading"><strong>前台 · 委托洽谈</strong><span title={project || detail?.state.project}>{project || detail?.state.project}</span></div>
        {state && <span className="chat-state">{STATE_LABEL[state]}</span>}
        {detail?.state.error && <span className="flex-1 truncate rounded bg-red-50 px-2 py-1 text-xs text-red-700" title={detail.state.error}>⚠ {detail.state.error}</span>}
        <div className="flex-1" />
        {hasQuestDocument && <button className="rounded border px-3 py-2 text-sm" onClick={toggleQuest}>{showQuest ? '收起需求单 →' : '查看需求单'}</button>}
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

      <div ref={columnsRef} style={{ '--chat-split': `${split}%` } as CSSProperties} className={`chat-columns flex min-h-0 flex-1 ${hasQuestPanel ? 'has-quest' : ''} ${hasQuestPanel && focusQuest ? 'document-focused' : ''}`}>

        <div className="chat-column flex min-w-0 flex-1 flex-col">
          {!hasQuestPanel && error && (
            <div className="m-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</div>
          )}
          <details className="chat-runtime"><summary><span className={agentStatus?.busy ? 'runtime-dot working' : 'runtime-dot'} />{agentStatus ? ACTIVITY_LABEL[agentStatus.activity] : '会话未连接'}{agentStatus?.active_tool && <span> · {agentStatus.active_tool.title || agentStatus.active_tool.name}</span>}{agentStatus?.busy && agentStatus.seconds_since_event >= 15 && <span> · {Math.round(agentStatus.seconds_since_event)} 秒无新事件</span>}<small>运行详情</small></summary><div><AgentStatusPanel roleName="前台" status={agentStatus} completedTools={completedTools} /></div></details>
          <div className="relative min-h-0 flex-1">
            <div ref={flowRef} className="chat-transcript h-full overflow-y-auto" onScroll={handleFlowScroll}>
              {!timeline.length && <div className="chat-empty"><span>前台</span><h2>有什么想交给公会？</h2><p>从一个想法开始。我们一起把目标、范围和验收方式说清楚。</p></div>}
              {timeline.map((item, index) => {
                if (item.kind === 'toolGroup') return <ToolGroup key={`tools-${item.tools[0].toolId}`} group={item} />
                if (item.kind === 'divider') return null
                if (item.kind === 'action') {
                  return <div key={`action-${index}`} className={`rounded border px-3 py-2 text-xs ${item.status === 'error' ? 'border-red-200 bg-red-50 text-red-700' : item.status === 'success' ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-blue-200 bg-blue-50 text-blue-700'}`}>{item.text}</div>
                }
                return (
                  <div key={`${item.kind}-${index}`} className={`flex ${item.kind === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <div className={`chat-message ${item.kind === 'user' ? (item.system ? 'whitespace-pre-wrap border border-amber-200 bg-amber-50 text-amber-800' : 'user-message whitespace-pre-wrap bg-blue-600 text-white') : 'agent-message text-gray-900'}`}>
                      {item.kind === 'agent' ? <><span className="speaker-label">前台</span><MarkdownMessage text={item.text} /></> : item.system ? <><span className="mr-1 rounded bg-amber-100 px-1 text-[10px] font-medium">系统转达</span>{item.text}</> : item.text}
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
          {hasQuestDocument && !showQuest && <button className="document-ready" onClick={toggleQuest}>需求单已就绪 <span>查看与编辑 →</span></button>}
          {drafting && (
            <div className="chat-composer border-t">
              <div className="flex items-end gap-2">
                <textarea
                  className="max-h-48 flex-1 resize-y rounded border p-2 text-sm"
                  rows={2}
                  aria-label="对话输入" placeholder="补充你的想法，工作中也可以继续发送…"
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
              <div className="mt-1 text-[11px] text-gray-400">Enter 发送 · Shift + Enter 换行 · 工作中也可补充说明</div>
            </div>
          )}
        </div>

        <ReceptionScene filter={sceneFilter} busy={Boolean(agentStatus?.busy)} hidden={hasQuestPanel} />
        <div role="separator" tabIndex={hasQuestPanel && !focusQuest ? 0 : -1} aria-label="调整对话与需求单宽度" aria-orientation="vertical" aria-valuemin={35} aria-valuemax={65} aria-valuenow={split} className="document-resizer" hidden={!hasQuestPanel || focusQuest}
          onKeyDown={event => {
            if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
              event.preventDefault(); rememberReading()
              setSplit(v => event.key === 'Home' ? 35 : event.key === 'End' ? 65 : Math.max(35, Math.min(65, v + (event.key === 'ArrowLeft' ? -2 : 2))))
            }
          }}
          onPointerDown={event => { rememberReading(); event.currentTarget.setPointerCapture(event.pointerId) }}
          onPointerMove={event => {
            if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
            const rect = columnsRef.current?.getBoundingClientRect()
            if (rect) { rememberReading(); setSplit(Math.max(35, Math.min(65, (event.clientX - rect.left) / rect.width * 100))) }
          }}
          onPointerUp={event => { event.currentTarget.releasePointerCapture(event.pointerId) }}
        ><span /></div>
        <QuestDocument text={documentText} visible={hasQuestPanel} editing={editing} dirty={documentDirty} drafting={drafting} busy={actionBusy} focused={focusQuest} error={error}
          onEdit={() => setEditing(v => !v)} onChange={setQuestDraft} onSave={saveDocument} onPost={post}
          onFocus={() => { rememberReading(); setFocusQuest(v => !v) }} onClose={toggleQuest} />
      </div>
    </div>
  )
}
