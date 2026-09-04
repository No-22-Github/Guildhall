import { useEffect, useMemo, useState } from 'react'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import type { Appraisal, QuestDetail } from '../api'
import { useEventStream } from '../useEventStream'

/** 冒险者/鉴定人的事件流摘要:工具调用行 + 最终文本。 */
function RoleDigest({ questId, role }: { questId: string; role: string }) {
  const events = useEventStream(questId, role, true)
  const digest = useMemo(() => {
    const tools: string[] = []
    let text = ''
    for (const { env } of events) {
      const u = env.params?.update
      if (!u) continue
      if (u.sessionUpdate === 'tool_call') tools.push(u.title ?? u._meta?.claudeCode?.toolName ?? 'tool')
      if (u.sessionUpdate === 'agent_message_chunk') text += u.content?.text ?? ''
    }
    return { tools, text }
  }, [events])

  return (
    <div className="rounded border bg-gray-50 p-2 text-xs">
      <div className="mb-1 font-semibold">{role === 'adventurer' ? '冒险者' : '鉴定人'} 实况</div>
      <div className="text-gray-500">
        {digest.tools.length > 0 ? `工具调用:${digest.tools.slice(-8).join(' → ')}` : '(还没有动作)'}
      </div>
      {digest.text && <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-gray-700">{digest.text}</pre>}
    </div>
  )
}

export default function Review({ questId, onBack }: { questId: string; onBack: () => void }) {
  const [detail, setDetail] = useState<QuestDetail | null>(null)
  const [diff, setDiff] = useState('')
  const [appraisal, setAppraisal] = useState<Appraisal | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function refresh() {
    const d = await api.getQuest(questId)
    setDetail(d)
    if (['in_progress', 'appraising', 'appraised', 'disputed', 'settled', 'failed'].includes(d.state.state)) {
      api.getDiff(questId).then((r) => setDiff(r.diff)).catch(() => {})
    }
    if (d.state.state !== 'appraising' || d.appraisal) {
      api.getAppraisal(questId).then(setAppraisal).catch(() => setAppraisal(null))
    }
  }

  useEffect(() => {
    refresh().catch((e) => setError(String(e)))
    const t = setInterval(() => refresh().catch(() => {}), 2000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questId])

  const state = detail?.state.state
  const invalidated = appraisal?.invalidated === true

  useEffect(() => {
    document.title = `${questId.slice(-12)} · ${state ? STATE_LABEL[state] : ''} · Guildhall`
  }, [questId, state])
  const canAccept = state === 'appraised' || state === 'disputed'
  const canAbandon = state === 'disputed'

  async function decide(to: string) {
    setBusy(true)
    setError('')
    try {
      await api.transition(questId, to)
      await refresh()
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
        <div className="flex-1" />
        {state === 'posted' && (
          <button className="rounded bg-green-600 px-4 py-2 text-sm text-white" disabled={busy} onClick={() => decide('in_progress')}>
            派单
          </button>
        )}
        {state === 'in_progress' && (
          <button
            className="rounded border border-amber-400 px-3 py-2 text-xs text-amber-700 disabled:opacity-50"
            disabled={busy}
            title="冒险者轮次结束后信号丢失时的恢复入口:直接推进到验收"
            onClick={() => decide('appraising')}
          >
            推进到验收
          </button>
        )}
        {state === 'appraising' && (
          <button
            className="rounded border border-amber-400 px-3 py-2 text-xs text-amber-700 disabled:opacity-50"
            disabled={busy}
            title="验收流程中断后的恢复入口:重跑验收"
            onClick={async () => {
              setBusy(true)
              setError('')
              try {
                await fetch(`/api/quests/${questId}/reappraise`, { method: 'POST' })
                await refresh()
              } catch (e) {
                setError(String(e))
              } finally {
                setBusy(false)
              }
            }}
          >
            重跑验收
          </button>
        )}
        {canAccept && (
          <button className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy} onClick={() => decide('settled')}>
            接受
          </button>
        )}
        {canAbandon && (
          <button className="rounded bg-red-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy} onClick={() => decide('withdrawn')}>
            放弃
          </button>
        )}
      </header>

      {error && <div className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-700">{error}</div>}

      {detail?.state.error && (
        <div className="border-b bg-red-50 p-3 text-sm text-red-800">
          <span className="font-semibold">⚠ 出入了:</span>
          <span className="whitespace-pre-wrap">{detail.state.error}</span>
        </div>
      )}

      {(state === 'in_progress' || state === 'appraising') && (
        <div className="grid grid-cols-2 gap-2 border-b bg-gray-50 p-2">
          <RoleDigest questId={questId} role="adventurer" />
          <RoleDigest questId={questId} role="appraiser" />
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-3 divide-x">
        {/* 委托书 */}
        <div className="flex min-h-0 flex-col overflow-hidden p-3">
          <h3 className="mb-2 text-sm font-semibold">委托书</h3>
          <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap rounded border bg-gray-50 p-2 font-mono text-xs">
            {detail?.quest_md ?? '(还没有委托书)'}
          </pre>
        </div>

        {/* diff */}
        <div className="flex min-h-0 flex-col overflow-hidden p-3">
          <h3 className="mb-2 text-sm font-semibold">
            git diff {detail?.state.base_commit ? <span className="font-mono text-xs text-gray-400">基点 {detail.state.base_commit.slice(0, 10)}</span> : ''}
          </h3>
          <pre className="min-h-0 flex-1 overflow-auto rounded border bg-gray-50 p-2 font-mono text-xs">
            {diff || '(还没有改动)'}
          </pre>
        </div>

        {/* 验收结果 */}
        <div className="flex min-h-0 flex-col overflow-y-auto p-3">
          <h3 className="mb-2 text-sm font-semibold">验收结果</h3>
          {invalidated && (
            <div className="mb-2 rounded border-2 border-red-500 bg-red-100 p-3 text-sm font-semibold text-red-800">
              鉴定人结束时改动了 worktree 且未还原:本次验收结论已整份作废,下面的勾叉不算数。
            </div>
          )}
          {appraisal?.touched_tests && (
            <div className="mb-2 rounded border-2 border-red-500 bg-red-100 p-3 text-sm font-semibold text-red-800">
              ⛔ 鉴定人发现 diff 里改了测试文件/断言/CI 配置——最常见的作弊路径,即使看起来合理也要人工确认。
            </div>
          )}
          {appraisal ? (
            <>
              <ul className="space-y-2">
                {appraisal.checks.map((c) => {
                  const pass = c.result === 'pass' && !invalidated
                  return (
                    <li key={c.index} className={`rounded border p-2 text-xs ${pass ? 'border-green-300 bg-green-50' : 'border-red-300 bg-red-50'}`}>
                      <div className="font-medium">
                        {pass ? '✓' : '✗'} #{c.index} {c.step}
                      </div>
                      <div className="mt-1 whitespace-pre-wrap text-gray-600">{c.evidence}</div>
                    </li>
                  )
                })}
              </ul>
              {appraisal.out_of_scope_files.length > 0 && (
                <div className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-800">
                  越界文件:{appraisal.out_of_scope_files.join(', ')}
                </div>
              )}
              <div className="mt-3 rounded border bg-white p-2">
                <div className="text-xs font-semibold text-gray-700">summary(有出入必须写在这里,不许省略)</div>
                <p className="mt-1 whitespace-pre-wrap text-xs text-gray-800">{appraisal.summary}</p>
              </div>
            </>
          ) : (
            <p className="text-sm text-gray-400">
              {state === 'appraising' ? '鉴定人正在逐条走验收步骤…' : '还没有验收结论'}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
