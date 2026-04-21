import { Layout } from 'antd'
import { Outlet } from 'react-router-dom'
import { useLocation } from 'react-router-dom'
import TopNav from './TopNav'
import ProjectTree from '../Sidebar/ProjectTree'

const { Sider, Content } = Layout

/**
 * 應用主版面
 * ┌─────────────────────────────────────────────────┐
 * │                   TopNav (48px)                 │
 * ├────────────┬────────────────────────────────────┤
 * │ ProjectTree│        <Outlet />                  │
 * │  (260px)   │  EditorPage 或 ReportPage          │
 * └────────────┴────────────────────────────────────┘
 *
 * 在報告詳細頁隱藏側邊欄，給截圖檢視留更多寬度。
 */
export default function AppLayout() {
  const location = useLocation()
  // 報告詳細頁（/reports/:id）折疊側邊欄節省空間
  const isDetailPage = /^\/reports\/.+/.test(location.pathname)

  return (
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <TopNav />
      <Layout style={{ overflow: 'hidden' }}>
        <Sider
          width={260}
          collapsible
          collapsed={isDetailPage}
          collapsedWidth={0}
          trigger={null}
          style={{
            background: 'var(--color-surface)',
            borderRight: '1px solid var(--color-border)',
            overflow: 'hidden auto',
          }}
        >
          <ProjectTree />
        </Sider>
        <Content
          style={{
            background: 'var(--color-bg)',
            overflow: 'hidden auto',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
