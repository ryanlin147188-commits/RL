import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Row, Col, Timeline, Card, Button, Spin, Tag, Space, Typography } from 'antd'
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import * as api from '../../api/client'
import StatusBadge from '../Common/StatusBadge'
import ScreenshotViewer from '../Common/ScreenshotViewer'

const { Text } = Typography

/** API 步驟：顯示 Request / Response JSON */
function ApiViewer({ step }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card
        title={<span style={{ color: '#9ca3af', fontSize: 12 }}>Request Payload</span>}
        size="small"
        style={{ background: '#0d1117', borderColor: '#30363d' }}
        headStyle={{ borderColor: '#30363d', padding: '4px 12px' }}
        bodyStyle={{ padding: 12 }}
      >
        <pre
          style={{
            fontSize: 11,
            color: '#86efac',
            margin: 0,
            overflowX: 'auto',
            maxHeight: 260,
          }}
        >
          {step.req_payload_json
            ? JSON.stringify(step.req_payload_json, null, 2)
            : '（無資料）'}
        </pre>
      </Card>

      <Card
        title={<span style={{ color: '#9ca3af', fontSize: 12 }}>Response Body</span>}
        size="small"
        style={{ background: '#0d1117', borderColor: '#30363d' }}
        headStyle={{ borderColor: '#30363d', padding: '4px 12px' }}
        bodyStyle={{ padding: 12 }}
      >
        <pre
          style={{
            fontSize: 11,
            color: '#93c5fd',
            margin: 0,
            overflowX: 'auto',
            maxHeight: 260,
          }}
        >
          {step.res_payload_json
            ? JSON.stringify(step.res_payload_json, null, 2)
            : '（無資料）'}
        </pre>
      </Card>
    </Space>
  )
}

export default function ReportDetail() {
  const { reportId } = useParams()
  const navigate = useNavigate()

  const [report, setReport] = useState(null)
  const [stepsData, setStepsData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [activeStep, setActiveStep] = useState(null)

  useEffect(() => {
    if (!reportId) return
    setLoading(true)
    Promise.all([api.getReportDetail(reportId), api.getReportSteps(reportId)])
      .then(([r, s]) => {
        setReport(r)
        setStepsData(s)
        // 預設聚焦到第一個失敗步驟，若全通過則選第一步
        const firstFailed = s.steps?.find((step) => step.status === 'FAILED')
        setActiveStep(firstFailed ?? s.steps?.[0] ?? null)
      })
      .catch(() => {
        setReport(null)
        setStepsData(null)
      })
      .finally(() => setLoading(false))
  }, [reportId])

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 80 }}>
        <Spin size="large" tip="載入報告..." />
      </div>
    )
  }

  if (!report) {
    return (
      <div style={{ padding: 24 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/reports')}>
          返回報告列表
        </Button>
        <p style={{ color: '#f87171', marginTop: 16 }}>無法載入報告</p>
      </div>
    )
  }

  const steps = stepsData?.steps ?? []
  const isApiStep = (step) => step?.req_payload_json != null || step?.res_payload_json != null

  // 依步驟狀態決定 Timeline dot 顏色與圖示
  const stepDot = (status) => {
    if (status === 'PASSED') return <CheckCircleOutlined style={{ color: '#4ade80' }} />
    if (status === 'FAILED') return <CloseCircleOutlined style={{ color: '#f87171' }} />
    return <LoadingOutlined style={{ color: '#fbbf24' }} />
  }

  return (
    <div style={{ padding: 16, height: '100%', overflow: 'auto' }}>
      {/* ── 頁首 ── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          marginBottom: 16,
          flexWrap: 'wrap',
        }}
      >
        <Button
          type="text"
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/reports')}
          style={{ color: '#8b949e' }}
        >
          返回
        </Button>
        <StatusBadge status={report.status} />
        <Text style={{ color: '#8b949e', fontSize: 13 }}>
          {dayjs(report.created_at).format('YYYY-MM-DD HH:mm:ss')}
        </Text>
        <Tag color="default" style={{ fontSize: 12 }}>
          {report.trigger_type}
        </Tag>
        <Text style={{ color: '#4ade80', fontSize: 13 }}>{report.passed_cases} 通過</Text>
        <Text style={{ color: '#f87171', fontSize: 13 }}>{report.failed_cases} 失敗</Text>
        <Text style={{ color: '#8b949e', fontSize: 13 }}>
          共 {report.total_cases} 案例 · {(report.duration_ms / 1000).toFixed(1)}s
        </Text>
      </div>

      {/* ── 主體：左側時間軸 + 右側截圖 / JSON ── */}
      <Row gutter={16} style={{ height: 'calc(100vh - 200px)' }}>
        {/* ── 左側：步驟時間軸 ── */}
        <Col
          span={8}
          style={{
            overflowY: 'auto',
            height: '100%',
            borderRight: '1px solid #30363d',
            paddingRight: 12,
          }}
        >
          {steps.length === 0 ? (
            <Text style={{ color: '#8b949e' }}>無步驟資料</Text>
          ) : (
            <Timeline
              items={steps.map((step) => ({
                color:
                  step.status === 'PASSED'
                    ? 'green'
                    : step.status === 'FAILED'
                      ? 'red'
                      : 'gold',
                dot: stepDot(step.status),
                children: (
                  <div
                    onClick={() => setActiveStep(step)}
                    style={{
                      cursor: 'pointer',
                      padding: '6px 10px',
                      borderRadius: 6,
                      marginBottom: 4,
                      background:
                        activeStep?.id === step.id
                          ? 'rgba(22, 119, 255, 0.15)'
                          : 'transparent',
                      border:
                        activeStep?.id === step.id
                          ? '1px solid rgba(22, 119, 255, 0.35)'
                          : '1px solid transparent',
                      transition: 'all 0.15s',
                    }}
                  >
                    <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 2 }}>
                      Step {step.step_index + 1}
                    </div>
                    <div style={{ fontSize: 13, color: '#e6edf3', lineHeight: 1.4 }}>
                      {step.description || `${step.action} ${step.locator ?? ''}`.trim()}
                    </div>
                    {step.error_message && (
                      <div
                        style={{
                          fontSize: 11,
                          color: '#f87171',
                          marginTop: 4,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={step.error_message}
                      >
                        ⚠ {step.error_message}
                      </div>
                    )}
                    <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>
                      {step.duration_ms}ms
                    </div>
                  </div>
                ),
              }))}
            />
          )}
        </Col>

        {/* ── 右側：截圖或 JSON 檢視 ── */}
        <Col span={16} style={{ overflowY: 'auto', height: '100%', paddingLeft: 12 }}>
          {activeStep ? (
            isApiStep(activeStep) ? (
              <ApiViewer step={activeStep} />
            ) : (
              <ScreenshotViewer step={activeStep} />
            )
          ) : (
            <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 40 }}>
              <Text style={{ color: '#8b949e' }}>請從左側點擊步驟查看詳細</Text>
            </div>
          )}
        </Col>
      </Row>
    </div>
  )
}
