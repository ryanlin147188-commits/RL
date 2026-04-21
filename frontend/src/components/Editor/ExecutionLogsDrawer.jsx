import { useEffect, useRef } from 'react'
import { Button, Space, Tag } from 'antd'
import {
  CloseOutlined,
  ClearOutlined,
  DownOutlined,
  UpOutlined,
  LoadingOutlined,
} from '@ant-design/icons'
import useExecutionLogs from '../../hooks/useExecutionLogs'

const LEVEL_COLOR = {
  INFO: '#9ca3af',
  WARN: '#fbbf24',
  ERROR: '#f87171',
}

/**
 * 即時日誌抽屜（內嵌於 EditorPanel 底部）
 * - 連線到 /ws/v1/executions/{taskId}/logs
 * - 自動捲動至底
 * - 支援收合 / 清除 / 關閉
 */
export default function ExecutionLogsDrawer({ taskId, collapsed, onToggleCollapse, onClose }) {
  const { logs, status, connected, clear } = useExecutionLogs(taskId)
  const bodyRef = useRef(null)

  // 新訊息進來自動捲到底
  useEffect(() => {
    if (!collapsed && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [logs, collapsed])

  const statusTag = (() => {
    if (status === 'PASSED') return <Tag color="success">PASSED</Tag>
    if (status === 'FAILED') return <Tag color="error">FAILED</Tag>
    if (status === 'RUNNING') {
      return (
        <Tag icon={<LoadingOutlined />} color="processing">
          RUNNING
        </Tag>
      )
    }
    return null
  })()

  return (
    <div
      style={{
        flexShrink: 0,
        borderTop: '1px solid var(--color-border)',
        background: '#0d1117',
        display: 'flex',
        flexDirection: 'column',
        height: collapsed ? 36 : 280,
        transition: 'height 0.2s ease',
      }}
    >
      {/* Header */}
      <div
        style={{
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '4px 12px',
          background: '#161b22',
          borderBottom: collapsed ? 'none' : '1px solid #30363d',
          height: 36,
        }}
      >
        <Space size={8}>
          <span style={{ fontSize: 12, color: '#e6edf3', fontWeight: 600 }}>
            ▶ 即時執行日誌
          </span>
          <span style={{ fontSize: 11, color: '#6e7681' }}>
            task: {taskId.slice(0, 8)}…
          </span>
          {statusTag}
          <Tag color={connected ? 'green' : 'default'} style={{ marginLeft: 4 }}>
            {connected ? '已連線' : '已中斷'}
          </Tag>
          <span style={{ fontSize: 11, color: '#6e7681' }}>{logs.length} 條</span>
        </Space>
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<ClearOutlined />}
            onClick={clear}
            style={{ color: '#8b949e' }}
            title="清除"
          />
          <Button
            type="text"
            size="small"
            icon={collapsed ? <UpOutlined /> : <DownOutlined />}
            onClick={onToggleCollapse}
            style={{ color: '#8b949e' }}
            title={collapsed ? '展開' : '收合'}
          />
          <Button
            type="text"
            size="small"
            icon={<CloseOutlined />}
            onClick={onClose}
            style={{ color: '#8b949e' }}
            title="關閉"
          />
        </Space>
      </div>

      {/* Body */}
      {!collapsed && (
        <div
          ref={bodyRef}
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '8px 12px',
            fontFamily: "'Fira Code', 'Consolas', monospace",
            fontSize: 12,
            lineHeight: 1.6,
          }}
        >
          {logs.length === 0 ? (
            <div style={{ color: '#6e7681', fontStyle: 'italic' }}>
              等待執行訊息中…
            </div>
          ) : (
            logs.map((entry, idx) => (
              <div
                key={idx}
                style={{
                  color: LEVEL_COLOR[entry.level] || LEVEL_COLOR.INFO,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {entry.message}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
