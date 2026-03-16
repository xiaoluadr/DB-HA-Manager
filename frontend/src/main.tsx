import React, { useEffect } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import './styles/global.css'

import App from './App'
import { useSettingsStore } from '@/store/settings'

// 设置 dayjs 中文
dayjs.locale('zh-cn')

// 自定义深色主题
const darkTheme = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: '#ff4d6d',
    colorSuccess: '#00e87a',
    colorWarning: '#ff9500',
    colorError: '#ff3b5c',
    colorInfo: '#9b6bff',
    colorBgBase: '#07090f',
    colorBgContainer: '#0c1118',
    colorBgElevated: '#111827',
    colorBgLayout: '#07090f',
    colorBorder: '#1a2535',
    colorBorderSecondary: '#243346',
    colorText: '#dde6f0',
    colorTextSecondary: '#8899aa',
    colorTextTertiary: '#445566',
    colorTextQuaternary: '#2d3f50',
    borderRadius: 10,
    fontFamily: `'Noto Sans SC', sans-serif`,
  },
}

const lightTheme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#ff4d6d',
    colorSuccess: '#0f9154',
    colorWarning: '#fa8c16',
    colorError: '#ff3b5c',
    colorInfo: '#845ef7',
    colorBgBase: '#f5f7fb',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#f5f7fb',
    colorBorder: '#e5e8f0',
    colorBorderSecondary: '#f1f3f5',
    colorText: '#1f2430',
    colorTextSecondary: '#475467',
    colorTextTertiary: '#667085',
    colorTextQuaternary: '#98a2b3',
    borderRadius: 10,
    fontFamily: `'Noto Sans SC', sans-serif`,
  },
}

function RootApp() {
  const themeMode = useSettingsStore((state) => state.theme)

  useEffect(() => {
    document.body.setAttribute('data-theme', themeMode)
  }, [themeMode])

  const themeConfig = themeMode === 'dark' ? darkTheme : lightTheme

  return (
    <ConfigProvider theme={themeConfig}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RootApp />
  </React.StrictMode>,
)
