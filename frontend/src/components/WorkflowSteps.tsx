import { Check } from 'lucide-react'
import type { QuestState } from '../api'

export default function WorkflowSteps({ state }: { state?: QuestState }) {
  const stage =
    state === 'withdrawn' || state === 'failed'
      ? -1
      : state === 'drafting' || state === 'posted'
        ? 0
        : state === 'in_progress'
          ? 1
          : state === 'appraising'
            ? 2
            : state
              ? 3
              : -1
  return (
    <ol className="workflow-steps" aria-label="任务流程">
      {['需求沟通', '执行', '自动验收', '人工审阅'].map((label, index) => (
        <li
          key={label}
          aria-current={index === stage ? 'step' : undefined}
          data-complete={index < stage || state === 'settled'}
        >
          <span>
            {index < stage || state === 'settled' ? (
              <Check size={12} />
            ) : (
              index + 1
            )}
          </span>
          {label}
        </li>
      ))}
    </ol>
  )
}
