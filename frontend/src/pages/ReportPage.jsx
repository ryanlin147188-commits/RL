import { useParams } from 'react-router-dom'
import Dashboard from '../components/Report/Dashboard'
import ReportDetail from '../components/Report/ReportDetail'

/**
 * 執行報告頁面（路由：/reports 與 /reports/:reportId）
 *
 * - 未帶 reportId → 顯示儀表板（統計卡片 + 圖表 + 歷史列表）
 * - 帶有 reportId  → 顯示單次執行的詳細報告（步驟時間軸 + 截圖）
 */
export default function ReportPage() {
  const { reportId } = useParams()
  return reportId ? <ReportDetail /> : <Dashboard />
}
