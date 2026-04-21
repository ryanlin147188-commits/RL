import { useState, useEffect } from 'react'
import { Table, Select, Input, Button, Tooltip, Space, Popconfirm } from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
} from '@ant-design/icons'
import useStore from '../../store/useStore'

// ── 下拉選項常數 ─────────────────────────────────────

const BDD_KEYWORDS = ['Given', 'When', 'Then', 'And', 'But']

const ACTIONS = [
  'Navigate',
  'Click',
  'Fill',
  'SelectOption',
  'VerifyText',
  'VerifyVisible',
  'VerifyURL',
  'VerifyAttribute',
  'Call_API',
  'Wait',
  'Screenshot',
  'ScrollTo',
].map((v) => ({ label: v, value: v }))

const CONDITIONS = [
  { label: 'Equals', value: 'Equals' },
  { label: 'Contains', value: 'Contains' },
  { label: 'NotEquals', value: 'NotEquals' },
  { label: 'NotContains', value: 'NotContains' },
  { label: 'StartsWith', value: 'StartsWith' },
  { label: 'EndsWith', value: 'EndsWith' },
  { label: 'IsVisible', value: 'IsVisible' },
  { label: 'IsEnabled', value: 'IsEnabled' },
  { label: 'IsDisabled', value: 'IsDisabled' },
  { label: 'GreaterThan', value: 'GreaterThan' },
  { label: 'LessThan', value: 'LessThan' },
  { label: '（略）', value: '' },
]

// ── 防抖輸入框元件 ────────────────────────────────────
/**
 * 本地維護輸入值，僅在 onBlur（失焦）時才通知父元件更新 Store。
 * 避免每個按鍵觸發全局重新渲染（尤其是大型步驟表格時）。
 *
 * @param {string}   value     外部（Store）的值
 * @param {string}   placeholder
 * @param {Function} onCommit  失焦時以最終值呼叫
 */
function BlurInput({ value, placeholder, onCommit }) {
  const [local, setLocal] = useState(value ?? '')

  // 切換測試案例（外部值重置）時同步本地值
  useEffect(() => {
    setLocal(value ?? '')
  }, [value])

  const input = (
    <Input
      value={local}
      size="small"
      placeholder={placeholder}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        if (local !== (value ?? '')) onCommit(local)
      }}
      style={{ fontSize: 12 }}
    />
  )
  // 滑鼠移上去時顯示完整內容（避免欄位過窄時看起來被截斷，例如
  // role=heading[name="..."] 這類較長的 Locator）
  return local ? (
    <Tooltip
      title={<span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{local}</span>}
      placement="topLeft"
      mouseEnterDelay={0.4}
    >
      {input}
    </Tooltip>
  ) : (
    input
  )
}

// ── StepTable 主元件 ──────────────────────────────────
export default function StepTable() {
  const { testcaseContent, addStep, updateStep, deleteStep, moveStep } = useStore()
  const steps = testcaseContent?.steps_json ?? []

  const columns = [
    {
      title: '#',
      width: 36,
      align: 'center',
      render: (_, __, idx) => (
        <span style={{ color: '#8b949e', fontSize: 11, userSelect: 'none' }}>{idx + 1}</span>
      ),
    },
    {
      title: 'BDD 關鍵字',
      dataIndex: 'keyword',
      width: 108,
      render: (val, _, idx) => (
        <Select
          value={val}
          size="small"
          style={{ width: '100%', fontSize: 12 }}
          options={BDD_KEYWORDS.map((k) => ({ label: k, value: k }))}
          onChange={(v) => updateStep(idx, 'keyword', v)}
        />
      ),
    },
    {
      title: '步驟描述',
      dataIndex: 'description',
      width: 220,
      render: (val, _, idx) => (
        <BlurInput
          value={val}
          placeholder="說明此步驟的意圖..."
          onCommit={(v) => updateStep(idx, 'description', v)}
        />
      ),
    },
    {
      title: 'Action',
      dataIndex: 'action',
      width: 130,
      render: (val, _, idx) => (
        <Select
          value={val}
          size="small"
          style={{ width: '100%', fontSize: 12 }}
          options={ACTIONS}
          onChange={(v) => updateStep(idx, 'action', v)}
        />
      ),
    },
    {
      title: 'Locator',
      dataIndex: 'locator',
      width: 280,
      render: (val, _, idx) => (
        <BlurInput
          value={val}
          placeholder="#id / .class / role=... / /api/path"
          onCommit={(v) => updateStep(idx, 'locator', v)}
        />
      ),
    },
    {
      title: 'Input',
      dataIndex: 'input',
      width: 180,
      render: (val, _, idx) => (
        <BlurInput
          value={val}
          placeholder="值 或 ${變數名稱}"
          onCommit={(v) => updateStep(idx, 'input', v)}
        />
      ),
    },
    {
      title: 'Condition',
      dataIndex: 'condition',
      width: 130,
      render: (val, _, idx) => (
        <Select
          value={val}
          size="small"
          style={{ width: '100%', fontSize: 12 }}
          options={CONDITIONS}
          onChange={(v) => updateStep(idx, 'condition', v)}
        />
      ),
    },
    {
      title: 'Expected',
      dataIndex: 'expected',
      width: 200,
      render: (val, _, idx) => (
        <BlurInput
          value={val}
          placeholder="預期結果..."
          onCommit={(v) => updateStep(idx, 'expected', v)}
        />
      ),
    },
    {
      title: '',
      width: 72,
      render: (_, __, idx) => (
        <Space size={0}>
          <Tooltip title="上移">
            <Button
              type="text"
              size="small"
              icon={<ArrowUpOutlined />}
              disabled={idx === 0}
              onClick={() => moveStep(idx, idx - 1)}
              style={{ color: '#8b949e' }}
            />
          </Tooltip>
          <Tooltip title="下移">
            <Button
              type="text"
              size="small"
              icon={<ArrowDownOutlined />}
              disabled={idx === steps.length - 1}
              onClick={() => moveStep(idx, idx + 1)}
              style={{ color: '#8b949e' }}
            />
          </Tooltip>
          <Popconfirm
            title="確認刪除此步驟？"
            onConfirm={() => deleteStep(idx)}
            okText="刪除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <section>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <h3 style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', margin: 0 }}>
          🪜 測試步驟（{steps.length} 步）
        </h3>
        <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addStep}>
          新增步驟
        </Button>
      </div>

      <Table
        dataSource={steps}
        columns={columns}
        rowKey="id"
        pagination={false}
        size="small"
        scroll={{ x: 1380 }}
        locale={{ emptyText: '尚無步驟，點擊「新增步驟」開始建立' }}
        style={{ fontSize: 12 }}
      />
    </section>
  )
}
