import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import type { AgentRuntimeStatus } from '../api'

export const ACTIVITY_LABEL: Record<AgentRuntimeStatus['activity'], string> = {
  starting: '正在启动 Claude Code',
  thinking: '正在思考',
  responding: '正在回复',
  tool: '正在使用工具',
  steering: '正在处理你的补充',
  waiting: '等待你的消息',
  error: '运行异常',
}

export function displayToolName(name: string): string {
  return ({ Bash: 'Terminal', Read: 'ReadFile', Write: 'WriteFile', Edit: 'EditFile' } as Record<string, string>)[name] ?? name
}

function activityText(status?: AgentRuntimeStatus): string {
  if (!status) return 'Claude Code 会话未连接'
  if (status.active_tool) {
    const tool = status.active_tool
    return `正在运行 ${displayToolName(tool.name)}${tool.title ? `：${tool.title}` : ''}`
  }
  return ACTIVITY_LABEL[status.activity] ?? '状态未知'
}

/** 思考区(§2.3.2 第 2 段):灰色小字、限高、新内容自动滚到底。 */
function ThoughtArea({ text }: { text: string }) {
  const ref = useRef<HTMLPreElement | null>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [text])
  return (
    <pre
      ref={ref}
      className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-gray-400"
    >
      {text}
    </pre>
  )
}

interface AgentStatusPanelProps {
  roleName: string
  status?: AgentRuntimeStatus
  /** 已完成的工具链(A → B → C);进行中的工具单独一行展示,不进链。 */
  completedTools?: string[]
  /** 活动行右侧的附加信息(如 Review 页的事件计数)。 */
  right?: ReactNode
}

/**
 * 三段结构(§2.3.2):活动行 / 思考区 / 工具区。Chat 的 receptionist 状态框与
 * Review 的 RoleDigest 共用此组件,不许各自复制一份。
 */
export default function AgentStatusPanel({ roleName, status, completedTools = [], right }: AgentStatusPanelProps) {
  const busy = status?.busy ?? false
  const running = status?.active_tool ?? null
  const quiet = busy && status!.seconds_since_event >= 60
    ? ` · 已 ${Math.round(status!.seconds_since_event)} 秒无新事件，可能卡住`
    : busy && status!.seconds_since_event >= 15
      ? ` · ${Math.round(status!.seconds_since_event)} 秒无新事件`
      : ''
  const context = status?.context_size && status.context_used != null
    ? ` · 上下文 ${Math.round((status.context_used / status.context_size) * 100)}%`
    : ''
  const runningNames = new Set(running ? [displayToolName(running.name)] : [])
  const chain = completedTools.filter((t) => !runningNames.has(t))
  const hasToolArea = Boolean(running) || chain.length > 0 || Boolean(status)

  return (
    <div className="min-w-0">
      <div className="flex min-w-0 items-center gap-2 text-xs text-gray-600" title={status?.error ?? status?.detail ?? undefined}>
        <span className={`h-2 w-2 shrink-0 rounded-full ${status?.activity === 'error' ? 'bg-red-500' : busy ? 'animate-pulse bg-emerald-500' : 'bg-gray-300'}`} />
        <span className="truncate">
          {roleName} · {activityText(status)}
          {status?.detail && !status.active_tool ? ` · ${status.detail}` : ''}
          {quiet}{context}
        </span>
        {right && <span className="ml-auto shrink-0 font-normal text-gray-400">{right}</span>}
      </div>
      {status?.thought_tail && <ThoughtArea text={status.thought_tail} />}
      {hasToolArea && (
        <div className="min-w-0 text-gray-500">
          {running && (
            <div className="flex items-center gap-1.5 font-medium text-gray-700">
              <span className="inline-block h-2 w-2 shrink-0 animate-spin rounded-full border border-amber-500 border-t-transparent" />
              <span className="truncate">正在运行 {displayToolName(running.name)}{running.title ? `：${running.title}` : ''}</span>
            </div>
          )}
          {chain.length > 0 && (
            <div className={running ? 'truncate text-gray-400' : 'truncate'}>工具调用：{chain.slice(-8).join(' → ')}</div>
          )}
          {!running && chain.length === 0 && (busy ? '事件流正在更新…' : '(还没有动作)')}
        </div>
      )}
    </div>
  )
}
