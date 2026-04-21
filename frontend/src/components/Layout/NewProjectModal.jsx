import { useState } from 'react'
import { Modal, Form, Input, message } from 'antd'
import useStore from '../../store/useStore'

/**
 * 新建專案 Modal
 * - 受控顯示（open + onClose）
 * - 提交後呼叫 store.createProject，成功則自動切換
 */
export default function NewProjectModal({ open, onClose }) {
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const { createProject } = useStore()

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      setSubmitting(true)
      const created = await createProject(values.name.trim())
      message.success(`已建立專案「${created.name}」`)
      form.resetFields()
      onClose()
    } catch (e) {
      if (e?.errorFields) return // antd 表單驗證錯誤，已自動標示
      message.error(`建立失敗：${e.message}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="新建專案"
      open={open}
      onOk={handleOk}
      onCancel={() => {
        form.resetFields()
        onClose()
      }}
      confirmLoading={submitting}
      okText="建立"
      cancelText="取消"
      destroyOnClose
    >
      <Form form={form} layout="vertical" preserve={false}>
        <Form.Item
          name="name"
          label="專案名稱"
          rules={[
            { required: true, message: '請輸入專案名稱' },
            { max: 100, message: '名稱長度不可超過 100 字' },
            {
              validator: (_, v) =>
                v && v.trim().length === 0
                  ? Promise.reject(new Error('名稱不能為空白'))
                  : Promise.resolve(),
            },
          ]}
        >
          <Input placeholder="例如：電商平台 E2E 測試" autoFocus maxLength={100} showCount />
        </Form.Item>
      </Form>
    </Modal>
  )
}
