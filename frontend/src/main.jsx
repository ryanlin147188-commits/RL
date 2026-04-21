import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, theme as antTheme } from 'antd'
import zhTW from 'antd/locale/zh_TW'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {/* Ant Design 全域設定：深色演算法 + 繁體中文 */}
    <ConfigProvider
      locale={zhTW}
      theme={{
        algorithm: antTheme.darkAlgorithm,
        token: {
          colorPrimary: '#1677ff',
          colorBgContainer: '#161b22',
          colorBgElevated: '#1c2128',
          colorBorder: '#30363d',
          borderRadius: 6,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang TC', 'Microsoft JhengHei', sans-serif",
        },
        components: {
          Layout: { headerBg: '#161b22', siderBg: '#161b22', bodyBg: '#0d1117' },
          Table: { headerBg: '#1c2128', rowHoverBg: 'rgba(22,119,255,0.06)' },
          Tree: { nodeSelectedBg: 'rgba(22,119,255,0.22)', nodeHoverBg: 'rgba(22,119,255,0.12)' },
          Modal: { contentBg: '#1c2128', headerBg: '#1c2128' },
          Card: { colorBgContainer: '#161b22' },
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
)
