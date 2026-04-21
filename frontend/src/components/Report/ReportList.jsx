import { useEffect, useState, useCallback } from 'react'
import { Table, Button, Space, Typography } from 'antd'
import { EyeOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import dayjs from 'dayjs'
import useStore from '../../store/useStore'
import * as api from '../../api/client'
import StatusBadge from '../Common/StatusBadge'

const { Text } = Typography

export default function ReportList() {
  const { currentProject } = useStore()
  const navigate = useNavigate()

  const [reports, setReports] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)

  const fetchReports = useCallback(
    async (p = 1) => {
      if (!currentProject) return
      setLoading(true)
      try {
        const res = await api.getReports(currentProject.id, p, 10)
        setReports(res.items ?? [])
        setTotal(res.total ?? 0)
        setPage(p)
      } catch {
        setReports([])
      } finally {
        setLoading(false)
      }
    },
    [currentProject?.id],
  )

  useEffect(() => {
    fetchReports(1)
  }, [fetchReports])

  const columns = [
    {
      title: '執行時間',
      dataIndex: 'created_at',
      width: 180,
      render: (v) => (
        <Text style={{ fontSize: 13, color: '#c9d1d9' }}>
          {dayjs(v).format('YYYY-MM-DD HH:mm:ss')}
        </Text>
      ),
    },
    {
      title: '觸發方式',
      dataIndex: 'trigger_type',
      width: 100,
      render: (v) => <Text style={{ fontSize: 13, color: '#8b949e' }}>{v}</Text>,
    },
    {
      title: '狀態',
      dataIndex: 'status',
      width: 90,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: '通過 / 失敗 / 總計',
      width: 150,
      render: (_, r) => (
        <span style={{ fontSize: 13 }}>
          <span style={{ color: '#4ade80' }}>{r.passed_cases}</span>
          <span style={{ color: '#8b949e' }}> / </span>
          <span style={{ color: '#f87171' }}>{r.failed_cases}</span>
          <span style={{ color: '#8b949e' }}> / </span>
          <span style={{ color: '#9ca3af' }}>{r.total_cases}</span>
        </span>
      ),
    },
    {
      title: '耗時',
      dataIndex: 'duration_ms',
      width: 80,
      render: (v) => (
        <Text style={{ fontSize: 13, color: '#8b949e' }}>{(v / 1000).toFixed(1)}s</Text>
      ),
    },
    {
      title: '',
      width: 80,
      render: (_, r) => (
        <Button
          type="link"
          size="small"
          icon={<EyeOutlined />}
          onClick={() => navigate(`/reports/${r.id}`)}
        >
          詳細
        </Button>
      ),
    },
  ]

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 12,
        }}
      >
        <h3 style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', margin: 0 }}>
          歷史執行紀錄
        </h3>
        <Button
          size="small"
          type="text"
          icon={<ReloadOutlined />}
          style={{ color: '#8b949e' }}
          onClick={() => fetchReports(page)}
        >
          重新整理
        </Button>
      </div>

      <Table
        dataSource={reports}
        columns={columns}
        rowKey="id"
        loading={loading}
        size="small"
        pagination={{
          current: page,
          total,
          pageSize: 10,
          onChange: (p) => fetchReports(p),
          showSizeChanger: false,
          showTotal: (t) => `共 ${t} 筆`,
          size: 'small',
        }}
      />
    </div>
  )
}
