import { useEffect, useState } from 'react'
import { questsStreamUrl, statusStreamUrl } from './api'
import type { QuestDetail, QuestSummary } from './api'

/**
 * 状态/运行态 SSE(§2.2)。与事件流语义相反:状态是「当前值」,不是历史,
 * 后端不做游标、每次推全量,所以这里不做增量累加,直接整包替换;
 * 断线重连后 EventSource 收到的第一条就是全量当前状态。
 * 心跳推送不带 quest_md(可能很大),合并时保留旧值。
 */
export function useQuestStatusStream(questId: string, enabled = true): QuestDetail | null {
  const [detail, setDetail] = useState<QuestDetail | null>(null)
  useEffect(() => {
    if (!enabled) {
      setDetail(null)
      return
    }
    setDetail(null)
    const es = new EventSource(statusStreamUrl(questId))
    es.onmessage = (m) => {
      let payload: Partial<QuestDetail> & Record<string, unknown>
      try {
        payload = JSON.parse(m.data)
      } catch {
        return
      }
      setDetail((prev) => ({
        quest_md: 'quest_md' in payload ? (payload.quest_md ?? null) : (prev?.quest_md ?? null),
        state: payload.state as QuestDetail['state'],
        appraisal: (payload.appraisal as QuestDetail['appraisal']) ?? null,
        runtime: (payload.runtime as QuestDetail['runtime']) ?? {},
      }))
    }
    return () => es.close()
  }, [questId, enabled])
  return detail
}

/** 列表页 SSE(§2.2):全量摘要,同样整包替换。 */
export function useQuestListStream(enabled = true): QuestSummary[] {
  const [quests, setQuests] = useState<QuestSummary[]>([])
  useEffect(() => {
    if (!enabled) return
    const es = new EventSource(questsStreamUrl())
    es.onmessage = (m) => {
      try {
        const data = JSON.parse(m.data)
        if (Array.isArray(data)) setQuests(data)
      } catch {
        /* 忽略非 JSON 心跳残片 */
      }
    }
    return () => es.close()
  }, [enabled])
  return quests
}
