import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Descriptions, Card, Space, Button, Alert, Badge, Tag, Divider, Typography, Progress, Modal, Spin } from 'antd'
import {
  SyncOutlined,
  SwapOutlined,
  WarningOutlined,
  ToolOutlined,
  ReloadOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  ThunderboltOutlined,
  SafetyOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  HddOutlined,
} from '@ant-design/icons'
import { clusterApi } from '@/api'
import type { SwitchoverRequest, FailoverRequest, ManageRequest, ResourceStatus } from '@/api/types'
import { useClusterStatus } from '@/hooks'
import { getSyncStatusColor, getSyncStatusText, formatDateTime } from '@/utils'
import { useSettingsStore } from '@/store/settings'

const { Title, Text } = Typography

const formatSizeFromMb = (value: number): string => {
  if (!value || value <= 0) {
    return '0 MB'
  }
  const units = ['MB', 'GB', 'TB', 'PB']
  let unitIndex = 0
  let current = value
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024
    unitIndex += 1
  }
  return `${current.toFixed(1)} ${units[unitIndex]}`
}

const clampPercent = (value: number): number => {
  if (!Number.isFinite(value) || value <= 0) {
    return 0
  }
  return Math.min(100, Number(value.toFixed(1)))
}

function ClusterDetail() {
  const { clusterId } = useParams<{ clusterId: string }>()
  const { status, loading, error, refetch } = useClusterStatus(clusterId || '')
  const refreshInterval = useSettingsStore((state) => state.refreshInterval)
  const [actionLoading, setActionLoading] = useState(false)
  const [resourceStatus, setResourceStatus] = useState<ResourceStatus | null>(null)
  const [resourceLoading, setResourceLoading] = useState(false)
  const [resourceError, setResourceError] = useState<string | null>(null)

  const fetchResources = useCallback(
    async (showLoading = true) => {
      if (!clusterId) return
      if (showLoading) {
        setResourceLoading(true)
      }
      setResourceError(null)
      try {
        const response = await clusterApi.getResources(clusterId)
        const payload = response.data?.data ?? null
        setResourceStatus(payload || null)
      } catch (err) {
        setResourceError(err instanceof Error ? err.message : '获取资源信息失败')
      } finally {
        if (showLoading) {
          setResourceLoading(false)
        }
      }
    },
    [clusterId]
  )

  useEffect(() => {
    if (!clusterId) return
    void fetchResources()
    const interval = setInterval(() => {
      void fetchResources(false)
    }, refreshInterval)
    return () => clearInterval(interval)
  }, [clusterId, fetchResources, refreshInterval])

  const handleSwitchover = async (dryRun: boolean) => {
    Modal.confirm({
      title: dryRun ? '切换演练' : '确认切换',
      content: dryRun ? '是否执行切换演练（仅检查可行性）？' : '确定要执行 Switchover 切换吗？',
      okText: '确认',
      cancelText: '取消',
      okButtonProps: { style: { background: 'var(--primary)', borderColor: 'var(--primary)' } },
      onOk: async () => {
        setActionLoading(true)
        try {
          const request: SwitchoverRequest = { dry_run: dryRun }
          const response = await clusterApi.executeSwitchover(clusterId!, request)
          Modal.info({
            title: '操作结果',
            content: JSON.stringify(response.data, null, 2),
          })
        } catch (err) {
          Modal.error({
            title: '操作失败',
            content: err instanceof Error ? err.message : '未知错误',
          })
        } finally {
          setActionLoading(false)
          refetch()
        }
      },
    })
  }

  const handleFailover = async (dryRun: boolean) => {
    Modal.confirm({
      title: dryRun ? '接管演练' : '应急接管',
      content: dryRun ? '是否执行接管演练（仅检查备库可用性）？' : '⚠️ 确定要执行 Failover 应急接管吗？此操作不可逆！',
      okText: '确认',
      cancelText: '取消',
      okType: 'danger',
      okButtonProps: { style: { background: 'var(--red)', borderColor: 'var(--red)' } },
      onOk: async () => {
        setActionLoading(true)
        try {
          const request: FailoverRequest = { dry_run: dryRun, confirm: true }
          const response = await clusterApi.executeFailover(clusterId!, request)
          Modal.info({
            title: '操作结果',
            content: JSON.stringify(response.data, null, 2),
          })
        } catch (err) {
          Modal.error({
            title: '操作失败',
            content: err instanceof Error ? err.message : '未知错误',
          })
        } finally {
          setActionLoading(false)
          refetch()
        }
      },
    })
  }

  const handleManage = async (action: string) => {
    const actionNames: Record<string, string> = {
      sync: '手动同步',
      cleanup_archive: '清理归档',
      backup: '备份主库',
      recovery: '恢复备库',
    }

    Modal.confirm({
      title: `执行${actionNames[action]}`,
      content: `确定要执行 ${actionNames[action]} 操作吗？`,
      okText: '确认',
      cancelText: '取消',
      okButtonProps: { style: { background: 'var(--primary)', borderColor: 'var(--primary)' } },
      onOk: async () => {
        setActionLoading(true)
        try {
          const request: ManageRequest = { action: action as any, parameters: {} }
          const response = await clusterApi.manageCluster(clusterId!, request)
          Modal.info({
            title: '操作结果',
            content: JSON.stringify(response.data, null, 2),
          })
        } catch (err) {
          Modal.error({
            title: '操作失败',
            content: err instanceof Error ? err.message : '未知错误',
          })
        } finally {
          setActionLoading(false)
          refetch()
        }
      },
    })
  }

  if (loading && !status) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: '100px 0' }}>
        <Spin size="large" />
      </div>
    )
  }

  if (!status) {
    return <Alert message="集群不存在" type="error" />
  }

  const syncStatus = status.sync
  const tablespaceUsedMb = resourceStatus?.totals.tablespace_used_mb ?? 0
  const tablespaceTotalMb = resourceStatus?.totals.tablespace_total_mb ?? 0
  const storagePercent = tablespaceTotalMb > 0 ? clampPercent((tablespaceUsedMb / tablespaceTotalMb) * 100) : 0
  const archiveTotalMb = resourceStatus?.totals.archive_total_mb ?? 0
  const archivePercent = tablespaceTotalMb > 0 ? clampPercent((archiveTotalMb / tablespaceTotalMb) * 100) : 0
  const topTablespaces = (resourceStatus?.tablespaces ?? []).slice(0, 3)
  const recentArchiveLogs = (resourceStatus?.archive_logs ?? []).slice(0, 5)

  return (
    <div style={{ animation: 'fadeUp 0.2s ease' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {/* 操作按钮 */}
        <Card
          bordered={false}
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
        >
          <Space wrap>
            <Button
              style={{ borderRadius: '8px', fontSize: '12px', padding: '7px 14px' }}
              icon={<SwapOutlined />}
              onClick={() => handleSwitchover(true)}
              loading={actionLoading}
            >
              切换演练
            </Button>
            <Button
              type="primary"
              style={{
                background: 'var(--primary)',
                borderColor: 'var(--primary)',
                borderRadius: '8px',
                fontSize: '12px',
                padding: '7px 14px',
              }}
              icon={<SwapOutlined />}
              onClick={() => handleSwitchover(false)}
              loading={actionLoading}
            >
              执行切换
            </Button>
            <Button
              style={{ borderRadius: '8px', fontSize: '12px', padding: '7px 14px' }}
              icon={<WarningOutlined />}
              onClick={() => handleFailover(true)}
              loading={actionLoading}
            >
              接管演练
            </Button>
            <Button
              danger
              style={{
                background: 'var(--red-dim)',
                borderColor: 'rgba(255, 59, 92, 0.3)',
                color: 'var(--red)',
                borderRadius: '8px',
                fontSize: '12px',
                padding: '7px 14px',
              }}
              icon={<WarningOutlined />}
              onClick={() => handleFailover(false)}
              loading={actionLoading}
            >
              应急接管
            </Button>
            <Divider type="vertical" style={{ height: '24px', borderColor: 'var(--border)' }} />
            <Button
              style={{ borderRadius: '8px', fontSize: '12px', padding: '7px 14px' }}
              icon={<SyncOutlined />}
              onClick={() => handleManage('sync')}
              loading={actionLoading}
            >
              手动同步
            </Button>
            <Button
              style={{ borderRadius: '8px', fontSize: '12px', padding: '7px 14px' }}
              icon={<ToolOutlined />}
              onClick={() => handleManage('recovery')}
              loading={actionLoading}
            >
              恢复备库
            </Button>
            <Button
              style={{ borderRadius: '8px', fontSize: '12px', padding: '7px 14px' }}
              icon={<ReloadOutlined />}
              onClick={() => {
                void refetch()
                void fetchResources()
              }}
              loading={loading || resourceLoading}
            >
              刷新状态
            </Button>
          </Space>
        </Card>

        {/* 同步状态 */}
        <Card
          title="同步状态"
          bordered={false}
          extra={
            <Badge
              status={getSyncStatusColor(syncStatus.status) as any}
              text={getSyncStatusText(syncStatus.status)}
              style={{ fontSize: '13px' }}
            />
          }
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
        >
          <Descriptions column={3} size="small">
            <Descriptions.Item label="同步延迟" labelStyle={{ color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
              <Space>
                {syncStatus.lag_seconds > 0 ? (
                  <>
                    <ThunderboltOutlined style={{ color: 'var(--orange)' }} />
                    <Text style={{ color: 'var(--orange)', fontSize: '14px', fontWeight: 600 }}>
                      {syncStatus.lag_seconds.toFixed(1)}s
                    </Text>
                  </>
                ) : (
                  <>
                    <CheckCircleOutlined style={{ color: 'var(--green)' }} />
                    <Text style={{ color: 'var(--green)', fontSize: '14px', fontWeight: 600 }}>
                      正常
                    </Text>
                  </>
                )}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="序列号差距" labelStyle={{ color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
              <Space>
                {syncStatus.lag_sequence > 0 ? (
                  <>
                    <CloseCircleOutlined style={{ color: 'var(--red)' }} />
                    <Text style={{ color: 'var(--red)', fontSize: '14px', fontWeight: 600 }}>
                      {syncStatus.lag_sequence}
                    </Text>
                  </>
                ) : (
                  <>
                    <CheckCircleOutlined style={{ color: 'var(--green)' }} />
                    <Text style={{ color: 'var(--green)', fontSize: '14px', fontWeight: 600 }}>
                      无
                    </Text>
                  </>
                )}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="存在 Gap" labelStyle={{ color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
              {syncStatus.gap ? (
                <Tag color="red" style={{ fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                  是
                </Tag>
              ) : (
                <Tag color="green" style={{ fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                  否
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="更新时间" span={3} labelStyle={{ color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
              <Text style={{ color: 'var(--text2)', fontSize: '13px' }}>
                {formatDateTime(status.timestamp)}
              </Text>
            </Descriptions.Item>
          </Descriptions>
        </Card>

        {/* 主库和备库状态 */}
        <Card
          bordered={false}
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
        >
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            {/* 主库信息 */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                <DatabaseOutlined style={{ fontSize: '16px', color: 'var(--primary)' }} />
                <Title level={4} style={{ margin: 0, fontFamily: "'Syne', sans-serif", fontWeight: 700, color: 'var(--text)' }}>
                  主库信息
                </Title>
                {status.primary.connected && (
                  <Tag color="green" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                    ONLINE
                  </Tag>
                )}
              </div>
              <Descriptions column={2} size="small" bordered>
                <Descriptions.Item label="主机地址" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.primary.host}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="数据库角色" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Tag color="blue" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.primary.database_role || '-'}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="打开模式" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px' }}>
                    {status.primary.open_mode || '-'}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="保护模式" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px' }}>
                    {status.primary.protection_mode || '-'}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="最新归档序列" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.primary.last_archived_sequence || '-'}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="连接状态" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {status.primary.connected ? (
                    <Space>
                      <CheckCircleOutlined style={{ color: 'var(--green)' }} />
                      <Text style={{ color: 'var(--green)', fontSize: '12px' }}>已连接</Text>
                    </Space>
                  ) : (
                    <Space>
                      <CloseCircleOutlined style={{ color: 'var(--red)' }} />
                      <Text style={{ color: 'var(--red)', fontSize: '12px' }}>未连接</Text>
                    </Space>
                  )}
                </Descriptions.Item>
              </Descriptions>
            </div>

            <Divider style={{ borderColor: 'var(--border)', margin: '16px 0' }} />

            {/* 备库信息 */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                <CloudServerOutlined style={{ fontSize: '16px', color: 'var(--green)' }} />
                <Title level={4} style={{ margin: 0, fontFamily: "'Syne', sans-serif", fontWeight: 700, color: 'var(--text)' }}>
                  备库信息
                </Title>
                {status.standby.connected && (
                  <Tag color="green" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                    ONLINE
                  </Tag>
                )}
              </div>
              <Descriptions column={2} size="small" bordered>
                <Descriptions.Item label="主机地址" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.standby.host}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="数据库角色" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Tag color="cyan" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.standby.database_role || '-'}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="打开模式" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px' }}>
                    {status.standby.open_mode || '-'}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="MRP 状态" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {status.standby.mrp_status ? (
                    <Tag color="green" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                      {status.standby.mrp_status}
                    </Tag>
                  ) : (
                    '-'
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="已应用序列" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {status.standby.applied_sequence || '-'}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="最后应用时间" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  <Text style={{ color: 'var(--text2)', fontSize: '12px' }}>
                    {formatDateTime(status.standby.last_applied_time)}
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="连接状态" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {status.standby.connected ? (
                    <Space>
                      <CheckCircleOutlined style={{ color: 'var(--green)' }} />
                      <Text style={{ color: 'var(--green)', fontSize: '12px' }}>已连接</Text>
                    </Space>
                  ) : (
                    <Space>
                      <CloseCircleOutlined style={{ color: 'var(--red)' }} />
                      <Text style={{ color: 'var(--red)', fontSize: '12px' }}>未连接</Text>
                    </Space>
                  )}
                </Descriptions.Item>
              </Descriptions>
            </div>
          </Space>
        </Card>

        {/* 系统资源 */}
        <Card
          title="系统资源"
          bordered={false}
          extra={
            <Space size="small">
              <Text style={{ color: 'var(--text3)', fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                上次更新：{resourceStatus?.collected_at ? formatDateTime(resourceStatus.collected_at) : '等待采集'}
              </Text>
              <Button
                type="link"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => void fetchResources()}
                loading={resourceLoading}
                style={{ padding: 0, height: 'auto' }}
              >
                刷新
              </Button>
            </Space>
          }
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
        >
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            {resourceLoading && !resourceStatus ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 0' }}>
                <Spin size="small" />
                <Text style={{ color: 'var(--text3)', fontSize: '12px' }}>资源数据加载中...</Text>
              </div>
            ) : (
              <>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                    <HddOutlined style={{ color: 'var(--text3)' }} />
                    <Text style={{ color: 'var(--text2)', fontSize: '13px' }}>存储空间使用率</Text>
                  </div>
                  <Progress
                    percent={storagePercent}
                    strokeColor={{ '0%': 'var(--green)', '100%': 'var(--red)' }}
                    size="small"
                    style={{ marginBottom: '12px' }}
                  />
                  <Text style={{ color: 'var(--text3)', fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {tablespaceTotalMb > 0
                      ? `已用: ${formatSizeFromMb(tablespaceUsedMb)} / ${formatSizeFromMb(tablespaceTotalMb)} (${storagePercent.toFixed(1)}%)`
                      : '暂无表空间容量数据'}
                  </Text>
                  {topTablespaces.length ? (
                    <Space direction="vertical" size={6} style={{ width: '100%', marginTop: '8px' }}>
                      {topTablespaces.map((item) => (
                        <div key={item.tablespace_name}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Space>
                              <Tag color="blue" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                                {item.tablespace_name}
                              </Tag>
                              <Text style={{ color: 'var(--text3)', fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                                {formatSizeFromMb(item.mb_used)} / {formatSizeFromMb(item.mb_max)}
                              </Text>
                            </Space>
                            <Text style={{ color: 'var(--text2)', fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                              {clampPercent(item.usage_percent).toFixed(1)}%
                            </Text>
                          </div>
                          <Progress
                            percent={clampPercent(item.usage_percent)}
                            size="small"
                            showInfo={false}
                            strokeColor={{ '0%': 'var(--primary)', '100%': 'var(--red)' }}
                          />
                        </div>
                      ))}
                      {resourceStatus && resourceStatus.tablespaces.length > topTablespaces.length && (
                        <Text style={{ color: 'var(--text3)', fontSize: '11px' }}>
                          其余 {resourceStatus.tablespaces.length - topTablespaces.length} 个表空间已省略
                        </Text>
                      )}
                    </Space>
                  ) : (
                    <Text style={{ color: 'var(--text3)', fontSize: '11px', display: 'block', marginTop: '8px' }}>
                      暂无表空间数据
                    </Text>
                  )}
                </div>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                    <SafetyOutlined style={{ color: 'var(--text3)' }} />
                    <Text style={{ color: 'var(--text2)', fontSize: '13px' }}>归档日志保留</Text>
                  </div>
                  <Progress
                    percent={archivePercent}
                    strokeColor={{ '0%': 'var(--green)', '100%': 'var(--orange)' }}
                    size="small"
                  />
                  <Text style={{ color: 'var(--text3)', fontSize: '11px', fontFamily: "'JetBrains Mono', monospace" }}>
                    {resourceStatus
                      ? `日志数量: ${resourceStatus.archive_logs.length}，累计占用 ${formatSizeFromMb(archiveTotalMb)}`
                      : '暂无归档日志数据'}
                  </Text>
                  {recentArchiveLogs.length ? (
                    <Space direction="vertical" size={4} style={{ width: '100%', marginTop: '8px' }}>
                      {recentArchiveLogs.map((log) => (
                        <div
                          key={`${log.name}-${log.size_mb}`}
                          style={{ display: 'flex', justifyContent: 'space-between', fontFamily: "'JetBrains Mono', monospace" }}
                        >
                          <Text style={{ color: 'var(--text2)', fontSize: '11px' }}>{log.name}</Text>
                          <Text style={{ color: 'var(--text3)', fontSize: '11px' }}>{formatSizeFromMb(log.size_mb)}</Text>
                        </div>
                      ))}
                      {resourceStatus && resourceStatus.archive_logs.length > recentArchiveLogs.length && (
                        <Text style={{ color: 'var(--text3)', fontSize: '11px' }}>
                          其余 {resourceStatus.archive_logs.length - recentArchiveLogs.length} 条归档日志已省略
                        </Text>
                      )}
                    </Space>
                  ) : (
                    <Text style={{ color: 'var(--text3)', fontSize: '11px', display: 'block', marginTop: '8px' }}>
                      等待获取归档日志明细
                    </Text>
                  )}
                </div>
              </>
            )}
            {resourceError && (
              <Alert
                message="获取资源信息失败"
                description={resourceError}
                type="error"
                showIcon
              />
            )}
          </Space>
        </Card>

        {error && (
          <Alert
            message="获取状态失败"
            description={error}
            type="error"
            showIcon
          />
        )}
      </Space>
    </div>
  )
}

export default ClusterDetail
