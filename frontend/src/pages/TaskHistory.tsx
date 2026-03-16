import { useState, useEffect, useMemo } from 'react'
import { Card, Table, Button, Space, Tag, Drawer, Descriptions, Typography, Select, DatePicker } from 'antd'
import {
  ReloadOutlined,
  EyeOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import { taskApi } from '@/api'
import type { TaskStatus } from '@/api/types'
import { formatDateTime } from '@/utils'
import dayjs, { Dayjs } from 'dayjs'

const { Text } = Typography
const { RangePicker } = DatePicker

type FilterState = {
  clusterId?: string
  status?: TaskStatus['status']
  startTime: Dayjs | null
  endTime: Dayjs | null
}

function TaskHistory() {
  const [tasks, setTasks] = useState<TaskStatus[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedTask, setSelectedTask] = useState<TaskStatus | null>(null)
  const [drawerVisible, setDrawerVisible] = useState(false)
  const [filters, setFilters] = useState<FilterState>({
    clusterId: undefined,
    status: undefined,
    startTime: null,
    endTime: null,
  })

  const fetchTasks = async () => {
    setLoading(true)
    try {
      const response = await taskApi.listTasks()
      if (response.data?.data) {
        setTasks(response.data.data)
      }
    } catch (error) {
      console.error('获取任务列表失败:', error)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchTasks()
  }, [])

  const handleViewLogs = (task: TaskStatus) => {
    setSelectedTask(task)
    setDrawerVisible(true)
  }

  const handleFilterChange = <K extends keyof FilterState>(key: K, value: FilterState[K]) => {
    setFilters((prev) => ({
      ...prev,
      [key]: value,
    }))
  }

  const handleDateChange = (dates: null | [Dayjs | null, Dayjs | null]) => {
    setFilters((prev) => ({
      ...prev,
      startTime: dates?.[0] ?? null,
      endTime: dates?.[1] ?? null,
    }))
  }

  const handleClearFilters = () => {
    setFilters({
      clusterId: undefined,
      status: undefined,
      startTime: null,
      endTime: null,
    })
  }

  const statusColors: Record<string, string> = {
    pending: 'default',
    running: 'processing',
    success: 'success',
    failed: 'error',
  }

  const statusText: Record<string, string> = {
    pending: '等待中',
    running: '运行中',
    success: '成功',
    failed: '失败',
  }

  const clusterOptions = useMemo(() => {
    const ids = Array.from(
      new Set(
        tasks
          .map((task) => task.cluster_id)
          .filter((id): id is string => Boolean(id))
      )
    )
    return ids.map((id) => ({ label: id, value: id }))
  }, [tasks])

  const statusOptions = Object.entries(statusText).map(([value, label]) => ({
    value,
    label,
  }))

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      if (filters.clusterId && task.cluster_id !== filters.clusterId) {
        return false
      }

      if (filters.status && task.status !== filters.status) {
        return false
      }

      if (filters.startTime || filters.endTime) {
        const createdTime = task.created_at ? dayjs(task.created_at) : null

        if (filters.startTime && (!createdTime || createdTime.isBefore(filters.startTime))) {
          return false
        }

        if (filters.endTime && (!createdTime || createdTime.isAfter(filters.endTime))) {
          return false
        }
      }

      return true
    })
  }, [tasks, filters])

  const isFiltering = Boolean(filters.clusterId || filters.status || filters.startTime || filters.endTime)
  const rangeValue: [Dayjs | null, Dayjs | null] | null =
    filters.startTime || filters.endTime ? [filters.startTime, filters.endTime] : null

  const columns = [
    {
      title: '任务 ID',
      dataIndex: 'task_id',
      key: 'task_id',
      width: 200,
      ellipsis: true,
      render: (text: string) => (
        <Text style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '11px', color: 'var(--primary)' }}>
          {text}
        </Text>
      ),
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 120,
      render: (text: string) => <Tag color="blue">{text}</Tag>,
    },
    {
      title: '集群 ID',
      dataIndex: 'cluster_id',
      key: 'cluster_id',
      width: 180,
      render: (text: string) => (
        <Text style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '12px' }}>
          {text}
        </Text>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => (
        <Tag color={statusColors[status] as any}>
          {statusText[status]}
        </Tag>
      ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      key: 'progress',
      width: 120,
      render: (progress: number, record: TaskStatus) => (
        <Space>
          {progress < 100 && <ClockCircleOutlined style={{ color: 'var(--orange)' }} />}
          {progress >= 100 && record.status === 'success' && <CheckCircleOutlined style={{ color: 'var(--green)' }} />}
          {progress >= 100 && record.status === 'failed' && <CloseCircleOutlined style={{ color: 'var(--red)' }} />}
          <Text style={{ fontSize: '12px', fontFamily: "'JetBrains Mono', monospace" }}>
            {progress}%
          </Text>
        </Space>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (date: string) => (
        <Text style={{ fontSize: '12px', color: 'var(--text2)' }}>
          {formatDateTime(date)}
        </Text>
      ),
    },
    {
      title: '完成时间',
      dataIndex: 'completed_at',
      key: 'completed_at',
      width: 180,
      render: (date?: string) => (
        <Text style={{ fontSize: '12px', color: 'var(--text2)' }}>
          {formatDateTime(date)}
        </Text>
      ),
    },
    {
      title: '操作',
      key: 'action_btn',
      width: 100,
      render: (_: any, record: TaskStatus) => (
        <Button
          type="link"
          icon={<EyeOutlined />}
          onClick={() => handleViewLogs(record)}
          style={{ fontSize: '12px' }}
        >
          查看日志
        </Button>
      ),
    },
  ]

  return (
    <div style={{ animation: 'fadeUp 0.2s ease' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Card
          title="任务历史"
          bordered={false}
          extra={
            <Space>
              <ReloadOutlined
                onClick={fetchTasks}
                style={{ cursor: 'pointer', transition: 'all 0.2s' }}
                spin={loading}
              />
            </Space>
          }
          style={{
            background: 'var(--bg2)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
          }}
        >
          <Space
            wrap
            size="middle"
            style={{
              marginBottom: '16px',
              width: '100%',
            }}
            align="center"
          >
            <Select
              placeholder="选择集群"
              allowClear
              value={filters.clusterId}
              onChange={(value) => handleFilterChange('clusterId', value)}
              options={clusterOptions}
              style={{ minWidth: 180 }}
              size="small"
              loading={loading}
            />
            <Select
              placeholder="选择状态"
              allowClear
              value={filters.status}
              onChange={(value) => handleFilterChange('status', value)}
              options={statusOptions}
              style={{ minWidth: 160 }}
              size="small"
            />
            <RangePicker
              value={rangeValue}
              onChange={handleDateChange}
              allowClear
              showTime
              style={{ minWidth: 260 }}
              size="small"
            />
            <Button onClick={handleClearFilters} disabled={!isFiltering} icon={<CloseCircleOutlined />} size="small">
              清空筛选
            </Button>
          </Space>
          <Table
            dataSource={filteredTasks}
            columns={columns}
            rowKey="task_id"
            loading={loading}
            pagination={{
              pageSize: 20,
              showSizeChanger: false,
              showTotal: (total: number) => (
                <Text style={{ fontSize: '12px', color: 'var(--text2)' }}>
                  共 {total} 条记录
                </Text>
              ),
              itemRender: (_: any, type: string, originalElement: any) => {
                if (type === 'prev' || type === 'next') {
                  return (
                    <Button
                      style={{
                        borderRadius: '8px',
                        background: 'var(--bg3)',
                        border: '1px solid var(--border)',
                      }}
                    >
                      {originalElement}
                    </Button>
                  )
                }
                return originalElement
              },
            }}
            style={{
              background: 'transparent',
            }}
            size="small"
          />
        </Card>
      </Space>

      {/* 日志查看抽屉 */}
      <Drawer
        title="任务详情"
        placement="right"
        width={600}
        open={drawerVisible}
        onClose={() => setDrawerVisible(false)}
        styles={{
          body: { background: 'var(--bg2)', padding: '0' },
          header: { background: 'var(--bg2)', borderBottom: '1px solid var(--border)', padding: '18px 20px' },
        }}
        closeIcon={<CloseCircleOutlined style={{ color: 'var(--text3)', fontSize: '16px' }} />}
      >
        {selectedTask && (
          <Space direction="vertical" size="large" style={{ width: '100%', padding: '20px' }}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="任务 ID" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Text style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '12px', color: 'var(--primary)' }}>
                  {selectedTask.task_id}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="操作" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Tag color="blue">{selectedTask.action}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="集群 ID" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Text style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '12px' }}>
                  {selectedTask.cluster_id}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="状态" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Tag color={statusColors[selectedTask.status] as any}>
                  {statusText[selectedTask.status]}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="进度" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Space>
                  <Text style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text)' }}>
                    {selectedTask.progress}%
                  </Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Text style={{ fontSize: '12px', color: 'var(--text2)' }}>
                  {formatDateTime(selectedTask.created_at)}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="完成时间" labelStyle={{ background: 'var(--bg3)', color: 'var(--text3)', fontFamily: "'JetBrains Mono', monospace" }}>
                <Text style={{ fontSize: '12px', color: 'var(--text2)' }}>
                  {formatDateTime(selectedTask.completed_at)}
                </Text>
              </Descriptions.Item>
            </Descriptions>

            {/* 执行日志 */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                <FileTextOutlined style={{ color: 'var(--text3)' }} />
                <Text style={{ fontSize: '13px', color: 'var(--text)', fontWeight: 600 }}>
                  执行日志
                </Text>
              </div>
              <div
                style={{
                  background: 'var(--bg3)',
                  border: '1px solid var(--border)',
                  borderRadius: '8px',
                  maxHeight: '400px',
                  overflow: 'auto',
                  padding: '12px',
                }}
              >
                {selectedTask.logs.length === 0 ? (
                  <Text type="secondary" style={{ fontSize: '12px' }}>
                    暂无日志
                  </Text>
                ) : (
                  <pre style={{ fontSize: '11px', margin: 0, color: 'var(--text2)', fontFamily: "'JetBrains Mono', monospace", lineHeight: '1.6' }}>
                    {selectedTask.logs.join('\n')}
                  </pre>
                )}
              </div>
            </div>

            {/* 执行结果 */}
            {selectedTask.result && (
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                  <CheckCircleOutlined style={{ color: 'var(--text3)' }} />
                  <Text style={{ fontSize: '13px', color: 'var(--text)', fontWeight: 600 }}>
                    执行结果
                  </Text>
                </div>
                <div
                  style={{
                    background: 'var(--bg3)',
                    border: '1px solid var(--border)',
                    borderRadius: '8px',
                    padding: '12px',
                  }}
                >
                  <pre style={{ fontSize: '11px', margin: 0, color: 'var(--text2)', fontFamily: "'JetBrains Mono', monospace" }}>
                    {JSON.stringify(selectedTask.result, null, 2)}
                  </pre>
                </div>
              </div>
            )}
          </Space>
        )}
      </Drawer>
    </div>
  )
}

export default TaskHistory
