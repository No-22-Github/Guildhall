import {
  Archive,
  ArrowUpRight,
  Box,
  ChevronDown,
  CircleDot,
  ClipboardCheck,
  FileText,
  Folder,
  LayoutList,
  Plus,
  Search,
  Settings2,
} from 'lucide-react'
import type { QuestSummary } from '../api'
import type { ColorMode } from '../useColorMode'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Input } from './ui/input'
import ColorModePicker from './ColorModePicker'
import { WORK_STATE_LABEL } from '../workbenchLabels'

export type WorkbenchPage =
  | 'new'
  | 'board'
  | 'adventurer'
  | 'appraiser'
  | 'archive'
  | 'settings'
  | 'projects'
  | 'chat'
  | 'review'
const navigation = [
  { page: 'board', label: '全部任务', Icon: LayoutList },
  { page: 'adventurer', label: '执行队列', Icon: CircleDot },
  { page: 'appraiser', label: '验收与审阅', Icon: ClipboardCheck },
  { page: 'archive', label: '已归档', Icon: Archive },
] as const
const projectName = (path: string) =>
  path.split(/[\\/]/).filter(Boolean).at(-1) || path

export function WorkbenchSidebar({
  page,
  questId,
  projects,
  project,
  onProject,
  quests,
  onNavigate,
  onQuest,
  mode,
  onMode,
  busy,
}: {
  page: WorkbenchPage | null
  questId: string
  projects: string[]
  project: string
  onProject: (project: string) => void
  quests: QuestSummary[]
  onNavigate: (page: WorkbenchPage) => void
  onQuest: (quest: QuestSummary) => void
  mode: ColorMode
  onMode: (mode: ColorMode) => void
  busy: boolean
}) {
  const recent = quests
    .filter((q) => !project || q.project === project)
    .slice(0, 7)
  return (
    <aside className="workbench-sidebar" aria-label="工作台导航">
      <div className="workbench-brand">
        <Box size={24} strokeWidth={1.7} />
        <span>Guildhall</span>
        <Badge variant="secondary">工作台</Badge>
      </div>
      <div className="workbench-project-select">
        <Folder size={16} aria-hidden="true" />
        <select
          aria-label="工作台项目"
          value={project}
          onChange={(e) => onProject(e.target.value)}
          disabled={busy}
        >
          <option value="">所有项目</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {projectName(p)}
            </option>
          ))}
        </select>
        <ChevronDown size={14} aria-hidden="true" />
      </div>
      <Button
        variant="outline"
        className="workbench-new"
        disabled={busy}
        onClick={() => onNavigate('new')}
      >
        <Plus />
        新建任务
      </Button>
      <nav className="workbench-navigation" aria-label="任务导航">
        {navigation.map(({ page: target, label, Icon }) => (
          <Button
            key={target}
            variant="ghost"
            disabled={busy}
            aria-current={page === target ? 'page' : undefined}
            onClick={() => onNavigate(target)}
          >
            <Icon />
            {label}
            {target === 'adventurer' && (
              <span className="nav-count">
                {
                  quests.filter(
                    (q) =>
                      (!project || q.project === project) &&
                      q.state === 'in_progress',
                  ).length
                }
              </span>
            )}
            {target === 'appraiser' && (
              <span className="nav-count">
                {
                  quests.filter(
                    (q) =>
                      (!project || q.project === project) &&
                      ['appraised', 'disputed'].includes(q.state),
                  ).length
                }
              </span>
            )}
          </Button>
        ))}
      </nav>
      <div className="workbench-recents">
        <h2>最近任务</h2>
        {recent.map((q) => (
          <Button
            key={q.id}
            variant="ghost"
            title={q.title || q.id}
            disabled={busy}
            aria-current={
              ['chat', 'review'].includes(page || '') && questId === q.id
                ? 'page'
                : undefined
            }
            onClick={() => onQuest(q)}
          >
            <FileText />
            <span>{q.title || '尚未定稿的任务'}</span>
            <i className={`task-dot state-${q.state}`} />
          </Button>
        ))}
        {!recent.length && <p>新建任务，从一个想法开始。</p>}
      </div>
      <div className="workbench-sidebar-footer">
        <Button
          variant="ghost"
          disabled={busy}
          aria-current={page === 'projects' ? 'page' : undefined}
          onClick={() => onNavigate('projects')}
        >
          <Folder />
          项目管理
        </Button>
        <Button
          variant="ghost"
          disabled={busy}
          aria-current={page === 'settings' ? 'page' : undefined}
          onClick={() => onNavigate('settings')}
        >
          <Settings2 />
          设置
        </Button>
        <div className="workbench-appearance">
          <span>显示模式</span>
          <ColorModePicker compact value={mode} onChange={onMode} />
        </div>
      </div>
    </aside>
  )
}

export function WorkbenchTaskList({
  quests,
  search,
  onSearch,
  onQuest,
  onNew,
}: {
  quests: QuestSummary[]
  search: string
  onSearch: (search: string) => void
  onQuest: (quest: QuestSummary) => void
  onNew: () => void
}) {
  return (
    <div className="workbench-task-list">
      <div className="workbench-list-toolbar">
        <div className="workbench-search">
          <Search size={16} />
          <Input
            aria-label="查找任务"
            placeholder="搜索标题、编号或状态…"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
          />
        </div>
        <span>{quests.length} 个任务</span>
      </div>
      <div className="workbench-table-heading">
        <span>任务</span>
        <span>项目</span>
        <span>状态</span>
        <span />
      </div>
      {quests.map((q) => (
        <button
          className="workbench-task-row"
          key={q.id}
          onClick={() => onQuest(q)}
        >
          <span className="workbench-task-title">
            <FileText size={17} />
            <span>
              <strong>{q.title || '尚未定稿的任务'}</strong>
              <small>{q.id}</small>
            </span>
          </span>
          <span className="workbench-task-project" title={q.project}>
            {projectName(q.project)}
          </span>
          <Badge variant="outline" className={`task-status state-${q.state}`}>
            <i className="task-dot" />
            {WORK_STATE_LABEL[q.state]}
          </Badge>
          <ArrowUpRight size={15} />
        </button>
      ))}
      {!quests.length && (
        <div className="workbench-empty">
          <LayoutList size={30} />
          <h3>{search ? '没有匹配的任务' : '这里还没有任务'}</h3>
          <p>
            {search
              ? '试试其他关键词，或调整项目筛选。'
              : '描述你的目标，一起明确范围与验收方式。'}
          </p>
          <Button variant="outline" onClick={onNew}>
            <Plus />
            新建任务
          </Button>
        </div>
      )}
    </div>
  )
}
