import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import * as api from '../api/client'

// ── 工具函式 ──────────────────────────────────────────

/**
 * 遞迴將後端樹狀節點轉換為 Ant Design Tree 的 DataNode 格式
 * 後端格式：{ id, name, level_type, children: [...] }
 * AntD 格式：{ key, title, levelType, isLeaf, children: [...] }
 */
function transformTree(nodes = []) {
  return nodes.map((node) => ({
    key: node.id,
    title: node.name,
    levelType: node.level_type,
    isLeaf: node.level_type === 'TESTCASE',
    children: node.children ? transformTree(node.children) : [],
  }))
}

/** 確保 testcaseContent 的 steps / ddt 有預設空值，避免下游 null 解構 */
function normalizeContent(data) {
  return {
    node_id: data.node_id,
    ac_text: data.ac_text ?? '',
    setup_text: data.setup_text ?? '',
    steps_json: Array.isArray(data.steps_json) ? data.steps_json : [],
    ddt_json:
      data.ddt_json && Array.isArray(data.ddt_json.headers)
        ? data.ddt_json
        : { headers: [], rows: [] },
  }
}

/** 建立一個空的步驟物件（crypto.randomUUID 為現代瀏覽器原生 API）*/
function createEmptyStep() {
  return {
    id: crypto.randomUUID(),
    keyword: 'When',
    description: '',
    action: 'Click',
    locator: '',
    input: '',
    condition: 'Equals',
    expected: '',
  }
}

// ═══════════════════════════════════════════════════════
// Zustand Store（搭配 devtools 方便 Redux DevTools 除錯）
// ═══════════════════════════════════════════════════════
const useStore = create(
  devtools(
    (set, get) => ({
      // ─────────────────────────────────────────────────
      // 📦 State
      // ─────────────────────────────────────────────────

      /** 專案列表 */
      projects: [],
      projectsLoading: false,

      /** 當前選中的專案 */
      currentProject: null,

      /** Ant Design Tree 資料（已轉換格式）*/
      treeData: [],
      treeLoading: false,

      /** Tree 展開的節點 keys */
      expandedKeys: [],

      /** Tree 被選中的節點 keys（最多一個）*/
      selectedKeys: [],

      /** 當前選中的節點 DataNode（來自 Tree） */
      activeNode: null,

      /** 測試案例內容 { node_id, ac_text, steps_json, ddt_json } */
      testcaseContent: null,
      contentLoading: false,
      contentSaving: false,

      /** 是否有未儲存的變更 */
      isDirty: false,

      // ─────────────────────────────────────────────────
      // 🔧 Actions — 專案
      // ─────────────────────────────────────────────────

      /** 拉取所有專案，並自動選中第一個 */
      fetchProjects: async () => {
        set({ projectsLoading: true })
        try {
          const data = await api.getProjects()
          set({ projects: data, projectsLoading: false })
          if (!get().currentProject && data.length > 0) {
            await get().setCurrentProject(data[0])
          }
        } catch {
          set({ projectsLoading: false })
        }
      },

      setCurrentProject: async (project) => {
        set({ currentProject: project, activeNode: null, testcaseContent: null, isDirty: false })
        if (project) {
          await get().fetchTree(project.id)
        }
      },

      /** 建立新專案，建立後自動切換為當前專案 */
      createProject: async (name) => {
        const created = await api.createProject({ name })
        set((s) => ({ projects: [...s.projects, created] }))
        await get().setCurrentProject(created)
        return created
      },

      // ─────────────────────────────────────────────────
      // 🔧 Actions — 目錄樹
      // ─────────────────────────────────────────────────

      fetchTree: async (projectId) => {
        set({ treeLoading: true })
        try {
          const root = await api.getProjectTree(projectId)
          const transformed = transformTree(root.children ?? [])
          set({ treeData: transformed, treeLoading: false })
        } catch {
          set({ treeLoading: false })
        }
      },

      setExpandedKeys: (keys) => set({ expandedKeys: keys }),

      /**
       * 選中樹節點：設定 activeNode，並在為 TESTCASE 層級時載入內容
       */
      selectNode: async (node) => {
        set({ activeNode: node, selectedKeys: [node.key] })
        if (node.levelType === 'TESTCASE') {
          await get().fetchTestcase(node.key)
        } else {
          set({ testcaseContent: null, isDirty: false })
        }
      },

      addNode: async (parentId, nodeData) => {
        await api.createNode({ parent_id: parentId, ...nodeData })
        await get().fetchTree(get().currentProject.id)
      },

      patchNode: async (nodeId, data) => {
        await api.patchNode(nodeId, data)
        await get().fetchTree(get().currentProject.id)
      },

      deleteNode: async (nodeId) => {
        await api.deleteNode(nodeId)
        if (get().activeNode?.key === nodeId) {
          set({ activeNode: null, testcaseContent: null, isDirty: false })
        }
        await get().fetchTree(get().currentProject.id)
      },

      // ─────────────────────────────────────────────────
      // 🔧 Actions — 測試案例內容
      // ─────────────────────────────────────────────────

      fetchTestcase: async (nodeId) => {
        set({ contentLoading: true })
        try {
          const data = await api.getTestcase(nodeId)
          set({ testcaseContent: normalizeContent(data), contentLoading: false, isDirty: false })
        } catch {
          set({ contentLoading: false })
        }
      },

      /** 更新驗收準則（ATDD 文字）*/
      updateAcText: (text) =>
        set((s) => ({
          testcaseContent: { ...s.testcaseContent, ac_text: text },
          isDirty: true,
        })),

      /** 更新前置動作（Pre-Setup 文字）*/
      updateSetupText: (text) =>
        set((s) => ({
          testcaseContent: { ...s.testcaseContent, setup_text: text },
          isDirty: true,
        })),

      // ── 步驟表格 ───────────────────────────────────

      addStep: () =>
        set((s) => ({
          testcaseContent: {
            ...s.testcaseContent,
            steps_json: [...(s.testcaseContent?.steps_json ?? []), createEmptyStep()],
          },
          isDirty: true,
        })),

      updateStep: (index, field, value) =>
        set((s) => {
          const steps = [...(s.testcaseContent?.steps_json ?? [])]
          steps[index] = { ...steps[index], [field]: value }
          return { testcaseContent: { ...s.testcaseContent, steps_json: steps }, isDirty: true }
        }),

      deleteStep: (index) =>
        set((s) => {
          const steps = [...(s.testcaseContent?.steps_json ?? [])]
          steps.splice(index, 1)
          return { testcaseContent: { ...s.testcaseContent, steps_json: steps }, isDirty: true }
        }),

      /** 上移 / 下移步驟 */
      moveStep: (fromIndex, toIndex) =>
        set((s) => {
          const steps = [...(s.testcaseContent?.steps_json ?? [])]
          const [item] = steps.splice(fromIndex, 1)
          steps.splice(toIndex, 0, item)
          return { testcaseContent: { ...s.testcaseContent, steps_json: steps }, isDirty: true }
        }),

      // ── DDT 表格 ───────────────────────────────────

      addDdtColumn: (header) =>
        set((s) => {
          const ddt = s.testcaseContent?.ddt_json ?? { headers: [], rows: [] }
          return {
            testcaseContent: {
              ...s.testcaseContent,
              ddt_json: {
                headers: [...ddt.headers, header],
                rows: ddt.rows.map((row) => [...row, '']),
              },
            },
            isDirty: true,
          }
        }),

      deleteDdtColumn: (colIndex) =>
        set((s) => {
          const ddt = s.testcaseContent?.ddt_json ?? { headers: [], rows: [] }
          return {
            testcaseContent: {
              ...s.testcaseContent,
              ddt_json: {
                headers: ddt.headers.filter((_, i) => i !== colIndex),
                rows: ddt.rows.map((row) => row.filter((_, i) => i !== colIndex)),
              },
            },
            isDirty: true,
          }
        }),

      addDdtRow: () =>
        set((s) => {
          const ddt = s.testcaseContent?.ddt_json ?? { headers: [], rows: [] }
          return {
            testcaseContent: {
              ...s.testcaseContent,
              ddt_json: { ...ddt, rows: [...ddt.rows, new Array(ddt.headers.length).fill('')] },
            },
            isDirty: true,
          }
        }),

      deleteDdtRow: (rowIndex) =>
        set((s) => {
          const ddt = s.testcaseContent?.ddt_json ?? { headers: [], rows: [] }
          return {
            testcaseContent: {
              ...s.testcaseContent,
              ddt_json: { ...ddt, rows: ddt.rows.filter((_, i) => i !== rowIndex) },
            },
            isDirty: true,
          }
        }),

      updateDdtCell: (rowIndex, colIndex, value) =>
        set((s) => {
          const ddt = s.testcaseContent?.ddt_json ?? { headers: [], rows: [] }
          const rows = ddt.rows.map((row, ri) =>
            ri === rowIndex ? row.map((cell, ci) => (ci === colIndex ? value : cell)) : row,
          )
          return {
            testcaseContent: { ...s.testcaseContent, ddt_json: { ...ddt, rows } },
            isDirty: true,
          }
        }),

      /**
       * 從 JSON 檔案匯入 DDT（解析後直接更新本地 Store，不自動呼叫 API）
       * 使用者仍需手動點「儲存」才會持久化至後端
       */
      importDdt: (ddtData) =>
        set((s) => ({
          testcaseContent: { ...s.testcaseContent, ddt_json: ddtData },
          isDirty: true,
        })),

      /** 整包儲存至後端（ac_text + steps_json + ddt_json）*/
      saveTestcase: async () => {
        const { activeNode, testcaseContent } = get()
        if (!activeNode || !testcaseContent) return
        set({ contentSaving: true })
        try {
          await api.saveTestcase(activeNode.key, {
            ac_text: testcaseContent.ac_text,
            setup_text: testcaseContent.setup_text,
            steps_json: testcaseContent.steps_json,
            ddt_json: testcaseContent.ddt_json,
          })
          set({ contentSaving: false, isDirty: false })
        } catch (e) {
          set({ contentSaving: false })
          throw e
        }
      },
    }),
    { name: 'AutoTestStore' },
  ),
)

export default useStore
