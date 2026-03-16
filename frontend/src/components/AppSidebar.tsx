import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Layout, Badge } from 'antd'
import {
  DashboardOutlined,
  PlusSquareOutlined,
  HistoryOutlined,
  SettingOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  DatabaseOutlined,
} from '@ant-design/icons'
import { useClusterStore } from '@/store'
import { useSettingsStore } from '@/store/settings'

const { Sider } = Layout

function AppSidebar() {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const taskCounts = useClusterStore((state) => state.taskCounts)
  const fetchTaskCounts = useClusterStore((state) => state.fetchTaskCounts)
  const refreshInterval = useSettingsStore((state) => state.refreshInterval)

  const activeTaskCount = taskCounts.running_count + taskCounts.pending_count

  useEffect(() => {
    fetchTaskCounts()
    const interval = window.setInterval(() => {
      fetchTaskCounts()
    }, refreshInterval)

    return () => window.clearInterval(interval)
  }, [fetchTaskCounts, refreshInterval])

  const menuItems = [
    {
      key: '/',
      icon: <DashboardOutlined />,
      label: '仪表盘',
      onClick: () => navigate('/'),
    },
    {
      key: '/setup',
      icon: <PlusSquareOutlined />,
      label: '搭建集群',
      onClick: () => navigate('/setup'),
    },
    {
      key: '/tasks',
      icon: <HistoryOutlined />,
      label: '任务历史',
      onClick: () => navigate('/tasks'),
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: '系统设置',
      onClick: () => navigate('/settings'),
    },
  ]

  return (
    <Sider
      width={224}
      collapsed={collapsed}
      collapsedWidth={54}
      onCollapse={setCollapsed}
      style={{
        background: 'var(--bg2)',
        borderRight: '1px solid var(--border)',
        overflow: 'hidden',
      }}
    >
      {/* Logo */}
      <div
        style={{
          height: '54px',
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
          padding: '0 11px',
          borderBottom: '1px solid var(--border)',
          cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        <div
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '9px',
            flexShrink: 0,
            background: 'linear-gradient(135deg, #ff4d6d 0%, #8b0000 100%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '16px',
            color: '#fff',
            boxShadow: '0 0 20px rgba(255, 77, 109, 0.35)',
          }}
        >
          <DatabaseOutlined />
        </div>
        {!collapsed && (
          <div style={{ minWidth: 0, transition: 'all 0.2s', overflow: 'hidden' }}>
            <div
              style={{
                fontFamily: "'Syne', sans-serif",
                fontWeight: 900,
                fontSize: '17px',
                letterSpacing: '-0.5px',
                background: 'linear-gradient(90deg, #fff 0%, var(--primary) 100%)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
                whiteSpace: 'nowrap',
              }}
            >
              DB-HA
            </div>
            <div
              style={{
                fontSize: '9px',
                color: 'var(--text3)',
                fontFamily: "'JetBrains Mono', monospace",
                letterSpacing: '2px',
                whiteSpace: 'nowrap',
                marginTop: '1px',
              }}
            >
              MANAGER
            </div>
          </div>
        )}
      </div>

      {/* Collapse Button */}
      <div
        onClick={() => setCollapsed(!collapsed)}
        style={{
          position: 'absolute',
          top: '50%',
          right: '-11px',
          transform: 'translateY(-50%)',
          width: '22px',
          height: '22px',
          borderRadius: '50%',
          background: 'var(--bg3)',
          border: '1px solid var(--border2)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          zIndex: 30,
          transition: 'all 0.2s',
          color: 'var(--text3)',
          fontSize: '9px',
        }}
      >
        {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
      </div>

      {/* Menu */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          padding: '8px 0',
        }}
      >
        {menuItems.map((item) => {
          const isActive = location.pathname === item.key
          return (
            <div
              key={item.key}
              onClick={item.onClick}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '9px',
                padding: '9px 16px',
                cursor: 'pointer',
                transition: 'all 0.15s',
                borderLeft: '2px solid transparent',
                color: isActive ? 'var(--primary)' : 'var(--text2)',
                fontSize: '12.5px',
                userSelect: 'none',
                position: 'relative',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                minHeight: '38px',
                background: isActive ? 'var(--primary-dim)' : 'transparent',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--bg3)'
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = isActive ? 'var(--primary-dim)' : 'transparent'
              }}
            >
              <span
                style={{
                  fontSize: '15px',
                  width: '20px',
                  minWidth: '20px',
                  textAlign: 'center',
                  flexShrink: 0,
                  lineHeight: '1',
                }}
              >
                {item.icon}
              </span>
              {!collapsed && (
                <span
                  style={{
                    flex: 1,
                    minWidth: 0,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    transition: 'opacity 0.15s',
                  }}
                >
                  {item.label}
                </span>
              )}
              {/* Notification Badge for tasks */}
              {item.key === '/tasks' && !collapsed && (
                <Badge
                  count={activeTaskCount}
                  showZero
                  style={{
                    background: 'var(--primary)',
                    color: '#000',
                    fontSize: '9px',
                    fontWeight: 700,
                    padding: '0 6px',
                    borderRadius: '10px',
                    fontFamily: "'JetBrains Mono', monospace",
                  }}
                />
              )}
            </div>
          )
        })}
      </div>

      {/* User Section */}
      <div
        style={{
          borderTop: '1px solid var(--border)',
          padding: '8px',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '9px',
            padding: '8px',
            borderRadius: '8px',
            cursor: 'pointer',
            transition: 'background 0.15s',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'var(--bg3)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'transparent'
          }}
        >
          <div
            style={{
              width: '32px',
              height: '32px',
              minWidth: '32px',
              borderRadius: '50%',
              background: 'linear-gradient(135deg, var(--purple), var(--primary))',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '13px',
              fontWeight: 700,
              color: '#fff',
              flexShrink: 0,
              fontFamily: "'Syne', sans-serif",
            }}
          >
            A
          </div>
          {!collapsed && (
            <div
              style={{
                transition: 'opacity 0.15s',
                overflow: 'hidden',
                minWidth: 0,
              }}
            >
              <div
                style={{
                  fontSize: '12.5px',
                  fontWeight: 600,
                  color: 'var(--text)',
                }}
              >
                Admin
              </div>
              <div
                style={{
                  fontSize: '9px',
                  color: 'var(--text3)',
                  fontFamily: "'JetBrains Mono', monospace",
                  letterSpacing: '1px',
                }}
              >
                SUPERUSER
              </div>
            </div>
          )}
          <div
            style={{
              width: '7px',
              height: '7px',
              minWidth: '7px',
              borderRadius: '50%',
              background: 'var(--green)',
              boxShadow: '0 0 6px var(--green-glow)',
              marginLeft: 'auto',
              flexShrink: 0,
              animation: 'pulse 2s infinite',
              transition: 'opacity 0.15s',
            }}
          />
        </div>
      </div>
    </Sider>
  )
}

export default AppSidebar
