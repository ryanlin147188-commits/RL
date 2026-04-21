import { useState, useEffect } from 'react'
import { Button, Space, Spin, Empty, message, Tooltip } from 'antd'
import { SaveOutlined, PlayCircleOutlined } from '@ant-design/icons'
import useStore from '../../store/useStore'
import StepTable from './StepTable'
import DdtTable from './DdtTable'
import ExecutionLogsDrawer from './ExecutionLogsDrawer'
import * as api from '../../api/client'

/** 驗收準則（ATDD）純文字輸入框
 *
 * 使用「本地 state + onBlur 提交」模式：
 * - 每次切換測試案例時，useEffect 同步外部值到本地
 * - 失焦（onBlur）時才將本地值更新至 Zustand Store
 * - 避免每個按鍵都觸發全域重新渲染
 */
function AtddSection() {
  const { testcaseContent, updateAcText } = useStore()
  const [localText, setLocalText] = useState('')

  // 切換到不同測試案例時重設本地值
  useEffect(() => {
    setLocalText(testcaseContent?.ac_text ?? '')
  }, [testcaseContent?.node_id])

  return (
    <section>
      <h3 style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', marginBottom: 8 }}>
        📋 驗收準則（ATDD / Gherkin 語法）
      </h3>
      <textarea
        value={localText}
        onChange={(e) => setLocalText(e.target.value)}
        onBlur={() => {
          if (localText !== (testcaseContent?.ac_text ?? '')) {
            updateAcText(localText)
          }
        }}
        placeholder={
          'Given 使用者位於登入頁面\nWhen 輸入有效的帳號與密碼\nThen 系統成功導向首頁\n  And 顯示歡迎訊息'
        }
        style={{
          width: '100%',
          height: 120,
          background: '#0d1117',
          border: '1px solid #30363d',
          borderRadius: 6,
          padding: '10px 12px',
          color: '#e6edf3',
          fontSize: 13,
          lineHeight: 1.6,
          resize: 'vertical',
          outline: 'none',
          fontFamily: "'Fira Code', 'Consolas', monospace",
          transition: 'border-color 0.2s',
        }}
        onFocus={(e) => (e.target.style.borderColor = '#1677ff')}
        onBlurCapture={(e) => (e.target.style.borderColor = '#30363d')}
      />
    </section>
  )
}

/** 前置動作（Pre-Setup）純文字輸入框
 *
 * 與 AtddSection 相同的「本地 state + onBlur 提交」模式。
 * 用於記錄執行測試前需要準備的環境/資料 (例如資料庫 seed、登入 token)。
 */
function SetupSection() {
  const { testcaseContent, updateSetupText } = useStore()
  const [localText, setLocalText] = useState('')

  useEffect(() => {
    setLocalText(testcaseContent?.setup_text ?? '')
  }, [testcaseContent?.node_id])

  return (
    <section>
      <h3 style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', marginBottom: 8 }}>
        ⚙️ 前置動作（Pre-Setup）
      </h3>
      <textarea
        value={localText}
        onChange={(e) => setLocalText(e.target.value)}
        onBlur={() => {
          if (localText !== (testcaseContent?.setup_text ?? '')) {
            updateSetupText(localText)
          }
        }}
        placeholder={'例：\n- 建立測試資料庫 seed\n- 預先取得 API token\n- 啟動 mock server'}
        style={{
          width: '100%',
          height: 100,
          background: '#0d1117',
          border: '1px solid #30363d',
          borderRadius: 6,
          padding: '10px 12px',
          color: '#e6edf3',
          fontSize: 13,
          lineHeight: 1.6,
          resize: 'vertical',
          outline: 'none',
          fontFamily: "'Fira Code', 'Consolas', monospace",
          transition: 'border-color 0.2s',
        }}
        onFocus={(e) => (e.target.style.borderColor = '#1677ff')}
        onBlurCapture={(e) => (e.target.style.borderColor = '#30363d')}
      />
    </section>
  )
}

/** 編輯區主元件 */
export default function EditorPanel() {
  const { activeNode, testcaseContent, contentLoading, contentSaving, isDirty, saveTestcase } =
    useStore()
  const [executing, setExecuting] = useState(false)
  const [activeTaskId, setActiveTaskId] = useState(null)
  const [logsCollapsed, setLogsCollapsed] = useState(false)

  // ── 空狀態 ──────────────────────────────────────────
  if (!activeNode) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#8b949e',
        }}
      >
        <Empty description="請從左側目錄樹選擇一個測試案例（.md）" />
      </div>
    )
  }

  if (activeNode.levelType !== 'TESTCASE') {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#8b949e',
        }}
      >
        <Empty
          description={
            <span>
              已選中：<strong style={{ color: '#e6edf3' }}>{activeNode.title}</strong>
              <br />
              請繼續展開，選擇最底層的 <code>.md</code> 測試案例
            </span>
          }
        />
      </div>
    )
  }

  if (contentLoading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" tip="載入測試案例..." />
      </div>
    )
  }

  // ── 儲存 ────────────────────────────────────────────
  const handleSave = async () => {
    try {
      await saveTestcase()
      message.success('儲存成功')
    } catch (e) {
      message.error(`儲存失敗：${e.message}`)
    }
  }

  // ── 觸發執行（向後端送出 Celery 任務）───────────────
  const handleExecute = async () => {
    if (isDirty) {
      message.warning('請先儲存後再執行')
      return
    }
    setExecuting(true)
    try {
      const res = await api.triggerExecution({
        node_id: activeNode.key,
        trigger_type: 'Manual',
      })
      message.success(`已送出執行任務 (task_id: ${res.task_id?.slice(0, 8)}…)`)
      setActiveTaskId(res.task_id)
      setLogsCollapsed(false)
    } catch (e) {
      message.error(`執行觸發失敗：${e.message}`)
    } finally {
      setExecuting(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* ── 頁首工具列 ── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 16px',
          borderBottom: '1px solid var(--color-border)',
          background: 'var(--color-surface)',
          flexShrink: 0,
        }}
      >
        <Space align="center">
          <span style={{ fontSize: 13, color: '#8b949e' }}>📄</span>
          <span style={{ fontSize: 14, color: '#e6edf3', fontWeight: 500 }}>
            {activeNode.title}
          </span>
          {isDirty && (
            <span style={{ fontSize: 11, color: '#fbbf24' }}>● 未儲存</span>
          )}
        </Space>

        <Space>
          <Button
            icon={<SaveOutlined />}
            type={isDirty ? 'primary' : 'default'}
            loading={contentSaving}
            disabled={!isDirty}
            onClick={handleSave}
          >
            儲存
          </Button>
          <Tooltip title="儲存後才能執行">
            <Button
              icon={<PlayCircleOutlined />}
              loading={executing}
              onClick={handleExecute}
              style={{ borderColor: '#22c55e', color: '#22c55e' }}
            >
              執行
            </Button>
          </Tooltip>
        </Space>
      </div>

      {/* ── 內容區（可捲動）── */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: 16,
          display: 'flex',
          flexDirection: 'column',
          gap: 24,
        }}
      >
        <AtddSection />
        <SetupSection />
        <StepTable />
        <DdtTable />
        {/* 底部留白，避免最後一個元素被捲軸遮住 */}
        <div style={{ height: 40 }} />
      </div>

      {/* ── 即時執行日誌抽屜 ── */}
      {activeTaskId && (
        <ExecutionLogsDrawer
          key={activeTaskId}
          taskId={activeTaskId}
          collapsed={logsCollapsed}
          onToggleCollapse={() => setLogsCollapsed((v) => !v)}
          onClose={() => setActiveTaskId(null)}
        />
      )}
    </div>
  )
}
