import { useEffect, useRef, useState } from 'react'
import { eventsUrl } from './api'

export interface StreamEvent {
  seq: number
  env: any
}

/**
 * SSE 事件流(§6.1):offset 由前端持有并回传,后端不做去重。
 * 挂载时从 0 全量重放(jsonl 是权威持久层);断线重连由 EventSource
 * 自动携带 Last-Event-ID,后端同游标续放,不重复。
 */
export function useEventStream(questId: string, role: string, enabled: boolean = true): StreamEvent[] {
  const [events, setEvents] = useState<StreamEvent[]>([])
  const seqRef = useRef<number>(-1)

  useEffect(() => {
    if (!enabled) return
    seqRef.current = -1
    setEvents([])

    const es = new EventSource(eventsUrl(questId, role, 0))
    es.onmessage = (m) => {
      const seq = Number(m.lastEventId)
      let env: any
      try {
        env = JSON.parse(m.data)
      } catch {
        return
      }
      if (seq <= seqRef.current) return // 重放中已收过的事件
      seqRef.current = seq
      setEvents((prev) => [...prev, { seq, env }])
    }
    return () => es.close()
  }, [questId, role, enabled])

  return events
}
