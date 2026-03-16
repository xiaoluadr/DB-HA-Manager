import { useEffect } from 'react'
import { Card, Form, Select, Space, Switch, Typography, message, Tag } from 'antd'
import { SettingOutlined, SyncOutlined, BgColorsOutlined } from '@ant-design/icons'
import {
  useSettingsStore,
  type Environment,
  type RefreshInterval,
} from '@/store/settings'

type SettingsFormValues = {
  environment: Environment
  refreshInterval: RefreshInterval
  themeIsDark: boolean
}

const ENVIRONMENT_OPTIONS: { value: Environment; label: string; description: string; color: string }[] = [
  { value: 'PROD', label: '生产环境', description: '面向正式集群，启用严格的安全保护', color: 'var(--red)' },
  { value: 'DEV', label: '开发环境', description: '调试与验证新功能，允许调试日志', color: 'var(--purple)' },
  { value: 'TEST', label: '测试环境', description: '模拟灾备演练，隔离真实数据', color: 'var(--orange)' },
]

const INTERVAL_OPTIONS: { value: RefreshInterval; label: string }[] = [
  { value: 30000, label: '30 秒' },
  { value: 60000, label: '60 秒' },
  { value: 120000, label: '120 秒' },
]

const REFRESH_DESCRIPTIONS: Record<RefreshInterval, string> = {
  30000: '推荐值，实时性与性能平衡',
  60000: '适合资源较少的环境，减少 API 频率',
  120000: '长周期巡检，降低干扰',
}

function Settings() {
  const [form] = Form.useForm<SettingsFormValues>()
  const [messageApi, contextHolder] = message.useMessage()
  const { environment, refreshInterval, theme, setEnvironment, setRefreshInterval, setTheme } = useSettingsStore()
  const selectedEnvironment = ENVIRONMENT_OPTIONS.find((option) => option.value === environment)
  const selectedInterval = INTERVAL_OPTIONS.find((option) => option.value === refreshInterval)

  useEffect(() => {
    form.setFieldsValue({
      environment,
      refreshInterval,
      themeIsDark: theme === 'dark',
    })
  }, [environment, refreshInterval, theme, form])

  const showSaved = () => {
    messageApi.open({
      key: 'settings-save',
      type: 'success',
      content: '设置已保存',
      duration: 1.2,
    })
  }

  const handleValuesChange = (changedValues: Partial<SettingsFormValues>) => {
    if (changedValues.environment) {
      setEnvironment(changedValues.environment)
      showSaved()
    }
    if (changedValues.refreshInterval) {
      setRefreshInterval(changedValues.refreshInterval)
      showSaved()
    }
    if (typeof changedValues.themeIsDark === 'boolean') {
      setTheme(changedValues.themeIsDark ? 'dark' : 'light')
      showSaved()
    }
  }

  return (
    <div style={{ animation: 'fadeUp 0.2s ease' }}>
      {contextHolder}
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Title level={4} style={{ color: 'var(--text)', marginBottom: 4 }}>
            系统设置
          </Typography.Title>
          <Typography.Text style={{ color: 'var(--text2)' }}>
            配置运行环境、刷新频率与主题偏好，信息会持久化在浏览器 localStorage。
          </Typography.Text>
        </div>
        <Form
          layout="vertical"
          form={form}
          onValuesChange={handleValuesChange}
          initialValues={{
            environment,
            refreshInterval,
            themeIsDark: theme === 'dark',
          }}
          style={{ width: '100%' }}
        >
          <Card
            title={
              <Space align="center" size={8}>
                <SettingOutlined />
                <span>基础配置</span>
              </Space>
            }
          >
            <Form.Item label="运行环境" name="environment">
              <Select
                options={ENVIRONMENT_OPTIONS.map((option) => ({
                  label: `${option.label} (${option.value})`,
                  value: option.value,
                }))}
                popupMatchSelectWidth={320}
                style={{ width: '100%' }}
              />
            </Form.Item>
            <div style={{ fontSize: '12px', color: 'var(--text3)', marginTop: '-4px', marginBottom: '16px' }}>
              {selectedEnvironment ? (
                <Space size={6} align="center" style={{ color: 'var(--text3)' }}>
                  <Tag color={selectedEnvironment.color} style={{ margin: 0 }}>
                    {selectedEnvironment.value}
                  </Tag>
                  <span>{selectedEnvironment.description}</span>
                </Space>
              ) : (
                '选择适配的目标环境'
              )}
            </div>
            <Form.Item label="刷新间隔" name="refreshInterval">
              <Select
                options={INTERVAL_OPTIONS}
                popupMatchSelectWidth={200}
                style={{ width: '200px' }}
              />
            </Form.Item>
            <div style={{ fontSize: '12px', color: 'var(--text3)', marginTop: '-4px' }}>
              {REFRESH_DESCRIPTIONS[refreshInterval]}
            </div>
          </Card>

          <Card
            title={
              <Space align="center" size={8}>
                <BgColorsOutlined />
                <span>主题与外观</span>
              </Space>
            }
            style={{ marginTop: '16px' }}
          >
            <Form.Item label="界面主题" name="themeIsDark" valuePropName="checked">
              <Switch checkedChildren="深色" unCheckedChildren="浅色" />
            </Form.Item>
            <Space align="center" size="middle" style={{ color: 'var(--text2)', fontSize: '12px' }}>
              <Tag color={theme === 'dark' ? 'magenta' : 'blue'}>{theme === 'dark' ? 'Dark Mode' : 'Light Mode'}</Tag>
              <span>实时应用到整站，包括 Ant Design 组件与自定义样式。</span>
            </Space>
          </Card>

          <Card
            title={
              <Space align="center" size={8}>
                <SyncOutlined />
                <span>当前状态</span>
              </Space>
            }
            style={{ marginTop: '16px' }}
          >
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                gap: '12px',
              }}
            >
              <StatusBadge label="环境" value={environment} color="var(--primary)" />
              <StatusBadge
                label="刷新频率"
                value={selectedInterval?.label ?? ''}
                color="var(--green)"
              />
              <StatusBadge label="主题" value={theme === 'dark' ? '深色' : '浅色'} color="var(--purple)" />
            </div>
          </Card>
        </Form>
      </Space>
    </div>
  )
}

function StatusBadge({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div
      style={{
        border: '1px dashed var(--border)',
        borderRadius: '10px',
        padding: '12px',
        background: 'var(--bg3)',
      }}
    >
      <div style={{ fontSize: '12px', color: 'var(--text3)', marginBottom: '4px' }}>{label}</div>
      <div
        style={{
          fontSize: '16px',
          fontWeight: 600,
          color,
          letterSpacing: '1px',
          fontFamily: "'JetBrains Mono', monospace",
        }}
      >
        {value}
      </div>
    </div>
  )
}

export default Settings
