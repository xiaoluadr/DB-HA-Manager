import { Card, Badge, Tag, Space, Button } from 'antd'
import {
  DatabaseOutlined,
  SyncOutlined,
  ClockCircleOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { ClusterInfo } from '@/api/types'
import { getSyncStatusColor, getSyncStatusText, formatRelativeTime } from '@/utils'

interface ClusterCardProps {
  cluster: ClusterInfo & {
    status?: {
      sync: { status: string; lag_seconds: number; gap: boolean }
      timestamp: string
    }
  }
}

function ClusterCard({ cluster }: ClusterCardProps) {
  const navigate = useNavigate()

  const syncStatus = cluster.status?.sync
  const lastUpdated = cluster.status?.timestamp
  const dbTypeBadge = {
    oracle: { color: 'blue', text: 'Oracle' },
    mysql: { color: 'orange', text: 'MySQL' },
    postgresql: { color: 'cyan', text: 'PostgreSQL' },
    sqlserver: { color: 'red', text: 'SQL Server' },
  }[cluster.db_type] || { color: 'default', text: cluster.db_type }

  return (
    <Card
      hoverable
      style={{ marginBottom: 16 }}
      title={
        <Space>
          <DatabaseOutlined />
          <span>{cluster.cluster_name}</span>
          <Tag color={dbTypeBadge.color}>{dbTypeBadge.text}</Tag>
        </Space>
      }
      extra={
        <Button type="link" onClick={() => navigate(`/clusters/${cluster.cluster_id}`)}>
          查看详情
        </Button>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }} size="small">
        {syncStatus && (
          <Space>
            <SyncOutlined />
            <span>同步状态：</span>
            <Badge
              status={getSyncStatusColor(syncStatus.status) as any}
              text={getSyncStatusText(syncStatus.status)}
            />
            {syncStatus.lag_seconds > 0 && (
              <Tag color="orange">{syncStatus.lag_seconds}s 延迟</Tag>
            )}
          </Space>
        )}
        {lastUpdated && (
          <Space>
            <ClockCircleOutlined />
            <span>更新时间：</span>
            <span style={{ color: '#999' }}>{formatRelativeTime(lastUpdated)}</span>
          </Space>
        )}
        <div style={{ fontSize: '12px', color: '#999' }}>
          集群 ID: {cluster.cluster_id}
        </div>
      </Space>
    </Card>
  )
}

export default ClusterCard
