import { useEffect, useRef, useState, useCallback } from 'react'
import { buildExecutionLogsWsUrl } from '../api/client'

/**
 * 即時執行日誌 WebSocket Hook
 *
 * 後端訊息格式：
 *   { type: "log",  level: "INFO" | "ERROR", message: string }
 *   { type: "done", status: "PASSED" | "FAILED" }
 *   { type: "pong" }            ← heartbeat 回應
 *   { type: "error", message }  ← 後端內部錯誤
 *
 * 用法：
 *   const { logs, status, connected, clear, disconnect } = useExecutionLogs(taskId)
 */
export default function useExecutionLogs(taskId) {
  const [logs, setLogs] = useState([])      // [{ level, message, ts }]
  const [status, setStatus] = useState(null) // null | "RUNNING" | "PASSED" | "FAILED"
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const heartbeatRef = useRef(null)

  const append = useCallback((entry) => {
    setLogs((prev) => [...prev, { ...entry, ts: Date.now() }])
  }, [])

  const clear = useCallback(() => setLogs([]), [])

  const disconnect = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
    }
    if (wsRef.current) {
      try {
        wsRef.current.close()
      } catch {
        // ignore
      }
      wsRef.current = null
    }
    setConnected(false)
  }, [])

  useEffect(() => {
    if (!taskId) return undefined

    setStatus('RUNNING')
    setLogs([])
    const url = buildExecutionLogsWsUrl(taskId)
    let ws
    try {
      ws = new WebSocket(url)
    } catch (e) {
      append({ level: 'ERROR', message: `WebSocket 連線失敗：${e.message}` })
      return undefined
    }
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      append({ level: 'INFO', message: `🔌 已連線：${url}` })
      // 每 25 秒送 ping，避免反向代理閒置斷線
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping')
      }, 25_000)
    }

    ws.onmessage = (evt) => {
      let payload
      try {
        payload = JSON.parse(evt.data)
      } catch {
        append({ level: 'INFO', message: String(evt.data) })
        return
      }
      switch (payload.type) {
        case 'log':
          append({ level: payload.level || 'INFO', message: payload.message })
          break
        case 'done':
          setStatus(payload.status)
          append({
            level: payload.status === 'PASSED' ? 'INFO' : 'ERROR',
            message: `🏁 執行完成（${payload.status}）`,
          })
          break
        case 'error':
          append({ level: 'ERROR', message: payload.message })
          break
        case 'pong':
        default:
          break
      }
    }

    ws.onerror = () => {
      append({ level: 'ERROR', message: '⚠ WebSocket 連線發生錯誤' })
    }

    ws.onclose = () => {
      setConnected(false)
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current)
        heartbeatRef.current = null
      }
    }

    return () => {
      if (heartbeatRef.current) clearInterval(heartbeatRef.current)
      try {
        ws.close()
      } catch {
        // ignore
      }
    }
  }, [taskId, append])

  return { logs, status, connected, clear, disconnect }
}
