import { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Spin, Empty } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  BarChartOutlined,
} from '@ant-design/icons'
import {
  Chart as ChartJS,
  ArcElement,
  CategoryScale,
  LinearScale,
  BarElement,
  Tooltip,
  Legend,
} from 'chart.js'
import { Doughnut, Bar } from 'react-chartjs-2'
import useStore from '../../store/useStore'
import * as api from '../../api/client'
import ReportList from './ReportList'

// Chart.js 元件必須在使用前先註冊
ChartJS.register(ArcElement, CategoryScale, LinearScale, BarElement, Tooltip, Legend)

const CARD_STYLE = { background: '#161b22', borderColor: '#30363d' }
const HEAD_STYLE = { borderColor: '#30363d' }

export default function Dashboard() {
  const { currentProject } = useStore()
  const [metrics, setMetrics] = useState(null)
  const [charts, setCharts] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!currentProject) return
    setLoading(true)
    Promise.all([
      api.getDashboardMetrics(currentProject.id),
      api.getDashboardCharts(currentProject.id),
    ])
      .then(([m, c]) => {
        setMetrics(m)
        setCharts(c)
      })
      .catch(() => {
        // API 尚未連通時優雅降級（顯示 0）
        setMetrics({ total_executions: 0, pass_rate: 0, total_failures: 0, avg_duration_ms: 0 })
        setCharts(null)
      })
      .finally(() => setLoading(false))
  }, [currentProject?.id])

  if (!currentProject) {
    return (
      <div style={{ padding: 24 }}>
        <Empty description="請先從上方選擇一個專案" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </div>
    )
  }

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 64 }}>
        <Spin size="large" tip="載入儀表板..." />
      </div>
    )
  }

  return (
    <div style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* ── 統計卡片 ── */}
      <Row gutter={[16, 16]}>
        <Col span={6}>
          <Card size="small" style={CARD_STYLE}>
            <Statistic
              title={<span style={{ color: '#8b949e' }}>總執行次數</span>}
              value={metrics?.total_executions ?? 0}
              prefix={<BarChartOutlined style={{ color: '#60a5fa' }} />}
              valueStyle={{ color: '#e6edf3' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={CARD_STYLE}>
            <Statistic
              title={<span style={{ color: '#8b949e' }}>通過率</span>}
              value={metrics?.pass_rate ?? 0}
              suffix="%"
              precision={1}
              prefix={<CheckCircleOutlined style={{ color: '#4ade80' }} />}
              valueStyle={{ color: '#4ade80' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={CARD_STYLE}>
            <Statistic
              title={<span style={{ color: '#8b949e' }}>失敗次數</span>}
              value={metrics?.total_failures ?? 0}
              prefix={<CloseCircleOutlined style={{ color: '#f87171' }} />}
              valueStyle={{ color: '#f87171' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={CARD_STYLE}>
            <Statistic
              title={<span style={{ color: '#8b949e' }}>平均耗時</span>}
              value={metrics?.avg_duration_ms ? (metrics.avg_duration_ms / 1000).toFixed(1) : 0}
              suffix=" 秒"
              precision={1}
              prefix={<ClockCircleOutlined style={{ color: '#fbbf24' }} />}
              valueStyle={{ color: '#fbbf24' }}
            />
          </Card>
        </Col>
      </Row>

      {/* ── 圖表區 ── */}
      {charts ? (
        <Row gutter={[16, 16]}>
          {/* 圓餅圖：執行結果分佈 */}
          <Col span={8}>
            <Card
              title={<span style={{ color: '#c9d1d9' }}>執行結果分佈</span>}
              size="small"
              style={CARD_STYLE}
              headStyle={HEAD_STYLE}
            >
              <div style={{ height: 220 }}>
                <Doughnut
                  data={{
                    labels: ['通過', '失敗', '進行中'],
                    datasets: [
                      {
                        data: [
                          charts.status_distribution?.PASSED ?? 0,
                          charts.status_distribution?.FAILED ?? 0,
                          charts.status_distribution?.RUNNING ?? 0,
                        ],
                        backgroundColor: ['#4ade80', '#f87171', '#fbbf24'],
                        borderColor: ['#16a34a', '#dc2626', '#d97706'],
                        borderWidth: 1,
                      },
                    ],
                  }}
                  options={{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                      legend: { labels: { color: '#9ca3af', font: { size: 12 } } },
                    },
                  }}
                />
              </div>
            </Card>
          </Col>

          {/* 長條圖：近 5 次趨勢 */}
          <Col span={16}>
            <Card
              title={<span style={{ color: '#c9d1d9' }}>近 5 次執行趨勢</span>}
              size="small"
              style={CARD_STYLE}
              headStyle={HEAD_STYLE}
            >
              <div style={{ height: 220 }}>
                <Bar
                  data={{
                    labels: charts.recent_trend?.map((d) => d.label) ?? [],
                    datasets: [
                      {
                        label: '通過',
                        data: charts.recent_trend?.map((d) => d.passed) ?? [],
                        backgroundColor: 'rgba(74, 222, 128, 0.75)',
                        borderColor: '#22c55e',
                        borderWidth: 1,
                      },
                      {
                        label: '失敗',
                        data: charts.recent_trend?.map((d) => d.failed) ?? [],
                        backgroundColor: 'rgba(248, 113, 113, 0.75)',
                        borderColor: '#ef4444',
                        borderWidth: 1,
                      },
                    ],
                  }}
                  options={{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                      x: {
                        ticks: { color: '#9ca3af', font: { size: 11 } },
                        grid: { color: '#30363d' },
                      },
                      y: {
                        ticks: { color: '#9ca3af', font: { size: 11 } },
                        grid: { color: '#30363d' },
                        beginAtZero: true,
                      },
                    },
                    plugins: {
                      legend: { labels: { color: '#9ca3af', font: { size: 12 } } },
                    },
                  }}
                />
              </div>
            </Card>
          </Col>
        </Row>
      ) : null}

      {/* ── 歷史執行紀錄 ── */}
      <ReportList />
    </div>
  )
}
