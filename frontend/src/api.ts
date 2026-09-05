export type QuestState =
  | 'drafting'
  | 'posted'
  | 'in_progress'
  | 'appraising'
  | 'appraised'
  | 'disputed'
  | 'settled'
  | 'failed'
  | 'withdrawn'

export interface StateJson {
  id: string
  state: QuestState
  project: string
  branch: string
  worktree: string
  base_commit: string | null
  delivery_commit?: string
  sessions: Record<string, string | null>
  offsets: Record<string, number>
  integrity: Record<string, { before: string | null; after: string | null; ok: boolean | null }>
  history: { at: string; from: string | null; to: string }[]
  error: string | null
}

export interface AppraisalCheck {
  index: number
  step: string
  result: 'pass' | 'fail'
  evidence: string
  /** 结构上无法满足(委托书的问题,不是实现的问题) */
  unsatisfiable?: boolean
}

export interface Appraisal {
  checks: AppraisalCheck[]
  touched_tests: boolean
  out_of_scope_files: string[]
  summary: string
  invalidated?: boolean
}

export interface QuestDetail {
  quest_md: string | null
  state: StateJson
  appraisal: Appraisal | null
  runtime: Record<string, AgentRuntimeStatus>
}

export interface AgentRuntimeStatus {
  connected: boolean
  busy: boolean
  activity: 'starting' | 'thinking' | 'responding' | 'tool' | 'steering' | 'waiting' | 'error'
  detail: string | null
  active_tool: { id?: string; name: string; title?: string; status?: string } | null
  seconds_since_event: number
  context_used: number | null
  context_size: number | null
  thought_tail: string | null
  error: string | null
}

export interface QuestSummary {
  id: string
  state: QuestState
  project: string
  title: string
  created: string | null
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  let r: Response
  try {
    r = await fetch(url, { ...init, signal: AbortSignal.timeout(120_000) })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'TimeoutError') {
      throw new Error('请求超过 120s 没有响应(后端可能被模型挂起):请重试,或刷新页面后继续')
    }
    throw e
  }
  if (!r.ok) {
    let detail = `${r.status}`
    try {
      const body = await r.json()
      detail = body.detail ?? JSON.stringify(body)
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail)
  }
  return r.json() as Promise<T>
}

export const api = {
  listProjects: () => req<{ path: string }[]>('/api/projects'),
  registerProject: (path: string) =>
    req<{ path: string }>('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    }),
  listQuests: (project: string) => req<QuestSummary[]>(`/api/quests?project=${encodeURIComponent(project)}`),
  createQuest: (project: string, message: string | null) =>
    req<{ id: string }>('/api/quests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project, message }),
    }),
  getQuest: (id: string) => req<QuestDetail>(`/api/quests/${id}`),
  putQuest: (id: string, text: string) =>
    req<{ ok: string }>(`/api/quests/${id}/quest`, { method: 'PUT', body: text }),
  postMessage: (id: string, text: string) =>
    req<{ ok: boolean }>(`/api/quests/${id}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),
  generate: (id: string) =>
    req<{ ok: boolean; started?: boolean; recovered?: boolean; quest_md?: string; reason?: string }>(`/api/quests/${id}/generate`, { method: 'POST' }),
  retryAdventurer: (id: string) =>
    req<{ ok: boolean; state: string }>(`/api/quests/${id}/retry-adventurer`, { method: 'POST' }),
  resumeAppraisal: (id: string) =>
    req<{ ok: boolean; state: string }>(`/api/quests/${id}/resume-appraisal`, { method: 'POST' }),
  transition: (id: string, to: string) =>
    req<{ state: string }>(`/api/quests/${id}/transition`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to }),
    }),
  getDiff: (id: string) => req<{ diff: string; base_commit: string | null }>(`/api/quests/${id}/diff`),
  delivery: (id: string) => req<{target_branch: string; ready: boolean; reason: string | null; files: string[]}>(`/api/quests/${id}/delivery`),
  reappraise: (id: string) => req<{ok: boolean}>(`/api/quests/${id}/reappraise`, {method: 'POST'}),
  getAppraisal: (id: string) => req<Appraisal>(`/api/quests/${id}/appraisal`),
}

export function eventsUrl(id: string, role: string, offset: number): string {
  return `/api/quests/${id}/events/${role}?offset=${offset}`
}

export function statusStreamUrl(id: string): string {
  return `/api/quests/${id}/status/stream`
}

export function questsStreamUrl(): string {
  return '/api/quests/stream'
}

export const STATE_LABEL: Record<QuestState, string> = {
  drafting: '拷问中',
  posted: '待派单',
  in_progress: '冒险中',
  appraising: '鉴定中',
  appraised: '待过目',
  disputed: '有争议',
  settled: '已接受',
  failed: '失败',
  withdrawn: '已放弃',
}

export const STATE_COLOR: Record<QuestState, string> = {
  drafting: 'bg-amber-100 text-amber-800',
  posted: 'bg-blue-100 text-blue-800',
  in_progress: 'bg-green-100 text-green-800',
  appraising: 'bg-purple-100 text-purple-800',
  appraised: 'bg-emerald-100 text-emerald-800',
  disputed: 'bg-red-100 text-red-800',
  settled: 'bg-gray-200 text-gray-700',
  failed: 'bg-gray-200 text-gray-500',
  withdrawn: 'bg-gray-200 text-gray-500',
}
