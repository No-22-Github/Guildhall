import { useEffect, useRef, useState, type CSSProperties } from 'react'
import Chat from './views/Chat'
import Review from './views/Review'
import Settings from './views/Settings'
import { api, STATE_LABEL, type QuestSummary } from './api'
import { useQuestListStream } from './useStatusStream'
import { getSkin, type StationId } from './skins'
import TavernScene from './components/TavernScene'
import './guildhall.css'
import './workbench.css'
import { WorkbenchSidebar, WorkbenchTaskList } from './components/Workbench'
import { WORK_STATE_LABEL } from './workbenchLabels'
import { useColorMode } from './useColorMode'
import { Textarea } from './components/ui/textarea'

type Panel =
  | 'new'
  | 'board'
  | 'adventurer'
  | 'appraiser'
  | 'archive'
  | 'settings'
  | 'projects'
  | 'chat'
  | 'review'
const terminal = new Set(['settled', 'withdrawn'])
function pref(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}
function savePref(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable */
  }
}
export default function App() {
  const quests = useQuestListStream()
  const [projects, setProjects] = useState<string[]>([])
  // Project selection belongs to reception; other desks have their own filter.
  const [project, setProject] = useState('')
  const [projectFilter, setProjectFilter] = useState('')
  const [questProject, setQuestProject] = useState('')
  const [panel, setPanel] = useState<Panel | null>(() =>
    getSkin(pref('guildhall.skin', 'workbench')).layout === 'workbench'
      ? 'board'
      : null,
  )
  const [questId, setQuestId] = useState('')
  const [skinId, setSkinId] = useState(
    () => getSkin(pref('guildhall.skin', 'workbench')).id,
  )
  const [motion, setMotion] = useState(
    () => pref('guildhall.reducedMotion', 'false') === 'true',
  )
  const [message, setMessage] = useState('')
  const [path, setPath] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')
  const [chatDirty, setChatDirty] = useState(false)
  const [settingsDirty, setSettingsDirty] = useState(false)
  const [pendingNavigation, setPendingNavigation] = useState<{
    panel: Panel | null
    quest?: QuestSummary
  } | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const skin = getSkin(skinId)
  const workbench = skin.layout === 'workbench'
  const { mode, changeMode, resolved } = useColorMode()
  const labels = workbench ? WORK_STATE_LABEL : STATE_LABEL
  const active = quests.filter((q) => q.state === 'in_progress')
  const review = quests.filter(
    (q) => q.state === 'appraised' || q.state === 'disputed',
  )
  function receiveProjects(result: { path: string }[]) {
    const paths = result.map((p) => p.path)
    setProjects(paths)
    setProject((old) =>
      paths.includes(old)
        ? old
        : paths.includes(pref('guildhall.project', ''))
          ? pref('guildhall.project', '')
          : (paths[0] ?? ''),
    )
  }
  async function refreshProjects() {
    receiveProjects(await api.listProjects())
  }
  useEffect(() => {
    api
      .listProjects()
      .then(receiveProjects)
      .catch((e) => setError(String(e)))
  }, [])
  useEffect(() => {
    // Keep the same DOM subtree when switching layouts so unsaved forms survive.
    const element = dialog.current
    element?.close()
    if (panel) {
      if (workbench) element?.show()
      else element?.showModal()
    } else {
      document.title = workbench ? 'Guildhall · 工作台' : 'Guildhall · 公会大厅'
    }
  }, [panel, workbench])
  function navigate(next: Panel | null, quest?: QuestSummary) {
    setPendingNavigation(null)
    setSearch('')
    if (!workbench) setProjectFilter('')
    setError('')
    if (quest) {
      setQuestId(quest.id)
      setQuestProject(quest.project)
    }
    setPanel(next)
  }
  function open(next: Panel | null, quest?: QuestSummary) {
    if (busy) return
    if (panel === next && (!quest || questId === quest.id)) return
    if (
      (settingsDirty && panel === 'settings') ||
      (chatDirty && panel === 'chat')
    ) {
      setPendingNavigation({ panel: next, quest })
      return
    }
    navigate(next, quest)
  }
  function close() {
    open(workbench ? 'board' : null)
  }
  function openQuest(q: QuestSummary) {
    open(q.state === 'drafting' ? 'chat' : 'review', q)
  }
  function station(id: StationId) {
    open(id === 'receptionist' ? 'new' : id)
  }
  async function create() {
    if (!project) {
      open('projects')
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await api.createQuest(project, message || null)
      setQuestId(result.id)
      setQuestProject(project)
      setMessage('')
      setPanel('chat')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function register() {
    setBusy(true)
    setError('')
    try {
      await api.registerProject(path)
      await refreshProjects()
      setProject(path)
      savePref('guildhall.project', path)
      setPath('')
      setPanel('new')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  const orderedQuests = workbench
    ? quests.toSorted((a, b) =>
        (b.created || b.id).localeCompare(a.created || a.id),
      )
    : quests
  const visible = orderedQuests
    .filter((q) =>
      panel === 'new'
        ? q.project === project
        : !projectFilter || q.project === projectFilter,
    )
    .filter((q) => {
      if (panel === 'archive') return terminal.has(q.state)
      if (panel === 'adventurer')
        return ['posted', 'in_progress', 'failed'].includes(q.state)
      if (panel === 'appraiser')
        return ['appraising', 'appraised', 'disputed'].includes(q.state)
      if (panel === 'new') return q.state === 'drafting'
      return workbench && panel === 'board' ? true : !terminal.has(q.state)
    })
    .filter((q) =>
      `${q.title} ${q.id} ${labels[q.state]}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    )
  const titles: Record<Panel, string> = {
    new: workbench ? '新建任务' : '前台接待',
    board: workbench ? '全部任务' : '委托告示板',
    adventurer: workbench ? '执行队列' : '冒险者',
    appraiser: workbench ? '验收与审阅' : '鉴定台',
    archive: workbench ? '已归档' : '公会档案',
    settings: workbench ? '设置' : '公会设置',
    projects: workbench ? '项目管理' : '登记项目',
    chat: workbench ? '需求沟通' : '前台 · 委托洽谈',
    review: workbench ? '执行与审阅' : '委托 · 执行与鉴定',
  }
  const panelTitle = panel ? titles[panel] : ''
  useEffect(() => {
    if (panel && !['chat', 'review'].includes(panel))
      document.title = `${panelTitle} · Guildhall`
  }, [panel, panelTitle])
  const list = workbench ? (
    <WorkbenchTaskList
      quests={visible}
      search={search}
      onSearch={setSearch}
      onQuest={openQuest}
      onNew={() => open('new')}
    />
  ) : (
    <>
      <label className="search-field">
        查找委托
        <input
          placeholder="搜索标题、编号或状态…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      <div className="quest-cards">
        {visible.map((q) => (
          <button
            className="quest-card"
            key={q.id}
            onClick={() => openQuest(q)}
          >
            <span className={`seal state-${q.state}`} />
            <span>
              <strong>{q.title || '尚未定稿的委托'}</strong>
              <small title={q.project}>
                {q.project} · {q.id}
              </small>
            </span>
            <span className={`status state-${q.state}`}>{labels[q.state]}</span>
            <span aria-hidden="true">↗</span>
          </button>
        ))}
        {visible.length === 0 && (
          <div className="empty-state">
            <span>◇</span>
            <h3>{search ? '没有匹配的委托' : '这里还没有委托'}</h3>
            <p>
              {search
                ? '试试其他关键词。'
                : '去前台聊聊想法，第一张委托从那里开始。'}
            </p>
            <button onClick={() => open('new')}>前往前台</button>
          </div>
        )}
      </div>
    </>
  )
  return (
    <div
      className={`guild-app ${workbench ? `workbench ${resolved === 'dark' ? 'dark' : ''}` : ''} ${motion ? 'reduce-motion' : ''}`}
      data-theme={workbench ? resolved : 'tavern'}
      style={
        {
          ...(workbench ? {} : { '--accent': skin.palette.accent }),
          '--hall-bg': skin.palette.background,
        } as CSSProperties
      }
    >
      {workbench && (
        <WorkbenchSidebar
          page={panel}
          questId={questId}
          projects={Array.from(
            new Set([...projects, ...quests.map((q) => q.project)]),
          )}
          project={projectFilter}
          onProject={(value) => {
            setProjectFilter(value)
            if (value) {
              setProject(value)
              savePref('guildhall.project', value)
            }
          }}
          quests={orderedQuests}
          onNavigate={open}
          onQuest={openQuest}
          mode={mode}
          onMode={changeMode}
          busy={busy}
        />
      )}
      {!workbench && (
        <>
          <header className="hall-header">
            <a className="brand" href="#" onClick={(e) => e.preventDefault()}>
              <span className="brand-mark">✦</span>
              <span>
                GUILDHALL<small>冒险家协会</small>
              </span>
            </a>
            <div className="hall-counts">
              <button onClick={() => open('adventurer')}>
                进行中 <b>{active.length}</b>
              </button>
              <button onClick={() => open('appraiser')}>
                待审阅 <b>{review.length}</b>
              </button>
            </div>
            <button
              aria-label="公会设置"
              className="settings-button"
              onClick={() => open('settings')}
            >
              ⚙ <span>设置</span>
            </button>
          </header>
          <main className="hall-main">
            {import.meta.env.VITE_GUILDHALL_DEMO === '1' && (
              <div className="demo-banner">
                演示环境 · 隔离测试项目与模拟 Agent，不连接真实模型
              </div>
            )}
            <div className="hall-intro">
              <div>
                <span className="eyebrow">YOUR NEXT ADVENTURE STARTS HERE</span>
                <h1>欢迎回到公会。</h1>
                <p>找前台聊聊想法，或去委托板看看大家的进展。</p>
              </div>
              <button className="primary" onClick={() => open('new')}>
                ＋ 找前台发布委托
              </button>
            </div>
            {error && !panel && (
              <div role="alert" className="notice error">
                {error}
                <button
                  onClick={() => {
                    setError('')
                    refreshProjects().catch((e) => setError(String(e)))
                  }}
                >
                  重试连接
                </button>
              </div>
            )}
            <TavernScene
              skin={skin}
              quests={quests}
              onStation={station}
              onQuest={openQuest}
            />
            <nav className="station-shortcuts" aria-label="公会设施">
              {skin.stations.map((s, i) => (
                <button key={s.id} onClick={() => station(s.id)}>
                  <span>0{i + 1}</span>
                  <strong>{s.label}</strong>
                  <small>{s.hint}</small>
                  <b>↗</b>
                </button>
              ))}
            </nav>
            <footer className="hall-footer">
              <span>
                {projects.length} 个项目 · {quests.length} 份委托
              </span>
              <span>自动鉴定之后，由你决定交付。</span>
            </footer>
          </main>
        </>
      )}
      <dialog
        aria-label={panel ? titles[panel] : undefined}
        ref={dialog}
        role={workbench ? 'region' : undefined}
        className={`hall-dialog ${panel === 'chat' || panel === 'review' ? 'workspace-dialog' : ''}`}
        onCancel={(e) => {
          e.preventDefault()
          close()
        }}
      >
        {panel && !['chat', 'review'].includes(panel) && (
          <div className="dialog-heading">
            <span>
              <small>GUILDHALL / </small>
              {panel && titles[panel]}
            </span>
            {!workbench && (
              <button aria-label="关闭面板" disabled={busy} onClick={close}>
                ✕
              </button>
            )}
          </div>
        )}

        {workbench && import.meta.env.VITE_GUILDHALL_DEMO === '1' && (
          <div className="demo-banner">演示环境 · 隔离数据与模拟 Agent</div>
        )}
        {pendingNavigation && (
          <div className="unsaved-banner" role="alert">
            <div>
              <strong>
                {panel === 'settings'
                  ? '模型配置尚未保存'
                  : '当前任务有未保存的内容'}
              </strong>
              <p>
                {panel === 'settings'
                  ? '离开会丢弃本次模型配置修改。'
                  : '离开会丢弃未发送的消息或需求单修改，已保存内容与后台任务会保留。'}
              </p>
            </div>
            <button onClick={() => setPendingNavigation(null)}>继续编辑</button>
            <button
              onClick={() => {
                if (!busy)
                  navigate(pendingNavigation.panel, pendingNavigation.quest)
              }}
            >
              放弃修改并离开
            </button>
          </div>
        )}
        {error && panel && (
          <div role="alert" className="notice error">
            {error}
          </div>
        )}
        {panel === 'review' && !workbench && (
          <div className="quest-project-context" title={questProject}>
            所属项目 · {questProject}
          </div>
        )}
        <div className="dialog-body">
          {panel === 'settings' && (
            <Settings
              colorMode={mode}
              onColorMode={changeMode}
              skinId={skinId}
              onSkin={(id) => {
                setSkinId(id)
                savePref('guildhall.skin', id)
              }}
              reducedMotion={motion}
              onMotion={(v) => {
                setMotion(v)
                savePref('guildhall.reducedMotion', String(v))
              }}
              onDirty={setSettingsDirty}
              onBusy={setBusy}
            />
          )}
          {panel === 'chat' && (
            <Chat
              taskTitle={quests.find((q) => q.id === questId)?.title}
              onBusy={setBusy}
              workbench={workbench}
              sceneFilter={skin.filter}
              project={questProject}
              onDirty={setChatDirty}
              key={questId}
              questId={questId}
              onBack={close}
              onGotoReview={(id) => {
                setQuestId(id)
                setPanel('review')
              }}
            />
          )}
          {panel === 'review' && (
            <Review
              taskTitle={quests.find((q) => q.id === questId)?.title}
              onBusy={setBusy}
              workbench={workbench}
              key={questId}
              questId={questId}
              onBack={close}
            />
          )}
          {panel === 'projects' && (
            <form
              className="panel-content"
              onSubmit={(e) => {
                e.preventDefault()
                void register()
              }}
            >
              {!workbench && <span className="eyebrow">A NEW CHAPTER</span>}
              <h2>{workbench ? '项目管理' : '登记你的项目'}</h2>
              <p>选择本机已有的 Git 仓库，为它保存任务与验收记录。</p>
              <label>
                仓库绝对路径
                <input
                  required
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  placeholder="/Users/you/Projects/my-project"
                />
              </label>
              <button className="primary" disabled={busy || !path.trim()}>
                {busy
                  ? '登记中…'
                  : workbench
                    ? '登记并新建任务'
                    : '登记并前往前台'}
              </button>
            </form>
          )}
          {panel === 'new' && (
            <div className="panel-content">
              {!workbench && (
                <span className="eyebrow">THE RECEPTION DESK</span>
              )}
              <h2>{workbench ? '从一个想法开始' : '有什么想交给公会？'}</h2>
              <p>先说个大概就好。我们一起把目标、范围与验收方式写清楚。</p>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void create()
                }}
              >
                <div className="reception-project">
                  <label>
                    {workbench ? '所属项目' : '这份委托属于哪个项目？'}
                    <select
                      aria-label="委托所属项目"
                      value={project}
                      disabled={busy}
                      onChange={(e) => {
                        setProject(e.target.value)
                        savePref('guildhall.project', e.target.value)
                      }}
                    >
                      <option value="" disabled>
                        先选择或登记一个项目
                      </option>
                      {projects.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => open('projects')}
                  >
                    ＋ 登记新项目
                  </button>
                </div>
                <label>
                  这次的想法
                  <Textarea
                    rows={4}
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    placeholder="例如：给项目增加一个可以切换主题的设置页面…"
                  />
                </label>
                <button className="primary" disabled={busy}>
                  {busy
                    ? '正在创建任务…'
                    : project
                      ? workbench
                        ? '开始需求沟通 →'
                        : '与前台交谈 →'
                      : '先登记项目 →'}
                </button>
              </form>
              <h3 className="list-heading">继续需求沟通</h3>
              {list}
            </div>
          )}
          {panel &&
            ['board', 'adventurer', 'appraiser', 'archive'].includes(panel) && (
              <div className="panel-content">
                {!workbench && <span className="eyebrow">全公会委托</span>}
                <h2>{titles[panel]}</h2>
                <p>
                  {panel === 'appraiser'
                    ? '查看独立验收的证据，审阅改动，然后决定是否接受并合并。'
                    : panel === 'adventurer'
                      ? '开始执行、观察进展，或恢复中断的任务。'
                      : panel === 'archive'
                        ? '已交付或已放弃的任务，保留完整成果与记录。'
                        : '从需求到交付，跟进每一项工作。'}
                </p>
                <label
                  className={
                    workbench ? 'workbench-mobile-project' : 'project-filter'
                  }
                >
                  筛选项目
                  <select
                    aria-label="筛选项目"
                    value={projectFilter}
                    onChange={(e) => setProjectFilter(e.target.value)}
                  >
                    <option value="">所有项目</option>
                    {Array.from(
                      new Set([...projects, ...quests.map((q) => q.project)]),
                    ).map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                </label>
                {list}
              </div>
            )}
        </div>
      </dialog>
    </div>
  )
}
