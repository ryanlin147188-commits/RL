import { useState } from 'react'
import {
  Tree,
  Button,
  Input,
  Select,
  Modal,
  Spin,
  Empty,
  Tooltip,
  Space,
  Popconfirm,
  message,
} from 'antd'
import {
  FolderOutlined,
  FolderOpenOutlined,
  FileTextOutlined,
  PlusOutlined,
  DeleteOutlined,
  ProjectOutlined,
  AppstoreOutlined,
  DesktopOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import useStore from '../../store/useStore'

// ── 層級對應圖示 ────────────────────────────────────
const LEVEL_ICON = {
  PROJECT: <ProjectOutlined style={{ color: '#60a5fa' }} />,
  FEATURE: <FolderOutlined style={{ color: '#fbbf24' }} />,
  PLATFORM: <AppstoreOutlined style={{ color: '#c084fc' }} />,
  PAGE: <DesktopOutlined style={{ color: '#4ade80' }} />,
  SCENARIO: <ExperimentOutlined style={{ color: '#fb923c' }} />,
  TESTCASE: <FileTextOutlined style={{ color: '#67e8f9' }} />,
}

/** 每個層級允許新增的子層級 */
const CHILD_LEVEL = {
  PROJECT: 'FEATURE',
  FEATURE: 'PLATFORM',
  PLATFORM: 'PAGE',
  PAGE: 'SCENARIO',
  SCENARIO: 'TESTCASE',
}

/** 特定層級使用固定選項（不允許自由輸入）*/
const FIXED_OPTIONS = {
  PLATFORM: ['WEB', 'API', 'APP'],
  SCENARIO: ['正向', '反向', '邊界'],
}

/** 部分層級有強制前綴 */
const NAME_PREFIX = {
  FEATURE: '功能：',
  PAGE: '頁面：',
}

export default function ProjectTree() {
  const {
    treeData,
    treeLoading,
    expandedKeys,
    selectedKeys,
    currentProject,
    setExpandedKeys,
    selectNode,
    addNode,
    deleteNode,
  } = useStore()

  const [addModal, setAddModal] = useState({ open: false, parentNode: null })
  const [newName, setNewName] = useState('')

  /* ── 新增節點表單內容 ──────────────────────────── */
  const childLevel = addModal.parentNode ? CHILD_LEVEL[addModal.parentNode.levelType] : null
  const fixedOptions = childLevel ? FIXED_OPTIONS[childLevel] : null
  const prefix = childLevel ? NAME_PREFIX[childLevel] : ''

  const openAddModal = (parentNode) => {
    setAddModal({ open: true, parentNode })
    setNewName('')
  }

  const handleAddConfirm = async () => {
    if (!newName.trim()) return
    const rawName = newName.trim()
    // TESTCASE 層級強制加 .md 副檔名
    const finalName =
      childLevel === 'TESTCASE' && !rawName.endsWith('.md') ? `${rawName}.md` : rawName

    try {
      await addNode(addModal.parentNode.key, {
        project_id: currentProject.id,
        name: finalName,
        level_type: childLevel,
      })
      message.success(`已新增：${finalName}`)
      setAddModal({ open: false, parentNode: null })
    } catch (e) {
      message.error(`新增失敗：${e.message}`)
    }
  }

  /* ── 自訂 Tree 節點 Title（加圖示 + 懸停操作）── */
  const renderTitle = (nodeData) => {
    const icon = LEVEL_ICON[nodeData.levelType] ?? <FileTextOutlined />
    const canAddChild = !!CHILD_LEVEL[nodeData.levelType]

    return (
      <span
        className="group flex items-center gap-1.5 w-full pr-1 select-none"
        style={{ minWidth: 0 }}
      >
        {icon}
        <span className="flex-1 truncate text-sm" style={{ color: '#e6edf3' }}>
          {nodeData.title}
        </span>

        {/* 懸停時顯示操作按鈕 */}
        <span className="hidden group-hover:flex items-center gap-0.5 shrink-0">
          {canAddChild && (
            <Tooltip title={`新增 ${CHILD_LEVEL[nodeData.levelType]}`}>
              <Button
                type="text"
                size="small"
                icon={<PlusOutlined />}
                style={{ color: '#8b949e', padding: '0 4px', height: 20 }}
                onClick={(e) => {
                  e.stopPropagation()
                  openAddModal(nodeData)
                }}
              />
            </Tooltip>
          )}
          {/* 頂層 PROJECT 節點不允許刪除 */}
          {nodeData.levelType !== 'PROJECT' && (
            <Popconfirm
              title={`確定刪除「${nodeData.title}」？`}
              description="此操作會連帶刪除所有子節點與測試案例，不可復原。"
              onConfirm={async (e) => {
                e?.stopPropagation()
                try {
                  await deleteNode(nodeData.key)
                  message.success('已刪除')
                } catch (err) {
                  message.error(`刪除失敗：${err.message}`)
                }
              }}
              okText="確認刪除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button
                type="text"
                size="small"
                danger
                icon={<DeleteOutlined />}
                style={{ padding: '0 4px', height: 20 }}
                onClick={(e) => e.stopPropagation()}
              />
            </Popconfirm>
          )}
        </span>
      </span>
    )
  }

  /* ── 新增節點 Modal 的輸入區域 ─────────────────── */
  const renderAddInput = () => {
    if (!childLevel) return null
    if (fixedOptions) {
      return (
        <Select
          className="w-full"
          placeholder={`選擇 ${childLevel}...`}
          options={fixedOptions.map((v) => ({ label: v, value: v }))}
          onChange={(v) => setNewName(v)}
          autoFocus
        />
      )
    }
    return (
      <Input
        prefix={prefix ? <span style={{ color: '#8b949e' }}>{prefix}</span> : null}
        suffix={childLevel === 'TESTCASE' ? <span style={{ color: '#8b949e' }}>.md</span> : null}
        placeholder={`輸入${childLevel}名稱...`}
        value={newName.startsWith(prefix) ? newName.slice(prefix.length) : newName}
        onChange={(e) => setNewName(prefix + e.target.value)}
        onPressEnter={handleAddConfirm}
        autoFocus
      />
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* ── 工具列 ── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '6px 12px',
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <span style={{ fontSize: 11, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1 }}>
          目錄樹
        </span>
        {currentProject && (
          <Tooltip title="新增功能模組">
            <Button
              type="text"
              size="small"
              icon={<PlusOutlined />}
              style={{ color: '#8b949e' }}
              onClick={() =>
                openAddModal({
                  key: currentProject.id,
                  levelType: 'PROJECT',
                  title: currentProject.name,
                })
              }
            />
          </Tooltip>
        )}
      </div>

      {/* ── 樹狀區域 ── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 4px' }}>
        {treeLoading ? (
          <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 32 }}>
            <Spin />
          </div>
        ) : treeData.length === 0 ? (
          <Empty
            description={
              <span style={{ color: '#8b949e', fontSize: 13 }}>
                {currentProject ? '尚無節點，點擊 + 新增' : '請先選擇專案'}
              </span>
            }
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            style={{ marginTop: 32 }}
          />
        ) : (
          <Tree
            treeData={treeData}
            titleRender={renderTitle}
            expandedKeys={expandedKeys}
            selectedKeys={selectedKeys}
            onExpand={(keys) => setExpandedKeys(keys)}
            onSelect={(_, { node }) => selectNode(node)}
            showLine={{ showLeafIcon: false }}
            blockNode
            style={{ background: 'transparent' }}
          />
        )}
      </div>

      {/* ── 新增節點 Modal ── */}
      <Modal
        title={`新增 ${childLevel ?? ''}`}
        open={addModal.open}
        onOk={handleAddConfirm}
        onCancel={() => setAddModal({ open: false, parentNode: null })}
        okText="新增"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ paddingTop: 8 }}>
          <p style={{ color: '#8b949e', fontSize: 13, marginBottom: 12 }}>
            父節點：<strong style={{ color: '#e6edf3' }}>{addModal.parentNode?.title}</strong>
          </p>
          {renderAddInput()}
        </div>
      </Modal>
    </div>
  )
}
