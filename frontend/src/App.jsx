import { Routes, Route, Navigate } from 'react-router-dom'
import AppLayout from './components/Layout/AppLayout'
import EditorPage from './pages/EditorPage'
import ReportPage from './pages/ReportPage'
import RecorderPage from './pages/RecorderPage'

/**
 * 應用根元件
 *
 * 路由結構：
 *   /          → 重導向至 /editor
 *   /editor    → 測試案例編輯模式（含左側目錄樹）
 *   /reports   → 執行報告儀表板
 *   /reports/:reportId → 單次執行詳細報告
 *   /recorder  → 瀏覽器錄製 → 自動產生 BDD/KDT 步驟
 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/editor" replace />} />
        <Route path="editor" element={<EditorPage />} />
        <Route path="reports" element={<ReportPage />} />
        <Route path="reports/:reportId" element={<ReportPage />} />
        <Route path="recorder" element={<RecorderPage />} />
      </Route>
    </Routes>
  )
}
