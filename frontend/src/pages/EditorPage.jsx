import EditorPanel from '../components/Editor/EditorPanel'

/** 測試案例編輯頁面（路由：/editor）*/
export default function EditorPage() {
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', height: '100%' }}>
      <EditorPanel />
    </div>
  )
}
