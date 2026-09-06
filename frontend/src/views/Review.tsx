import { useEffect, useMemo, useState } from 'react'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import type { Appraisal, QuestDetail } from '../api'
import { useEventStream } from '../useEventStream'
import { useQuestStatusStream } from '../useStatusStream'
import AgentStatusPanel from '../components/AgentStatusPanel'

/** 冒险者/鉴定人的事件流摘要:三段状态面板(§2.3.2) + 最终文本。 */
function RoleDigest({ questId, role, status }: { questId: string; role: string; status?: QuestDetail['runtime'][string] }) {
  // failed 页初次挂载时角色可能尚未恢复；connected 翻转后必须重建 SSE，
  // 否则旧连接只重放失败前的几条事件，后续工具/文本永远进不了页面。
  const events = useEventStream(questId, role, true, status?.connected ? 'connected' : 'offline')
  const digest = useMemo(() => {
    const seen = new Map<string, string>()
    const tools: string[] = []
    let text = ''
    for (const { env } of events) {
      const u = env.params?.update
      if (!u) continue
      if (u.sessionUpdate === 'tool_call' || u.sessionUpdate === 'tool_call_update') {
        const id = u.toolCallId ?? ''
        if (!seen.has(id)) {
          const name = u._meta?.claudeCode?.toolName ?? u.title ?? 'tool'
          seen.set(id, name)
          tools.push(name)
        }
      }
      if (u.sessionUpdate === 'agent_message_chunk') text += u.content?.text ?? ''
    }
    return { tools, text }
  }, [events])

  const roleName = role === 'adventurer' ? '冒险者' : '鉴定人'

  return (
    <div className="rounded border bg-gray-50 p-2 text-xs">
      <AgentStatusPanel roleName={roleName} status={status} completedTools={digest.tools} right={<span>事件 {events.length.toLocaleString()}</span>} />
      {digest.text && <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-gray-700">{digest.text}</pre>}
    </div>
  )
}

export default function Review({ questId, onBack }: { questId: string; onBack: () => void }) {
  const detail = useQuestStatusStream(questId)
  const [diff, setDiff] = useState('')
  const [appraisal, setAppraisal] = useState<Appraisal | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmDelivery, setConfirmDelivery] = useState(false)
  const [delivery, setDelivery] = useState<Awaited<ReturnType<typeof api.delivery>> | null>(null)

  const state = detail?.state.state
  const invalidated = appraisal?.invalidated === true

  // diff / appraisal 不走状态流:状态流告知状态,内容仍按需拉取
  useEffect(() => {
    if (state && ['in_progress', 'appraising', 'appraised', 'disputed', 'settled', 'failed'].includes(state)) {
      api.getDiff(questId).then((r) => setDiff(r.diff)).catch(() => {})
    }
  }, [questId, state])

  useEffect(() => {
    if (state && ['appraised', 'disputed', 'settled'].includes(state)) {
      api.getAppraisal(questId).then(setAppraisal).catch(() => setAppraisal(null))
    } else {
      setAppraisal(null)
    }
  }, [questId, state])

  useEffect(() => {
    document.title = `${questId.slice(-12)} · ${state ? STATE_LABEL[state] : ''} · Guildhall`
  }, [questId, state])
  useEffect(() => {
    if (state && ['appraised', 'disputed'].includes(state)) {
      api.delivery(questId).then(setDelivery).catch((e) => setError(String(e)))
    }
  }, [questId, state])
  const canAccept = state === 'appraised' || state === 'disputed'
  const canAbandon = state === 'disputed'

  async function decide(to: string) {
    setBusy(true)
    setError('')
    try {
      await api.transition(questId, to)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function retryAdventurer() {
    setBusy(true)
    setError('')
    try {
      await api.retryAdventurer(questId)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function resumeAppraisal() {
    setBusy(true)
    setError('')
    try {
      await api.resumeAppraisal(questId)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="chat-toolbar flex items-center gap-3 border-b p-3">
        <button className="text-sm text-blue-600" onClick={onBack}>
          ← 大厅
        </button>
        <div className="chat-heading">
          <strong>验收台 · 执行与鉴定</strong>
          <span title={questId}>{questId}</span>
        </div>
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
        {state === 'failed' && detail?.state.error?.startsWith('adventurer') && (
          <>
            <button
              className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              disabled={busy}
              title="冒险者已有完整输出并正常退出时，不重复执行，直接启动鉴定人"
              onClick={() => void resumeAppraisal()}
            >
              冒险者已完成，开始验收
            </button>
            <button
              className="rounded bg-amber-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              disabled={busy}
              title="复用现有 worktree，重新建立 Claude Code 会话并继续执行"
              onClick={() => void retryAdventurer()}
            >
              重试冒险者
            </button>
          </>
        )}
        {state && ['appraising', 'appraised', 'disputed'].includes(state) && (
          <button
            className="rounded border border-amber-400 px-3 py-2 text-xs text-amber-700 disabled:opacity-50"
            disabled={busy}
            title="验收流程中断后的恢复入口:重跑验收"
            onClick={async () => {
              setBusy(true)
              setError('')
              try {
                await api.reappraise(questId)
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
          <button className="rounded bg-green-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy || !delivery?.ready} onClick={() => setConfirmDelivery(true)}>
            接受并合并
          </button>
        )}
        {canAbandon && (
          <button className="rounded bg-red-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy} onClick={() => decide('withdrawn')}>
            放弃
          </button>
        )}
      </header>

      {confirmDelivery && canAccept && delivery?.ready && <div className="border-b bg-amber-50 p-3 text-sm">
        {state === 'disputed' && <span>本单有争议，请确认已阅读全部验收证据。 </span>}
        <button disabled={busy} className="rounded bg-green-700 px-3 py-2 text-white disabled:opacity-50" onClick={() => { setConfirmDelivery(false); void decide('settled') }}>确认合并至 {delivery.target_branch}</button>
        <button className="ml-3" onClick={() => setConfirmDelivery(false)}>取消</button>
      </div>}
      {delivery && canAccept && <div className="border-b bg-blue-50 p-3 text-sm">交付目标：{delivery.target_branch} · {delivery.files.length} 个文件{delivery.reason && <span className="ml-2 text-red-700">{delivery.reason}</span>}</div>}
      {detail?.state.delivery_commit && <div className="border-b p-3 text-sm">交付提交：{detail.state.delivery_commit}</div>}
      {error && <div className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-700">{error}</div>}

      {detail?.state.error && (
        <div className="border-b bg-red-50 p-3 text-sm text-red-800">
          <span className="font-semibold">⚠ 出入了:</span>
          <span className="whitespace-pre-wrap">{detail.state.error}</span>
        </div>
      )}

      {(state === 'in_progress' || state === 'appraising' || state === 'failed') && (
        <div className="grid grid-cols-2 gap-2 border-b bg-gray-50 p-2">
          <RoleDigest questId={questId} role="adventurer" status={detail?.runtime?.adventurer} />
          <RoleDigest questId={questId} role="appraiser" status={detail?.runtime?.appraiser} />
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
              ⚠ 测试或 CI 有变更，请检查是否新增覆盖、削弱断言或修改检查条件。此提示与逐项验收结果分别展示。
            </div>
          )}
          {appraisal ? (
            <>
              <ul className="space-y-2">
                {appraisal.checks.map((c) => {
                  const pass = c.result === 'pass' && !invalidated
                  const unsatisfiable = c.unsatisfiable === true && !pass
                  return (
                    <li
                      key={c.index}
                      className={`rounded border p-2 text-xs ${
                        pass
                          ? 'border-green-300 bg-green-50'
                          : unsatisfiable
                            ? 'border-amber-400 bg-amber-50'
                            : 'border-red-300 bg-red-50'
                      }`}
                    >
                      <div className="font-medium">
                        {pass ? '✓' : unsatisfiable ? '⚠' : '✗'} #{c.index} {c.step}
                      </div>
                      {unsatisfiable && (
                        <div className="mt-1 font-medium text-amber-700">结构上无法满足:问题出在委托书,不是实现。</div>
                      )}
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
