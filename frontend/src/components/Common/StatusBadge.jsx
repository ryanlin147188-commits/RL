import { Tag } from 'antd'

const STATUS_MAP = {
  RUNNING: { color: 'processing', label: '執行中' },
  PASSED: { color: 'success', label: '通過' },
  FAILED: { color: 'error', label: '失敗' },
}

/**
 * 執行狀態標籤
 * @param {{ status: 'RUNNING' | 'PASSED' | 'FAILED' }} props
 */
export default function StatusBadge({ status }) {
  const config = STATUS_MAP[status] ?? { color: 'default', label: status ?? '未知' }
  return <Tag color={config.color}>{config.label}</Tag>
}
