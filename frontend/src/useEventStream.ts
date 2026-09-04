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
export function useEventStream(
  questId: string,
  role: string,
  enabled: boolean = true,
  restartKey: string | number = '',
): StreamEvent[] {
  const streamKey = `${questId}:${role}:${enabled}:${restartKey}`
  const [stream, setStream] = useState<{ key: string; events: StreamEvent[] }>({ key: streamKey, events: [] })
  const seqRef = useRef<number>(-1)
  const pendingRef = useRef<StreamEvent[]>([])
  const frameRef = useRef<number | null>(null)

  useEffect(() => {
    if (!enabled) return
    seqRef.current = -1
    pendingRef.current = []

    const flush = () => {
      frameRef.current = null
      const batch = pendingRef.current
      pendingRef.current = []
      if (batch.length) {
        setStream((prev) => ({ key: streamKey, events: prev.key === streamKey ? [...prev.events, ...batch] : batch }))
      }
    }

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
      pendingRef.current.push({ seq, env })
      // 真实 Claude 会产生数万条 token chunk；按动画帧合批，避免全量重放时
      // 为每个 token 复制一次数组并触发一次 React render。
      if (frameRef.current === null) frameRef.current = requestAnimationFrame(flush)
    }
    return () => {
      es.close()
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
      frameRef.current = null
      pendingRef.current = []
    }
  }, [questId, role, enabled, streamKey])

  return stream.key === streamKey ? stream.events : []
}
