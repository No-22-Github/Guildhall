import { useEffect, useMemo, useState } from 'react'
import { api, STATE_COLOR, STATE_LABEL } from '../api'
import type { QuestState, QuestSummary } from '../api'
import { useQuestListStream } from '../useStatusStream'

const ACTIVE: QuestState[] = ['drafting', 'posted', 'in_progress', 'appraising', 'appraised', 'disputed']

export default function QuestList({ onOpen }: { onOpen: (id: string) => void }) {
  const [projects, setProjects] = useState<string[]>([])
  const [newPath, setNewPath] = useState('')
  const [newMsg, setNewMsg] = useState('')
  const [selectedProject, setSelectedProject] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // quest 摘要走 SSE(§2.2),按 project 分组展示;projects 列表仍一次性拉取
  const summaries = useQuestListStream()
  const quests = useMemo(() => {
    const map: Record<string, QuestSummary[]> = {}
    for (const q of summaries) {
      ;(map[q.project] ??= []).push(q)
    }
    return map
  }, [summaries])

  async function refreshProjects() {
    const ps = await api.listProjects()
    setProjects(ps.map((p) => p.path))
    if (!selectedProject && ps.length > 0) setSelectedProject(ps[0].path)
  }

  useEffect(() => {
    document.title = 'Guildhall 公会大厅'
    refreshProjects().catch((e) => setError(String(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function createQuest() {
    if (!selectedProject) return
    setBusy(true)
    setError('')
    try {
      const r = await api.createQuest(selectedProject, newMsg || null)
      onOpen(r.id)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function registerProject() {
    setError('')
    try {
      await api.registerProject(newPath)
      setNewPath('')
      await refreshProjects()
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-6 space-y-6">
      <header>
        <h1 className="text-2xl font-bold">公会大厅</h1>
        <p className="text-sm text-gray-500">把一句模糊的话,变成一张能验收的委托单</p>
      </header>

      {error && <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      <section className="rounded-lg border p-4 space-y-3">
        <h2 className="font-semibold">新建委托</h2>
        <select
          className="w-full rounded border p-2 text-sm"
          value={selectedProject ?? ''}
          onChange={(e) => setSelectedProject(e.target.value)}
        >
          {projects.length === 0 && <option value="">(还没有注册项目)</option>}
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <textarea
          className="w-full rounded border p-2 text-sm"
          rows={2}
          placeholder="想干点什么?就这么模糊,可以。例:上游那个量化的东西更新了,我这边得跟一下"
          value={newMsg}
          onChange={(e) => setNewMsg(e.target.value)}
        />
        <button
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          disabled={busy || !selectedProject}
          onClick={createQuest}
        >
          进前台
        </button>
        <div className="flex gap-2 border-t pt-3">
          <input
            className="flex-1 rounded border p-2 text-sm"
            placeholder="注册新项目:输入仓库绝对路径,如 /Users/you/code/project"
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
          />
          <button className="rounded border px-3 py-2 text-sm" onClick={registerProject}>
            注册项目
          </button>
        </div>
      </section>

      {projects.map((p) => (
        <section key={p} className="rounded-lg border p-4">
          <h2 className="mb-3 font-mono text-sm text-gray-600">{p}</h2>
          {(quests[p] ?? []).length === 0 && <p className="text-sm text-gray-400">这个项目还没有委托单</p>}
          <div className="space-y-2">
            {(quests[p] ?? []).map((q) => (
              <button
                key={q.id}
                onClick={() => onOpen(q.id)}
                className="flex w-full items-center gap-3 rounded border p-3 text-left hover:bg-gray-50"
              >
                <span className="font-mono text-xs text-gray-400">{q.id}</span>
                <span className="flex-1 truncate text-sm font-medium">{q.title || '(未定稿)'}</span>
                <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATE_COLOR[q.state]}`}>
                  {STATE_LABEL[q.state]}
                </span>
                {ACTIVE.includes(q.state) && <span className="h-2 w-2 animate-pulse rounded-full bg-current opacity-40" />}
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
