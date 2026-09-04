import { useEffect, useMemo, useRef, useState } from 'react'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import type { QuestDetail } from '../api'
import { useEventStream } from '../useEventStream'

interface ChatBubble {
  kind: 'user' | 'agent' | 'tool'
  text: string
  toolId?: string
}

/** 把事件流重放成对话:user_message → 用户气泡;agent_message_chunk → 助手气泡;
 * tool_call/_update → 独立的工具状态行(按 toolCallId 归并)。 */
function buildBubbles(events: { seq: number; env: any }[]): ChatBubble[] {
  const bubbles: ChatBubble[] = []
  let current: ChatBubble | null = null
  const toolIndex = new Map<string, ChatBubble>()
  for (const { env } of events) {
    if (env.method === '_guildhall/user_message') {
      bubbles.push({ kind: 'user', text: env.params.text })
      current = null
      continue
    }
    if (env.method === '_guildhall/turn_end') {
      current = null
      continue
    }
    const u = env.params?.update
    if (!u) continue
    switch (u.sessionUpdate) {
      case 'agent_message_chunk': {
        const t = u.content?.text ?? ''
        if (!current) {
          current = { kind: 'agent', text: t }
          bubbles.push(current)
        } else {
          current.text += t
        }
        break
      }
      case 'tool_call': {
        const b: ChatBubble = {
          kind: 'tool',
          text: `${u.title ?? u._meta?.claudeCode?.toolName ?? '工具'} · ${u.status ?? 'pending'}`,
          toolId: u.toolCallId,
        }
        toolIndex.set(u.toolCallId, b)
        bubbles.push(b)
        break
      }
      case 'tool_call_update': {
        const b = toolIndex.get(u.toolCallId)
        if (b && u.status && !b.text.endsWith(u.status)) b.text = `${b.text.split(' · ')[0]} · ${u.status}`
        break
      }
      default:
        break // thought/usage/plan/available_commands 等不进对话流
    }
  }
  return bubbles
}

export default function Chat({ questId, onBack, onGotoReview }: { questId: string; onBack: () => void; onGotoReview: (id: string) => void }) {
  const [detail, setDetail] = useState<QuestDetail | null>(null)
  const [input, setInput] = useState('')
  const [questDraft, setQuestDraft] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const events = useEventStream(questId, 'receptionist', true)
  const bubbles = useMemo(() => buildBubbles(events), [events])
  const flowRef = useRef<HTMLDivElement>(null)

  async function refresh() {
    const d = await api.getQuest(questId)
    setDetail(d)
    return d
  }

  useEffect(() => {
    refresh().catch((e) => setError(String(e)))
    const t = setInterval(() => refresh().catch(() => {}), 2000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questId])

  useEffect(() => {
    flowRef.current?.scrollTo({ top: flowRef.current.scrollHeight })
  }, [bubbles.length])

  const state = detail?.state.state
  const drafting = state === 'drafting'

  useEffect(() => {
    document.title = `${questId.slice(-12)} · ${state ? STATE_LABEL[state] : ''} · Guildhall`
  }, [questId, state])

  async function send() {
    if (!input.trim()) return
    setBusy(true)
    setError('')
    try {
      await api.postMessage(questId, input)
      setInput('')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function generate() {
    setBusy(true)
    setError('')
    try {
      const r = await api.generate(questId)
      if (r.ok && r.quest_md) setQuestDraft(r.quest_md)
      else setError(r.reason ?? '生成失败')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function post() {
    // textarea 里显示的可能是 detail.quest_md(未手改过,questDraft 为 null),一并接受
    const text = questDraft ?? detail?.quest_md
    if (!text) return
    setBusy(true)
    setError('')
    try {
      await api.putQuest(questId, text)
      await api.transition(questId, 'posted')
      setQuestDraft(null)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function dispatch() {
    setBusy(true)
    setError('')
    try {
      await api.transition(questId, 'in_progress')
      onGotoReview(questId)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b p-3">
        <button className="text-sm text-blue-600" onClick={onBack}>
          ← 大厅
        </button>
        <span className="font-mono text-xs text-gray-400">{questId}</span>
        {state && (
          <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATE_COLOR[state]}`}>{STATE_LABEL[state]}</span>
        )}
        {detail?.state.error && (
          <span className="flex-1 truncate rounded bg-red-50 px-2 py-1 text-xs text-red-700" title={detail.state.error}>
            ⚠ {detail.state.error}
          </span>
        )}
        <div className="flex-1" />
        {drafting && (
          <button
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={generate}
          >
            生成需求单
          </button>
        )}
        {state === 'posted' && (
          <button
            className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={dispatch}
          >
            派单
          </button>
        )}
        {(state === 'in_progress' || state === 'appraising' || state === 'appraised' || state === 'disputed') && (
          <button className="rounded border px-4 py-2 text-sm" onClick={() => onGotoReview(questId)}>
            去 review →
          </button>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左:消息流 */}
        <div className="flex min-w-0 flex-1 flex-col border-r">
          <div ref={flowRef} className="flex-1 space-y-3 overflow-y-auto p-4">
            {bubbles.map((b, i) =>
              b.kind === 'tool' ? (
                <div key={i} className="flex justify-start">
                  <div className="rounded bg-gray-50 px-2 py-1 font-mono text-xs text-gray-400">🔧 {b.text}</div>
                </div>
              ) : (
                <div key={i} className={`flex ${b.kind === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div
                    className={`max-w-[75%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                      b.kind === 'user' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-900'
                    }`}
                  >
                    {b.text || <span className="text-gray-400">…</span>}
                  </div>
                </div>
              ),
            )}
          </div>
          {drafting && (
            <div className="flex gap-2 border-t p-3">
              <input
                className="flex-1 rounded border p-2 text-sm"
                placeholder="回答前台的问题;说「不知道」也是有效回答"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && send()}
                disabled={busy}
              />
              <button className="rounded bg-blue-600 px-4 text-sm text-white disabled:opacity-50" disabled={busy} onClick={send}>
                发送
              </button>
            </div>
          )}
        </div>

        {/* 右:需求单预览/编辑 */}
        <div className="flex w-[42%] min-w-[360px] flex-col">
          {error && <div className="m-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</div>}
          {questDraft != null ? (
            <div className="flex min-h-0 flex-1 flex-col p-3">
              <h3 className="mb-2 text-sm font-semibold">需求单(可直接手改)</h3>
              <textarea
                className="min-h-0 flex-1 rounded border p-2 font-mono text-xs"
                value={questDraft}
                onChange={(e) => setQuestDraft(e.target.value)}
              />
              <div className="mt-2 flex gap-2">
                <button className="rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={busy} onClick={post}>
                  张贴
                </button>
                <button className="rounded border px-4 py-2 text-sm" onClick={() => setQuestDraft(null)}>
                  丢弃
                </button>
              </div>
            </div>
          ) : detail?.quest_md ? (
            <div className="flex min-h-0 flex-1 flex-col p-3">
              <h3 className="mb-2 text-sm font-semibold">
                quest.md {state === 'drafting' ? '(已生成,可再编辑后张贴)' : '(已张贴)'}
              </h3>
              {state === 'drafting' ? (
                <>
                  <textarea
                    className="min-h-0 flex-1 rounded border p-2 font-mono text-xs"
                    value={questDraft ?? detail.quest_md}
                    onChange={(e) => setQuestDraft(e.target.value)}
                  />
                  <button className="mt-2 rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={busy} onClick={post}>
                    张贴
                  </button>
                </>
              ) : (
                <pre className="min-h-0 flex-1 overflow-auto rounded border bg-gray-50 p-2 font-mono text-xs">{detail.quest_md}</pre>
              )}
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
              {drafting ? '和前台聊清楚后,点「生成需求单」' : '…'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
