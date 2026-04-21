import { useState, useEffect } from 'react'
import { Table, Button, Input, Modal, Upload, Space, Tooltip, Popconfirm, message } from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  UploadOutlined,
  PlusCircleOutlined,
} from '@ant-design/icons'
import useStore from '../../store/useStore'

/** 防抖儲存格輸入框（onBlur 提交，避免頻繁更新 Store）*/
function CellInput({ value, onCommit }) {
  const [local, setLocal] = useState(value ?? '')

  useEffect(() => {
    setLocal(value ?? '')
  }, [value])

  return (
    <Input
      value={local}
      size="small"
      variant="borderless"
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        if (local !== (value ?? '')) onCommit(local)
      }}
      style={{ fontSize: 12, padding: '2px 4px' }}
    />
  )
}

export default function DdtTable() {
  const {
    testcaseContent,
    addDdtColumn,
    deleteDdtColumn,
    addDdtRow,
    deleteDdtRow,
    updateDdtCell,
    importDdt,
  } = useStore()

  const ddt = testcaseContent?.ddt_json ?? { headers: [], rows: [] }
  const { headers, rows } = ddt

  const [colModalOpen, setColModalOpen] = useState(false)
  const [newColName, setNewColName] = useState('')

  const handleAddColumn = () => {
    if (!newColName.trim()) return
    addDdtColumn(newColName.trim())
    setNewColName('')
    setColModalOpen(false)
  }

  /** 讀取 JSON 檔案並解析成 DDT 格式 */
  const handleImportJson = (file) => {
    const reader = new FileReader()
    reader.onload = (e) => {
      try {
        const parsed = JSON.parse(e.target.result)
        if (!Array.isArray(parsed) || parsed.length === 0) {
          message.error('格式錯誤：請提供非空的 JSON 陣列，例如 [{"username":"admin","pwd":"123"}]')
          return
        }
        const importHeaders = Object.keys(parsed[0])
        const importRows = parsed.map((obj) =>
          importHeaders.map((k) => (obj[k] !== undefined ? String(obj[k]) : '')),
        )
        importDdt({ headers: importHeaders, rows: importRows })
        message.success(`✅ 已匯入 ${importRows.length} 筆資料，${importHeaders.length} 個變數`)
      } catch (err) {
        message.error(`JSON 解析失敗：${err.message}`)
      }
    }
    reader.readAsText(file)
    return false // 阻止 Upload 元件自動發送請求
  }

  // ── 動態欄位定義 ────────────────────────────────────
  const columns = [
    // 序號欄
    {
      title: '#',
      width: 36,
      align: 'center',
      render: (_, __, rowIdx) => (
        <span style={{ color: '#8b949e', fontSize: 11 }}>{rowIdx + 1}</span>
      ),
    },
    // 每個 DDT 變數對應一欄
    ...headers.map((header, colIdx) => ({
      key: `col-${colIdx}`,
      width: 140,
      title: (
        // 欄位標題：顯示變數名稱，懸停時出現刪除按鈕
        <div className="group flex items-center gap-1">
          <code style={{ fontSize: 11, color: '#67e8f9' }}>${'{' + header + '}'}</code>
          <Popconfirm
            title={`刪除變數「${header}」？`}
            onConfirm={() => deleteDdtColumn(colIdx)}
            okText="刪除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              className="hidden group-hover:inline-flex"
              style={{ padding: '0 2px', height: 16, fontSize: 10 }}
              onClick={(e) => e.stopPropagation()}
            />
          </Popconfirm>
        </div>
      ),
      render: (_, __, rowIdx) => (
        <CellInput
          value={rows[rowIdx]?.[colIdx] ?? ''}
          onCommit={(v) => updateDdtCell(rowIdx, colIdx, v)}
        />
      ),
    })),
    // 刪除列按鈕欄
    {
      key: 'del-row',
      title: '',
      width: 36,
      render: (_, __, rowIdx) => (
        <Popconfirm
          title="刪除此列？"
          onConfirm={() => deleteDdtRow(rowIdx)}
          okText="刪除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
        >
          <Button type="text" size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <section>
      {/* ── 標題列 ── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <h3 style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', margin: 0 }}>
          📊 DDT 測試資料集（{rows.length} 筆 × {headers.length} 變數）
        </h3>
        <Space size={4}>
          {/* 匯入 JSON */}
          <Upload accept=".json" showUploadList={false} beforeUpload={handleImportJson}>
            <Button size="small" icon={<UploadOutlined />}>
              匯入 JSON
            </Button>
          </Upload>
          {/* 新增變數欄位 */}
          <Button
            size="small"
            type="dashed"
            icon={<PlusCircleOutlined />}
            onClick={() => {
              setNewColName('')
              setColModalOpen(true)
            }}
          >
            新增變數
          </Button>
          {/* 新增資料列 */}
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            onClick={addDdtRow}
            disabled={headers.length === 0}
          >
            新增列
          </Button>
        </Space>
      </div>

      {/* ── 資料表格 ── */}
      <Table
        // dataSource 只需 rowIndex，實際值由 render closure 存取 rows[]
        dataSource={rows.map((_, i) => ({ _key: i }))}
        columns={columns}
        rowKey="_key"
        pagination={false}
        size="small"
        scroll={{ x: Math.max(480, headers.length * 150 + 120) }}
        locale={{ emptyText: '尚無資料。點擊「匯入 JSON」或先「新增變數」再手動輸入' }}
        style={{ fontSize: 12 }}
      />

      {/* ── 新增變數欄位 Modal ── */}
      <Modal
        title="新增測試變數"
        open={colModalOpen}
        onOk={handleAddColumn}
        onCancel={() => setColModalOpen(false)}
        okText="新增"
        cancelText="取消"
        destroyOnClose
      >
        <p style={{ fontSize: 13, color: '#8b949e', marginBottom: 12 }}>
          在步驟 Input 欄位中，可以用{' '}
          <code style={{ color: '#67e8f9' }}>${'{變數名稱}'}</code> 引用此變數。
        </p>
        <Input
          value={newColName}
          onChange={(e) => setNewColName(e.target.value)}
          onPressEnter={handleAddColumn}
          placeholder="例：username（不須加 ${}）"
          addonBefore="${"
          addonAfter="}"
          autoFocus
        />
      </Modal>
    </section>
  )
}
