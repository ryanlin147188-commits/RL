import { Routes, Route, Navigate } from 'react-router-dom'
import AppLayout from './components/Layout/AppLayout'
import EditorPage from './pages/EditorPage'
import ReportPage from './pages/ReportPage'

/**
 * 應用根元件
 *
 * 路由結構：
 *   /          → 重導向至 /editor
 *   /editor    → 測試案例編輯模式（含左側目錄樹）
 *   /reports   → 執行報告儀表板
 *   /reports/:reportId → 單次執行詳細報告
 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/editor" replace />} />
        <Route path="editor" element={<EditorPage />} />
        <Route path="reports" element={<ReportPage />} />
        <Route path="reports/:reportId" element={<ReportPage />} />
      </Route>
    </Routes>
  )
}
