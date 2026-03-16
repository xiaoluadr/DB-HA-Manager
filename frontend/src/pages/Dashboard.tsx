import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Row, Col, Card, Statistic, Empty, Alert, Badge, Space, Typography, Tag, DatePicker, Spin } from 'antd'
import {
  DatabaseOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { clusterApi } from '@/api'
import type { ClusterInfo, SyncHistoryItem } from '@/api/types'
import { useClusterStore } from '@/store'
import { useSettingsStore } from '@/store/settings'
import { formatDateTime, getSyncStatusColor, getSyncStatusText } from '@/utils'
import dayjs, { type Dayjs } from 'dayjs'

const { Text } = Typography
const { RangePicker } = DatePicker

type HistoryPoint = {
  time: string
  lag: number
  status: string
}

type DateRangeValue = [Dayjs | null, Dayjs | null] | null

interface SyncHistoryParams {
  start_time?: string
  end_time?: string
}

const STATUS_PRIORITY: Record<string, number> = {
  ERROR: 3,
  LAGGING: 2,
  WARNING: 2,
  GAP: 2,
  SYNCED: 1,
  UNKNOWN: 0,
}

const STATUS_COLORS: Record<string, string> = {
  ERROR: 'var(--red)',
  LAGGING: 'var(--orange)',
  WARNING: 'var(--orange)',
  GAP: 'var(--orange)',
  SYNCED: 'var(--green)',
  UNKNOWN: 'var(--text3)',
}

const MAX_HISTORY_POINTS = 60

const createDefaultHistoryRange = (): [Dayjs, Dayjs] => [
  dayjs().subtract(20, 'minute'),
  dayjs(),
]

const buildHistoryParams = (range: DateRangeValue): SyncHistoryParams | undefined => {
  if (!range || !range[0] || !range[1]) return undefined
  return {
    start_time: range[0].toDate().toISOString(),
    end_time: range[1].toDate().toISOString(),
  }
}

const aggregateHistory = (historyMap: Record<string, SyncHistoryItem[]>): HistoryPoint[] => {
  const buckets: Record<string, { totalLag: number; count: number; status: string; priority: number }> = {}

  Object.values(historyMap).forEach((items) => {
    items.forEach((item) => {
      if (!item?.timestamp) return
      const priority = STATUS_PRIORITY[item.status] ?? STATUS_PRIORITY.UNKNOWN
      if (!buckets[item.timestamp]) {
        buckets[item.timestamp] = {
          totalLag: item.lag_seconds ?? 0,
          count: 1,
          status: item.status,
          priority,
        }
        return
      }
      const bucket = buckets[item.timestamp]
      bucket.totalLag += item.lag_seconds ?? 0
      bucket.count += 1
      if (priority >= bucket.priority) {
        bucket.status = item.status
        bucket.priority = priority
      }
    })
  })

  return Object.entries(buckets)
    .map(([timestamp, bucket]) => ({
      time: timestamp,
      lag: bucket.count > 0 ? bucket.totalLag / bucket.count : 0,
      status: bucket.status,
    }))
    .sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime())
    .slice(-MAX_HISTORY_POINTS)
}

function Dashboard() {
  const { clusters, setClusters, setClusterStatus, setLoading, searchKeyword } = useClusterStore()
  const refreshInterval = useSettingsStore((state) => state.refreshInterval)
  const [loading, setLoadingLocal] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statusMap, setStatusMap] = useState<Record<string, any>>({})
  const [historyData, setHistoryData] = useState<HistoryPoint[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [historyRange, setHistoryRange] = useState<DateRangeValue>(() => createDefaultHistoryRange())
  const historyRangeRef = useRef<DateRangeValue>(historyRange)

  useEffect(() => {
    historyRangeRef.current = historyRange
  }, [historyRange])

  const fetchSyncHistory = useCallback(
    async (targetClusters?: ClusterInfo[], params?: SyncHistoryParams) => {
      const effectiveClusters = targetClusters ?? useClusterStore.getState().clusters
      if (!effectiveClusters.length) {
        setHistoryData([])
        setHistoryError(null)
        return
      }

      setHistoryLoading(true)
      const historyMap: Record<string, SyncHistoryItem[]> = {}
      const finalParams = params ?? buildHistoryParams(historyRangeRef.current)
      let successCount = 0

      try {
        for (const cluster of effectiveClusters) {
          try {
            const response = await clusterApi.getSyncHistory(cluster.cluster_id, finalParams)
            const payload = response?.data?.data ?? response?.data ?? []
            const normalizedHistory = (Array.isArray(payload) ? payload : []) as SyncHistoryItem[]
            historyMap[cluster.cluster_id] = normalizedHistory
            successCount += 1
          } catch (err) {
            console.error(`Failed to fetch sync history for ${cluster.cluster_id}:`, err)
            historyMap[cluster.cluster_id] = []
          }
        }

        setHistoryData(aggregateHistory(historyMap))
        setHistoryError(successCount === 0 ? '无法获取同步历史数据' : null)
      } catch (err) {
        setHistoryError(err instanceof Error ? err.message : '获取同步历史失败')
      } finally {
        setHistoryLoading(false)
      }
    },
    []
  )

  const fetchClusters = useCallback(async () => {
    setLoadingLocal(true)
    setError(null)
    setLoading(true)
    try {
      const response = await clusterApi.listClusters()
      const payload = response.data?.data ?? response.data ?? []
      const clusterList: ClusterInfo[] = Array.isArray(payload) ? payload : []
      setClusters(clusterList)

      if (clusterList.length === 0) {
        setStatusMap({})
        setHistoryData([])
        setHistoryError(null)
      } else {
        void fetchSyncHistory(clusterList, buildHistoryParams(historyRangeRef.current))
        // 获取每个集群的状态
        for (const cluster of clusterList) {
          try {
            const statusRes = await clusterApi.getClusterStatus(cluster.cluster_id)
            if (statusRes.data?.data) {
              setClusterStatus(cluster.cluster_id, statusRes.data.data)
              setStatusMap(prev => ({
                ...prev,
                [cluster.cluster_id]: statusRes.data.data,
              }))
            }
          } catch (err) {
            console.error(`Failed to fetch status for ${cluster.cluster_id}:`, err)
          }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取集群列表失败')
    } finally {
      setLoadingLocal(false)
      setLoading(false)
    }
  }, [fetchSyncHistory, setClusterStatus, setClusters, setLoading])

  const resetHistoryRange = () => {
    const defaultRange = createDefaultHistoryRange()
    setHistoryRange(defaultRange)
    historyRangeRef.current = defaultRange
    void fetchSyncHistory(undefined, buildHistoryParams(defaultRange))
  }

  const handleHistoryRangeChange = (value: DateRangeValue) => {
    if (!value) {
      resetHistoryRange()
      return
    }
    setHistoryRange(value)
    historyRangeRef.current = value
    if (value[0] && value[1]) {
      void fetchSyncHistory(undefined, buildHistoryParams(value))
    }
  }

  useEffect(() => {
    void fetchClusters()
    const interval = setInterval(() => {
      void fetchClusters()
    }, refreshInterval)
    return () => clearInterval(interval)
  }, [fetchClusters, refreshInterval])

  const stats = {
    total: clusters.length,
    synced: clusters.filter((c) => {
      const clusterStatus = useClusterStore.getState().clusterStatusMap[c.cluster_id]
      return clusterStatus?.sync?.status === 'SYNCED'
    }).length,
    lagging: clusters.filter((c) => {
      const clusterStatus = useClusterStore.getState().clusterStatusMap[c.cluster_id]
      return clusterStatus?.sync?.status === 'LAGGING'
    }).length,
    error: clusters.filter((c) => {
      const clusterStatus = useClusterStore.getState().clusterStatusMap[c.cluster_id]
      return clusterStatus?.sync?.status === 'ERROR' || clusterStatus?.sync?.gap
    }).length,
  }

  const maxHistoryLag = historyData.reduce((max, item) => Math.max(max, item.lag), 0) || 1

  const getClusterTypeBadge = (dbType: string) => {
    const badgeStyle: Record<string, { color: string; text: string }> = {
      oracle: { color: 'blue', text: 'Oracle' },
      mysql: { color: 'orange', text: 'MySQL' },
      postgresql: { color: 'cyan', text: 'PostgreSQL' },
      sqlserver: { color: 'red', text: 'SQL Server' },
    }
    const badge = badgeStyle[dbType] || { color: 'default', text: dbType }
    return <Tag color={badge.color as any}>{badge.text}</Tag>
  }

  const filteredClusters = useMemo(() => {
    const keyword = searchKeyword.trim().toLowerCase()
    if (!keyword) {
      return clusters
    }

    return clusters.filter((cluster) => {
      const name = cluster.cluster_name?.toLowerCase() ?? ''
      const id = cluster.cluster_id?.toLowerCase() ?? ''
      return name.includes(keyword) || id.includes(keyword)
    })
  }, [clusters, searchKeyword])

  const hasClusters = clusters.length > 0
  const hasFiltered = filteredClusters.length > 0

  return (
    <div style={{ animation: 'fadeUp 0.2s ease' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {/* 统计卡片 */}
        <Row gutter={[14, 14]}>
          <Col xs={24} sm={12} md={6}>
            <Card
              bordered={false}
              hoverable
              style={{
                background: 'var(--bg2)',
                border: '1px solid var(--border)',
                borderRadius: '10px',
                position: 'relative',
                overflow: 'hidden',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
              bodyStyle={{ padding: '16px' }}
            >
              <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '2px', background: 'var(--primary)' }} />
              <div style={{ fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace", letterSpacing: '1.5px', textTransform: 'uppercase', marginBottom: '10px' }}>
                总集群数
              </div>
              <Statistic
                value={stats.total}
                prefix={<DatabaseOutlined style={{ color: 'var(--primary)' }} />}
                valueStyle={{ fontFamily: "'Syne', sans-serif", fontSize: '30px', fontWeight: 900, lineHeight: 1, color: 'var(--text)' }}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card
              bordered={false}
              hoverable
              style={{
                background: 'var(--bg2)',
                border: '1px solid var(--border)',
                borderRadius: '10px',
                position: 'relative',
                overflow: 'hidden',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
              bodyStyle={{ padding: '16px' }}
            >
              <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '2px', background: 'var(--green)' }} />
              <div style={{ fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace", letterSpacing: '1.5px', textTransform: 'uppercase', marginBottom: '10px' }}>
                已同步
              </div>
              <Statistic
                value={stats.synced}
                prefix={<CheckCircleOutlined style={{ color: 'var(--green)' }} />}
                valueStyle={{ fontFamily: "'Syne', sans-serif", fontSize: '30px', fontWeight: 900, lineHeight: 1, color: 'var(--text)' }}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card
              bordered={false}
              hoverable
              style={{
                background: 'var(--bg2)',
                border: '1px solid var(--border)',
                borderRadius: '10px',
                position: 'relative',
                overflow: 'hidden',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
              bodyStyle={{ padding: '16px' }}
            >
              <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '2px', background: 'var(--orange)' }} />
              <div style={{ fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace", letterSpacing: '1.5px', textTransform: 'uppercase', marginBottom: '10px' }}>
                同步延迟
              </div>
              <Statistic
                value={stats.lagging}
                prefix={<ClockCircleOutlined style={{ color: 'var(--orange)' }} />}
                valueStyle={{ fontFamily: "'Syne', sans-serif", fontSize: '30px', fontWeight: 900, lineHeight: 1, color: 'var(--text)' }}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Card
              bordered={false}
              hoverable
              style={{
                background: 'var(--bg2)',
                border: '1px solid var(--border)',
                borderRadius: '10px',
                position: 'relative',
                overflow: 'hidden',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
              bodyStyle={{ padding: '16px' }}
            >
              <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '2px', background: 'var(--red)' }} />
              <div style={{ fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace", letterSpacing: '1.5px', textTransform: 'uppercase', marginBottom: '10px' }}>
                异常
              </div>
              <Statistic
                value={stats.error}
                prefix={<CloseCircleOutlined style={{ color: 'var(--red)' }} />}
                valueStyle={{ fontFamily: "'Syne', sans-serif", fontSize: '30px', fontWeight: 900, lineHeight: 1, color: 'var(--text)' }}
              />
            </Card>
          </Col>
        </Row>

        {/* 延迟趋势图 */}
        <Card
          title="集群平均延迟趋势"
          bordered={false}
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
          extra={
            <Space size="middle">
              <RangePicker
                value={historyRange}
                size="small"
                allowClear
                showTime={{ format: 'HH:mm' }}
                format="MM-DD HH:mm"
                disabledDate={(current) => !!current && current > dayjs()}
                onChange={handleHistoryRangeChange}
                style={{ width: 240 }}
              />
              <Text type="secondary" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                {historyData.length > 0 ? `${historyData.length} 个数据点` : '暂无数据'}
              </Text>
            </Space>
          }
        >
          {historyError && (
            <Alert
              message={historyError}
              type="error"
              showIcon
              closable
              style={{ marginBottom: 16 }}
              onClose={() => setHistoryError(null)}
            />
          )}
          {historyLoading ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minHeight: '160px', justifyContent: 'center' }}>
              <Spin size="small" />
              <Text type="secondary" style={{ fontSize: '12px' }}>加载同步历史...</Text>
            </div>
          ) : historyData.length === 0 ? (
            <Empty description="暂无历史数据" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '24px 0' }} />
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: '4px', height: '120px', paddingBottom: '20px' }}>
                {historyData.map((item) => {
                  const barHeight = Math.max(6, (item.lag / maxHistoryLag) * 120)
                  return (
                    <div
                      key={item.time}
                      style={{
                        flex: 1,
                        borderRadius: '4px 4px 0 0',
                        cursor: 'pointer',
                        transition: 'opacity 0.2s',
                        position: 'relative',
                        height: `${barHeight}px`,
                        background: STATUS_COLORS[item.status] || 'var(--green)',
                        opacity: 0.85,
                      }}
                      title={`${formatDateTime(item.time)} | 延迟 ${item.lag.toFixed(1)}s`}
                    />
                  )
                })}
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '6px' }}>
                <span style={{ flex: 1, textAlign: 'left', fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {formatDateTime(historyData[0]?.time)}
                </span>
                <span style={{ flex: 1, textAlign: 'center', fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {formatDateTime(historyData[Math.floor(historyData.length / 2)]?.time)}
                </span>
                <span style={{ flex: 1, textAlign: 'right', fontSize: '10px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                  {formatDateTime(historyData[historyData.length - 1]?.time)}
                </span>
              </div>
            </>
          )}
        </Card>

        {/* 集群列表 */}
        <Card
          title="集群列表"
          bordered={false}
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
          extra={
            <Space>
              <ReloadOutlined
                onClick={() => {
                  void fetchClusters()
                }}
                style={{ cursor: 'pointer', transition: 'all 0.2s' }}
                spin={loading}
              />
            </Space>
          }
        >
          {error && (
            <Alert
              message={error}
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              closable
              onClose={() => setError(null)}
            />
          )}
          {!hasClusters ? (
            <Empty description="暂无集群" style={{ padding: '60px 0' }} />
          ) : !hasFiltered ? (
            <Empty description="未找到匹配的集群" style={{ padding: '60px 0' }} />
          ) : (
            <Space direction="vertical" style={{ width: '100%' }} size="small">
              {filteredClusters.map((cluster) => {
                const status = statusMap[cluster.cluster_id]
                const syncStatus = status?.sync

                return (
                  <Card
                    key={cluster.cluster_id}
                    bordered={false}
                    hoverable
                    style={{
                      background: 'var(--bg3)',
                      border: '1px solid var(--border)',
                      borderRadius: '10px',
                      marginBottom: '14px',
                      transition: 'all 0.2s',
                    }}
                    bodyStyle={{ padding: '16px' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                      <DatabaseOutlined style={{ fontSize: '20px', color: 'var(--text2)' }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                          <span style={{ fontFamily: "'Syne', sans-serif", fontWeight: 700, fontSize: '15px', color: 'var(--text)' }}>
                            {cluster.cluster_name}
                          </span>
                          {getClusterTypeBadge(cluster.db_type)}
                        </div>
                        {syncStatus && (
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px', flexWrap: 'wrap' }}>
                            <Space size="small">
                              <ThunderboltOutlined style={{ fontSize: '14px', color: syncStatus.lag_seconds > 0 ? 'var(--orange)' : 'var(--green)' }} />
                              <Badge
                                status={getSyncStatusColor(syncStatus.status) as any}
                                text={getSyncStatusText(syncStatus.status)}
                                style={{ fontSize: '11px' }}
                              />
                              {syncStatus.lag_seconds > 0 && (
                                <Tag color="orange" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                                  {syncStatus.lag_seconds.toFixed(1)}s 延迟
                                </Tag>
                              )}
                            </Space>
                          </div>
                        )}
                        <div style={{ fontSize: '12px', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace", marginTop: '4px' }}>
                          更新时间: {formatDateTime(status?.timestamp)}
                        </div>
                      </div>
                      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '8px' }}>
                        {syncStatus?.primary?.connected && syncStatus?.standby?.connected && (
                          <Tag color="green" style={{ fontSize: '10px', fontFamily: "'JetBrains Mono', monospace" }}>
                            ONLINE
                          </Tag>
                        )}
                      </div>
                    </div>
                  </Card>
                )
              })}
            </Space>
          )}
        </Card>
      </Space>
    </div>
  )
}

export default Dashboard
