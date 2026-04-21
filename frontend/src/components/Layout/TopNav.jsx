import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Layout, Select, Button, Space, Typography, Tooltip } from 'antd'
import {
  EditOutlined,
  BarChartOutlined,
  BugOutlined,
  PlusOutlined,
  VideoCameraAddOutlined,
} from '@ant-design/icons'
import useStore from '../../store/useStore'
import NewProjectModal from './NewProjectModal'

const { Header } = Layout
const { Text } = Typography

export default function TopNav() {
  const navigate = useNavigate()
  const location = useLocation()
  const {
    projects,
    projectsLoading,
    currentProject,
    fetchProjects,
    setCurrentProject,
  } = useStore()

  const [newProjectOpen, setNewProjectOpen] = useState(false)

  /* 應用啟動時拉取專案列表 */
  useEffect(() => {
    fetchProjects()
  }, [fetchProjects])

  const isEditor = location.pathname.startsWith('/editor')
  const isReport = location.pathname.startsWith('/reports')
  const isRecorder = location.pathname.startsWith('/recorder')

  return (
    <Header
      style={{
        background: 'var(--color-surface)',
        borderBottom: '1px solid var(--color-border)',
        height: 48,
        lineHeight: '48px',
        padding: '0 16px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}
    >
      {/* ── 品牌 Logo ── */}
      <Space align="center">
        <BugOutlined style={{ color: '#1677ff', fontSize: 18 }} />
        <Text strong style={{ color: '#e6edf3', fontSize: 15 }}>
          AutoTest{' '}
          <span style={{ color: '#8b949e', fontWeight: 400, fontSize: 12 }}>v1.0</span>
        </Text>
      </Space>

      {/* ── 模式切換按鈕 ── */}
      <Space>
        <Button
          type={isEditor ? 'primary' : 'text'}
          icon={<EditOutlined />}
          onClick={() => navigate('/editor')}
          style={{ color: isEditor ? undefined : '#8b949e' }}
        >
          案例編輯
        </Button>
        <Button
          type={isReport ? 'primary' : 'text'}
          icon={<BarChartOutlined />}
          onClick={() => navigate('/reports')}
          style={{ color: isReport ? undefined : '#8b949e' }}
        >
          執行報告
        </Button>
        <Button
          type={isRecorder ? 'primary' : 'text'}
          icon={<VideoCameraAddOutlined />}
          onClick={() => navigate('/recorder')}
          style={{ color: isRecorder ? undefined : '#8b949e' }}
        >
          錄製
        </Button>
      </Space>

      {/* ── 專案選擇器 ── */}
      <Space align="center">
        <Text style={{ color: '#8b949e', fontSize: 13 }}>專案：</Text>
        <Select
          style={{ width: 220 }}
          placeholder="請選擇專案..."
          loading={projectsLoading}
          value={currentProject?.id ?? undefined}
          onChange={(val) => {
            const proj = projects.find((p) => p.id === val)
            if (proj) setCurrentProject(proj)
          }}
          options={projects.map((p) => ({ label: p.name, value: p.id }))}
          dropdownStyle={{ minWidth: 240 }}
        />
        <Tooltip title="新建專案">
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            style={{ color: '#8b949e' }}
            onClick={() => setNewProjectOpen(true)}
          />
        </Tooltip>
      </Space>

      <NewProjectModal open={newProjectOpen} onClose={() => setNewProjectOpen(false)} />
    </Header>
  )
}
