import { useEffect, useRef, useState } from 'react'
import {
  Layout,
  Card,
  Input,
  Button,
  Space,
  Typography,
  Tabs,
  Tag,
  Upload,
  message,
  Alert,
  Modal,
  Tooltip,
} from 'antd'
import {
  VideoCameraAddOutlined,
  ThunderboltOutlined,
  DeleteOutlined,
  ReloadOutlined,
  CopyOutlined,
  UploadOutlined,
  DownloadOutlined,
  InboxOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import useStore from '../store/useStore'
import {
  createRecording,
  getRecording,
  uploadRecording,
  deleteRecording,
  convertRecording,
  traceDownloadUrl,
} from '../api/recordings'

const { Header, Content } = Layout
const { Title, Text, Paragraph } = Typography
const { Dragger } = Upload

export default function RecorderPage() {
  const navigate = useNavigate()
  const { currentProject, activeNode, testcaseContent, mergeRecordedSteps } =
    useStore()

  const [targetUrl, setTargetUrl] = useState('https://example.com')
  const [session, setSession] = useState(null)
  const [commands, setCommands] = useState(null)
  const [busy, setBusy] = useState(false)
  const [scriptFile, setScriptFile] = useState(null)
  const [traceFile, setTraceFile] = useState(null)
  const pollRef = useRef(null)

  useEffect(() => () => clearInterval(pollRef.current), [])

  // ── 1. 建立 session ────────────────────────────────
  const handleCreate = async () => {
    if (!targetUrl.trim()) return message.warning('請先輸入目標 URL')
    setBusy(true)
    try {
      const data = await createRecording({
        project_id: currentProject?.id ?? null,
        target_url: targetUrl.trim(),
      })
      setSession(data.session)
      setCommands(data.commands)
      setScriptFile(null)
      setTraceFile(null)
      message.success('已建立錄製階段，請依步驟在本機執行 codegen')
    } catch (e) {
      message.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  // ── 2. 上傳 codegen 結果 ─────────────────────────
  const handleUpload = async () => {
    if (!session) return
    if (!scriptFile && !traceFile)
      return message.warning('請至少選擇 recorded.py 或 trace.zip 其中一個')
    setBusy(true)
    try {
      const data = await uploadRecording(session.id, {
        script: scriptFile,
        trace: traceFile,
      })
      setSession(data)
      message.success('已上傳並解析腳本')
    } catch (e) {
      message.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  // ── 3. 轉成 BDD 步驟並合併 ────────────────────────
  const handleConvert = async () => {
    if (!session) return
    if (!testcaseContent) {
      return Modal.info({
        title: '尚未開啟測試案例',
        content: '請先到「案例編輯」頁面選取一筆 TESTCASE 後再回來。',
      })
    }
    setBusy(true)
    try {
      const { steps } = await convertRecording(session.id)
      if (!steps?.length) {
        message.warning('腳本中找不到可轉換的動作（goto/click/fill...）')
        return
      }
      mergeRecordedSteps(steps)
      message.success(`已合併 ${steps.length} 個步驟`)
      Modal.confirm({
        title: '已套用至測試案例',
        content: '是否立刻返回「案例編輯」檢視結果？',
        okText: '前往編輯',
        cancelText: '留在此頁',
        onOk: () => navigate('/editor'),
      })
    } catch (e) {
      message.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  // ── 4. 重置 ─────────────────────────────────────
  const handleReset = async () => {
    if (session) {
      try {
        await deleteRecording(session.id)
      } catch {
        /* ignore */
      }
    }
    setSession(null)
    setCommands(null)
    setScriptFile(null)
    setTraceFile(null)
  }

  const handleRefresh = async () => {
    if (!session) return
    try {
      const data = await getRecording(session.id)
      setSession(data)
    } catch (e) {
      message.error(e.message)
    }
  }

  const copy = async (text, label = '指令') => {
    try {
      await navigator.clipboard.writeText(text)
      message.success(`已複製${label}`)
    } catch {
      message.warning('複製失敗，請手動選取')
    }
  }

  // ── UI ──────────────────────────────────────────
  return (
    <Layout style={{ minHeight: '100vh', background: 'var(--color-bg)' }}>
      <Header
        style={{
          background: 'var(--color-surface)',
          borderBottom: '1px solid var(--color-border)',
          padding: '0 16px',
          height: 48,
          lineHeight: '48px',
        }}
      >
        <Space>
          <VideoCameraAddOutlined style={{ color: '#1677ff' }} />
          <Text strong style={{ color: '#e6edf3' }}>
            🎬 瀏覽器錄製（robotframework-browser / Playwright codegen）
          </Text>
        </Space>
      </Header>

      <Content style={{ padding: 16, display: 'grid', gap: 16 }}>
        {/* ① 建立錄製 */}
        <Card
          size="small"
          title="① 建立錄製階段"
          extra={
            session ? (
              <Tag color={session.status === 'UPLOADED' ? 'green' : 'gold'}>
                {session.status}
              </Tag>
            ) : null
          }
        >
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder="目標網站 URL（例如 https://example.com/login）"
              value={targetUrl}
              onChange={(e) => setTargetUrl(e.target.value)}
              disabled={!!session}
            />
            {session ? (
              <Button icon={<ReloadOutlined />} danger onClick={handleReset}>
                重新開始
              </Button>
            ) : (
              <Button
                type="primary"
                icon={<VideoCameraAddOutlined />}
                loading={busy}
                onClick={handleCreate}
              >
                建立錄製階段
              </Button>
            )}
          </Space.Compact>
          <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
            目前專案：<b>{currentProject?.name ?? '（未選擇）'}</b>　目前案例：
            <b>{activeNode?.name ?? '（未開啟）'}</b>
          </Paragraph>
        </Card>

        {/* ② 在本機執行 codegen */}
        {commands && (
          <Card size="small" title="② 在本機執行 Playwright codegen（任選一種方式）">
            <Alert
              type="info"
              showIcon
              message="說明"
              description={
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  <li>
                    Playwright codegen 會開啟一個真實瀏覽器視窗，您操作頁面時自動產生 Python
                    腳本，並透過 <Text code>--save-trace</Text> 同時錄製可用 Playwright Trace
                    Viewer 開啟的 trace.zip。
                  </li>
                  <li>
                    完成後關閉瀏覽器視窗，本機會留下{' '}
                    <Text code>recorded_xxxx.py</Text> 與 <Text code>trace_xxxx.zip</Text>。
                  </li>
                  <li>把這兩個檔案在下方③上傳即可。</li>
                </ul>
              }
            />

            <Tabs
              style={{ marginTop: 12 }}
              items={[
                {
                  key: 'npx',
                  label: 'A) Node.js（npx，免安裝）',
                  children: (
                    <CommandBlock cmd={commands.npx_command} onCopy={copy} />
                  ),
                },
                {
                  key: 'pip',
                  label: 'B) Python（pip 已裝 playwright）',
                  children: (
                    <CommandBlock cmd={commands.pip_command} onCopy={copy} />
                  ),
                },
                {
                  key: 'rf',
                  label: 'C) robotframework-browser（rfbrowser）',
                  children: (
                    <CommandBlock cmd={commands.rfbrowser_command} onCopy={copy} />
                  ),
                },
                {
                  key: 'one',
                  label: 'D) 一鍵執行＋自動上傳（PowerShell）',
                  children: (
                    <>
                      <CommandBlock cmd={commands.powershell_oneliner} onCopy={copy} />
                      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                        會在 codegen 結束後自動 curl 上傳腳本與 trace 至：
                        <Text code>{commands.upload_url}</Text>
                      </Paragraph>
                    </>
                  ),
                },
              ]}
            />
          </Card>
        )}

        {/* ③ 上傳結果檔案 */}
        {session && (
          <Card
            size="small"
            title="③ 上傳 codegen 結果（recorded.py / trace.zip）"
            extra={
              <Button
                icon={<ReloadOutlined />}
                size="small"
                onClick={handleRefresh}
              >
                重新整理
              </Button>
            }
          >
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Dragger
                multiple={false}
                maxCount={1}
                beforeUpload={(f) => {
                  setScriptFile(f)
                  return false
                }}
                onRemove={() => setScriptFile(null)}
                fileList={scriptFile ? [{ uid: '-1', name: scriptFile.name, status: 'done' }] : []}
                accept=".py,.txt"
              >
                <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                <p>拖曳 recorded_xxxx.py</p>
              </Dragger>
              <Dragger
                multiple={false}
                maxCount={1}
                beforeUpload={(f) => {
                  setTraceFile(f)
                  return false
                }}
                onRemove={() => setTraceFile(null)}
                fileList={traceFile ? [{ uid: '-1', name: traceFile.name, status: 'done' }] : []}
                accept=".zip"
              >
                <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                <p>拖曳 trace_xxxx.zip</p>
              </Dragger>
            </div>
            <Space style={{ marginTop: 12 }}>
              <Button
                type="primary"
                icon={<UploadOutlined />}
                onClick={handleUpload}
                loading={busy}
                disabled={!scriptFile && !traceFile}
              >
                上傳
              </Button>
              {session.trace_path && (
                <Tooltip title="下載已上傳的 trace.zip">
                  <Button
                    icon={<DownloadOutlined />}
                    href={traceDownloadUrl(session.id)}
                    target="_blank"
                  >
                    下載 trace.zip
                  </Button>
                </Tooltip>
              )}
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                onClick={handleConvert}
                disabled={!session.script_text}
              >
                套用至當前案例
              </Button>
              <Button danger icon={<DeleteOutlined />} onClick={handleReset}>
                捨棄
              </Button>
            </Space>
          </Card>
        )}

        {/* ④ 預覽腳本 */}
        {session?.script_text && (
          <Card size="small" title="④ Playwright 腳本預覽">
            <pre
              style={{
                maxHeight: 320,
                overflow: 'auto',
                background: '#0d1117',
                color: '#c9d1d9',
                padding: 12,
                borderRadius: 6,
                fontSize: 12,
                margin: 0,
              }}
            >
              {session.script_text}
            </pre>
          </Card>
        )}
      </Content>
    </Layout>
  )
}

function CommandBlock({ cmd, onCopy }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
      <Input.TextArea
        value={cmd}
        readOnly
        autoSize={{ minRows: 2, maxRows: 4 }}
        style={{ fontFamily: 'monospace', fontSize: 12 }}
      />
      <Button icon={<CopyOutlined />} onClick={() => onCopy(cmd)}>
        複製
      </Button>
    </div>
  )
}
