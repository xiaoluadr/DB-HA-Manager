import { useEffect, useState } from 'react'
import { Layout, Input } from 'antd'
import {
  GithubOutlined,
  SearchOutlined,
  BellOutlined,
  SettingOutlined,
  FullscreenOutlined,
  LogoutOutlined,
  CloseCircleFilled,
} from '@ant-design/icons'
import { useClusterStore } from '@/store'
import { useSettingsStore, type Environment } from '@/store/settings'

const { Header } = Layout

const ENVIRONMENT_BADGE_STYLES: Record<
  Environment,
  { background: string; color: string; border: string; label: string }
> = {
  PROD: {
    background: 'var(--red-dim)',
    color: 'var(--red)',
    border: '1px solid rgba(255, 59, 92, 0.3)',
    label: 'PROD',
  },
  DEV: {
    background: 'var(--purple-dim)',
    color: 'var(--purple)',
    border: '1px solid rgba(155, 107, 255, 0.3)',
    label: 'DEV',
  },
  TEST: {
    background: 'var(--orange-dim)',
    color: 'var(--orange)',
    border: '1px solid rgba(255, 149, 0, 0.3)',
    label: 'TEST',
  },
}

function AppHeader() {
  const { searchKeyword, setSearchKeyword } = useClusterStore()
  const [searchValue, setSearchValue] = useState(searchKeyword)
  const [notifications] = useState(5)
  const environment = useSettingsStore((state) => state.environment)

  useEffect(() => {
    setSearchValue(searchKeyword)
  }, [searchKeyword])

  useEffect(() => {
    const handle = setTimeout(() => {
      const trimmed = searchValue.trim()
      if (trimmed !== searchKeyword) {
        setSearchKeyword(trimmed)
      }
    }, 250)

    return () => clearTimeout(handle)
  }, [searchValue, searchKeyword, setSearchKeyword])

  const handleSearchChange = (value: string) => {
    setSearchValue(value)
  }

  const clearSearch = () => {
    setSearchValue('')
    setSearchKeyword('')
  }

  return (
    <Header
      style={{
        display: 'flex',
        alignItems: 'center',
        background: 'var(--bg2)',
        borderBottom: '1px solid var(--border)',
        padding: '0 20px',
        gap: '12px',
        height: '54px',
      }}
    >
      {/* Breadcrumb */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
        <span
          style={{
            fontFamily: "'Syne', sans-serif",
            fontWeight: 700,
            fontSize: '15px',
            color: 'var(--text)',
            whiteSpace: 'nowrap',
          }}
        >
          DB-HA Manager
        </span>
        <span
          style={{
            fontSize: '10px',
            color: 'var(--text3)',
            fontFamily: "'JetBrains Mono', monospace",
            whiteSpace: 'nowrap',
          }}
        >
          / 仪表盘
        </span>
      </div>

      {/* Spacer */}
      <div style={{ flex: 1, minWidth: 0 }} />

      {/* Search */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '7px',
          background: 'var(--bg3)',
          border: '1px solid var(--border)',
          borderRadius: '8px',
          padding: '6px 12px',
          width: '200px',
          cursor: 'text',
          transition: 'border-color 0.2s',
          flexShrink: 0,
        }}
      >
        <SearchOutlined style={{ fontSize: '14px', color: 'var(--text3)' }} />
        <Input
          value={searchValue}
          onChange={(e) => handleSearchChange(e.target.value)}
          placeholder="搜索集群、任务..."
          bordered={false}
          style={{
            background: 'transparent',
            color: 'var(--text2)',
            fontSize: '12px',
            width: '100%',
            fontFamily: "'JetBrains Mono', monospace",
          }}
        />
        {searchValue && (
          <CloseCircleFilled
            onClick={clearSearch}
            style={{
              fontSize: '12px',
              color: 'var(--text3)',
              cursor: 'pointer',
              transition: 'color 0.2s',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = 'var(--text2)'
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = 'var(--text3)'
            }}
          />
        )}
      </div>

      {/* Action Buttons */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          flexShrink: 0,
        }}
      >
        {/* Environment Badge */}
        <div
          style={{
            padding: '4px 10px',
            borderRadius: '6px',
            fontSize: '10px',
            fontFamily: "'JetBrains Mono', monospace",
            fontWeight: 600,
            letterSpacing: '1px',
            flexShrink: 0,
            background: ENVIRONMENT_BADGE_STYLES[environment].background,
            color: ENVIRONMENT_BADGE_STYLES[environment].color,
            border: ENVIRONMENT_BADGE_STYLES[environment].border,
          }}
        >
          {ENVIRONMENT_BADGE_STYLES[environment].label}
        </div>

        {/* Notification */}
        <div
          style={{
            position: 'relative',
            cursor: 'pointer',
          }}
        >
          <div
            style={{
              width: '32px',
              height: '32px',
              borderRadius: '8px',
              background: 'var(--bg3)',
              border: '1px solid var(--border)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '14px',
              color: 'var(--text2)',
              transition: 'all 0.15s',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = 'var(--primary)'
              e.currentTarget.style.color = 'var(--primary)'
              e.currentTarget.style.background = 'var(--primary-dim)'
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = 'var(--border)'
              e.currentTarget.style.color = 'var(--text2)'
              e.currentTarget.style.background = 'var(--bg3)'
            }}
          >
            <BellOutlined />
          </div>
          {notifications > 0 && (
            <div
              style={{
                position: 'absolute',
                top: '5px',
                right: '5px',
                width: '6px',
                height: '6px',
                borderRadius: '50%',
                background: 'var(--red)',
                border: '1.5px solid var(--bg2)',
              }}
            />
          )}
        </div>

        {/* Settings */}
        <div
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            background: 'var(--bg3)',
            border: '1px solid var(--border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '14px',
            color: 'var(--text2)',
            cursor: 'pointer',
            transition: 'all 0.15s',
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = 'var(--primary)'
            e.currentTarget.style.color = 'var(--primary)'
            e.currentTarget.style.background = 'var(--primary-dim)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = 'var(--border)'
            e.currentTarget.style.color = 'var(--text2)'
            e.currentTarget.style.background = 'var(--bg3)'
          }}
        >
          <SettingOutlined />
        </div>

        {/* Fullscreen */}
        <div
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            background: 'var(--bg3)',
            border: '1px solid var(--border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '14px',
            color: 'var(--text2)',
            cursor: 'pointer',
            transition: 'all 0.15s',
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = 'var(--primary)'
            e.currentTarget.style.color = 'var(--primary)'
            e.currentTarget.style.background = 'var(--primary-dim)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = 'var(--border)'
            e.currentTarget.style.color = 'var(--text2)'
            e.currentTarget.style.background = 'var(--bg3)'
          }}
        >
          <FullscreenOutlined />
        </div>

        {/* GitHub */}
        <a
          href="https://github.com"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            background: 'var(--bg3)',
            border: '1px solid var(--border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '14px',
            color: 'var(--text2)',
            cursor: 'pointer',
            transition: 'all 0.15s',
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = 'var(--primary)'
            e.currentTarget.style.color = 'var(--primary)'
            e.currentTarget.style.background = 'var(--primary-dim)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = 'var(--border)'
            e.currentTarget.style.color = 'var(--text2)'
            e.currentTarget.style.background = 'var(--bg3)'
          }}
        >
          <GithubOutlined />
        </a>

        {/* Logout */}
        <div
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            background: 'var(--red-dim)',
            border: '1px solid rgba(255, 59, 92, 0.3)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '14px',
            color: 'var(--red)',
            cursor: 'pointer',
            transition: 'all 0.15s',
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'rgba(255, 59, 92, 0.18)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'var(--red-dim)'
          }}
        >
          <LogoutOutlined />
        </div>
      </div>
    </Header>
  )
}

export default AppHeader
