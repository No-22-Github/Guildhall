import type { QuestState } from './api'

export const WORK_STATE_LABEL: Record<QuestState, string> = {
  drafting: '需求沟通',
  posted: '待执行',
  in_progress: '执行中',
  appraising: '自动验收',
  appraised: '待审阅',
  disputed: '有争议',
  settled: '已交付',
  failed: '执行异常',
  withdrawn: '已放弃',
}
