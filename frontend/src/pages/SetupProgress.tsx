import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import { useNavigate, useParams, useLocation } from 'react-router-dom'
import {
  Card,
  Steps,
  Progress,
  Collapse,
  Tag,
  Button,
  Space,
  Typography,
  Alert,
  Spin,
  Modal,
} from 'antd'
import {
  ArrowLeftOutlined,
  StopOutlined,
  ReloadOutlined,
  FileTextOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'

import { setupTaskApi } from '@/api'
import type {
  SetupLogItem,
  SetupProgressDetail,
  SetupStepStatus,
  TaskStatus,
} from '@/api/types'
import { useClusterStore } from '@/store'
import { formatDateTime } from '@/utils'

const { Title, Text } = Typography

const STAGE_DEFINITIONS = [
  { key: 'collect_input_and_validate', name: '收集输入并校验', description: '验证搭建所需的配置信息与输入参数' },
  { key: 'remote_discovery', name: '远程环境发现', description: '连接主备主机并采集现状信息' },
  { key: 'precheck', name: '运行前检查', description: '检查主库状态、归档模式、磁盘空间等依赖' },
  { key: 'generate_plan', name: '生成执行计划', description: '汇总检查结果，生成幂等执行计划' },
  { key: 'prepare_primary', name: '主库准备', description: '调整主库参数、准备必要文件' },
  { key: 'prepare_standby', name: '备库准备', description: '创建备库目录及参数文件' },
  { key: 'duplicate_standby', name: '备库复制', description: '执行 RMAN 复制与数据同步' },
  { key: 'start_managed_recovery', name: '启动受管恢复', description: '启动 MRP 并开始实时日志应用' },
  { key: 'verify_result', name: '验证结果', description: '检查同步延迟、GAP 等关键指标' },
  { key: 'finalize_report', name: '生成报告', description: '汇总执行结果并生成最终报告' },
] as const

type LogLevelFilter = 'all' | 'info' | 'warning' | 'error'

const LOG_LEVEL_TAGS: { value: LogLevelFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'info', label: 'INFO' },
  { value: 'warning', label: 'WARN' },
  { value: 'error', label: 'ERROR' },
]

const STAGE_STATUS_META: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '未开始' },
  running: { color: 'processing', text: '执行中' },
  success: { color: 'success', text: '完成' },
  failed: { color: 'error', text: '失败' },
  skipped: { color: 'default', text: '已跳过' },
}

// 兼容旧的步骤状态定义
const STEP_STATUS_META: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '未开始' },
  running: { color: 'processing', text: '执行中' },
  completed: { color: 'success', text: '完成' },
  warning: { color: 'warning', text: '警告' },
  error: { color: 'error', text: '失败' },
  skipped: { color: 'default', text: '已跳过' },
}

const TASK_STATUS_META: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '等待中' },
  running: { color: 'processing', text: '运行中' },
  success: { color: 'success', text: '成功' },
  failed: { color: 'error', text: '失败' },
  cancelled: { color: 'warning', text: '已取消' },
}

function SetupProgress() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const locationState = location.state as { clusterName?: string } | undefined
  const [logLevel, setLogLevel] = useState<LogLevelFilter>('all')
  const [initialLoading, setInitialLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const [manualRefresh, setManualRefresh] = useState(false)
  const logContainerRef = useRef<HTMLDivElement>(null)

  const { setupTask, setSetupTask, setupLogs, setSetupLogs } = useClusterStore((state) => ({
    setupTask: state.setupTask,
    setSetupTask: state.setSetupTask,
    setupLogs: state.setupLogs,
    setSetupLogs: state.setSetupLogs,
  }))

  const fetchProgress = useCallback(async () => {
    if (!taskId) {
      setPageError('缺少任务 ID')
      setInitialLoading(false)
      return
    }

    try {
      const response = await setupTaskApi.getSetupProgress(taskId)
      const data = response.data?.data
      if (!data) {
        setPageError('未找到任务信息')
        return
      }
      const detail = buildSetupDetail(data)
      setSetupTask(detail)
      setSetupLogs(detail.logs)
      setPageError(null)
    } catch (error) {
      console.error('Get setup progress failed', error)
      setPageError(error instanceof Error ? error.message : '获取搭建进度失败')
    } finally {
      setInitialLoading(false)
      setManualRefresh(false)
    }
  }, [setSetupLogs, setSetupTask, taskId])

  useEffect(() => {
    fetchProgress()
    const interval = setInterval(fetchProgress, 3000)
    return () => {
      clearInterval(interval)
    }
  }, [fetchProgress])

  useEffect(() => {
    return () => {
      setSetupTask(null)
      setSetupLogs([])
    }
  }, [setSetupLogs, setSetupTask])

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight
    }
  }, [setupLogs])

  const taskStatus = setupTask?.task.status ?? 'pending'
  const statusMeta = TASK_STATUS_META[taskStatus] || { color: 'default', text: taskStatus }
  const isFinished = ['success', 'failed', 'cancelled'].includes(taskStatus)
  const progressSummary = setupTask?.progress
  const basePercent = setupTask?.task.progress ?? 0
  const progressPercent = Math.max(basePercent, progressSummary?.progress_percent ?? 0)
  const currentStep = progressSummary?.current_step || '等待执行'
  const elapsedSecondsFromSummary = progressSummary?.elapsed_seconds
  const elapsedSeconds = typeof elapsedSecondsFromSummary === 'number'
    ? elapsedSecondsFromSummary
    : setupTask
      ? Math.max(0, dayjs().diff(dayjs(setupTask.task.created_at), 'second'))
      : 0
  const clusterNameFromResult =
    (setupTask?.task.result &&
      typeof setupTask.task.result === 'object' &&
      (setupTask.task.result as Record<string, any>).cluster_name) ||
    ''
  const clusterDisplayName =
    locationState?.clusterName || clusterNameFromResult || setupTask?.task.cluster_id || '-'

  const stepDetails = useMemo(() => {
    // 优先使用新的 stages 字典（后端阶段化结构）
    const stagesDict = progressSummary?.stages || {}

    return STAGE_DEFINITIONS.map((stage) => {
      // 从 stages 字典中获取阶段详情
      const stageDetail = stagesDict[stage.key] || {}
      const normalizedStatus = normalizeStageStatus(
        stageDetail?.status || 'pending'
      )

      return {
        ...stage,
        detail: stageDetail,
        status: normalizedStatus,
      }
    })
  }, [progressSummary?.stages])

  const stepSummary = useMemo(() => {
    const stats = { completed: 0, warning: 0, error: 0, skipped: 0 }
    stepDetails.forEach((step) => {
      if (step.status === 'completed') stats.completed += 1
      if (step.status === 'warning') stats.warning += 1
      if (step.status === 'error') stats.error += 1
      if (step.status === 'skipped') stats.skipped += 1
    })
    return stats
  }, [stepDetails])

  const filteredLogs = useMemo(() => {
    if (logLevel === 'all') return setupLogs
    return setupLogs.filter((log) => (log.level || 'info').toLowerCase() === logLevel)
  }, [logLevel, setupLogs])

  const handleCancelExecution = () => {
    if (!taskId || !setupTask) return
    Modal.confirm({
      title: '取消执行',
      content: '确认要取消当前搭建任务吗？操作将立即停止，且无法恢复。',
      okText: '确定取消',
      cancelText: '继续执行',
      centered: true,
      okButtonProps: { danger: true, loading: isCancelling },
      onOk: async () => {
        setIsCancelling(true)
        try {
          await setupTaskApi.cancelSetupTask(taskId)
          await fetchProgress()
        } catch (error) {
          console.error('Cancel setup task failed', error)
          Modal.error({
            title: '取消失败',
            content: error instanceof Error ? error.message : '取消任务时出现未知错误',
          })
        } finally {
          setIsCancelling(false)
        }
      },
    })
  }

  const handleRefresh = () => {
    setManualRefresh(true)
    fetchProgress()
  }

  const renderStepDescription = (stage: (typeof STAGE_DEFINITIONS)[number]) => (
    <Text style={{ color: 'var(--text2)', fontSize: '12px' }}>
      {stage.description}
    </Text>
  )

  const stepsItems = stepDetails.map((stage) => ({
    title: stage.name,
    description: renderStepDescription(stage),
    status: mapAntStepStatus(stage.status),
  }))

  return (
    <div style={{ animation: 'fadeUp 0.2s ease' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/')}
          style={{
            marginBottom: '12px',
            borderRadius: '8px',
            fontSize: '12px',
            padding: '7px 14px',
          }}
        >
          返回仪表盘
        </Button>

        {pageError && (
          <Alert
            type="error"
            message="获取搭建进度失败"
            description={pageError}
            showIcon
            style={{ borderRadius: '10px' }}
          />
        )}

        <Spin spinning={initialLoading && !setupTask}>
          {setupTask ? (
            <>
              <Card
                bordered={false}
                style={{
                  background: 'var(--bg2)',
                  border: '1px solid var(--border)',
                  borderRadius: '10px',
                }}
              >
                <Space direction="vertical" style={{ width: '100%' }} size="middle">
                  <Space align="center" style={{ width: '100%', justifyContent: 'space-between' }}>
                    <div>
                      <Title level={4} style={{ marginBottom: 4 }}>
                        {clusterDisplayName}
                      </Title>
                      <Space size="small">
                        <Tag color="geekblue">任务 ID: {setupTask.task.task_id}</Tag>
                        <Tag color={statusMeta.color as any}>{statusMeta.text}</Tag>
                      </Space>
                    </div>
                    <Space>
                      <Button
                        icon={<ReloadOutlined />}
                        onClick={handleRefresh}
                        loading={manualRefresh}
                        style={{ borderRadius: '8px' }}
                      >
                        刷新
                      </Button>
                      <Button
                        danger
                        icon={<StopOutlined />}
                        onClick={handleCancelExecution}
                        disabled={isFinished || isCancelling || taskStatus === 'failed'}
                        loading={isCancelling}
                        style={{ borderRadius: '8px' }}
                      >
                        取消执行
                      </Button>
                      {isFinished && (
                        <Button
                          type="primary"
                          icon={<CheckCircleOutlined />}
                          onClick={() => navigate('/tasks')}
                          style={{
                            background: 'var(--primary)',
                            borderColor: 'var(--primary)',
                            borderRadius: '8px',
                          }}
                        >
                          查看任务详情
                        </Button>
                      )}
                    </Space>
                  </Space>
                  <div style={{ padding: '12px 0' }}>
                    <Progress
                      percent={progressPercent}
                      status={taskStatus === 'failed' ? 'exception' : isFinished ? 'success' : 'active'}
                      strokeColor="var(--primary)"
                      trailColor="var(--bg3)"
                    />
                  </div>
                  <Space size="large" wrap>
                    <div>
                      <Text type="secondary" style={{ fontSize: '12px' }}>当前步骤</Text>
                      <div style={{ fontSize: '15px', color: 'var(--text)', marginTop: 4 }}>
                        {currentStep}
                      </div>
                    </div>
                    <div>
                      <Text type="secondary" style={{ fontSize: '12px' }}>开始时间</Text>
                      <div style={{ fontSize: '15px', color: 'var(--text)', marginTop: 4 }}>
                        {formatDateTime(setupTask.task.created_at)}
                      </div>
                    </div>
                    <div>
                      <Text type="secondary" style={{ fontSize: '12px' }}>已用时间</Text>
                      <div style={{ fontSize: '15px', color: 'var(--text)', marginTop: 4 }}>
                        {formatDuration(elapsedSeconds)}
                      </div>
                    </div>
                    {setupTask.task.completed_at && (
                      <div>
                        <Text type="secondary" style={{ fontSize: '12px' }}>完成时间</Text>
                        <div style={{ fontSize: '15px', color: 'var(--text)', marginTop: 4 }}>
                          {formatDateTime(setupTask.task.completed_at)}
                        </div>
                      </div>
                    )}
                  </Space>
                </Space>
              </Card>

              <Card
                bordered={false}
                style={{
                  background: 'var(--bg2)',
                  border: '1px solid var(--border)',
                  borderRadius: '10px',
                }}
              >
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Title level={5} style={{ margin: 0 }}>步骤进度</Title>
                    <Space size="small">
                      <Tag color="green">已完成 {stepSummary.completed}</Tag>
                      <Tag color="orange">警告 {stepSummary.warning}</Tag>
                      <Tag color="red">失败 {stepSummary.error}</Tag>
                      <Tag color="default">跳过 {stepSummary.skipped}</Tag>
                    </Space>
                  </div>
                  <Steps
                    size="small"
                    current={Math.max(
                      0,
                      STAGE_DEFINITIONS.findIndex((s) => s.key === progressSummary?.current_stage)
                    )}
                    items={stepsItems}
                  />
                </Space>
              </Card>

              <Card
                bordered={false}
                style={{
                  background: 'var(--bg2)',
                  border: '1px solid var(--border)',
                  borderRadius: '10px',
                }}
              >
                <Title level={5} style={{ marginBottom: 16 }}>
                  步骤详情
                </Title>
                <Collapse
                  ghost
                  accordion={false}
                  bordered={false}
                  style={{ background: 'transparent' }}
                  items={stepDetails.map((stage) => ({
                    key: stage.key,
                    label: (
                      <Space size="middle">
                        <Text style={{ fontWeight: 500 }}>{stage.name}</Text>
                        <Tag color={STAGE_STATUS_META[stage.status]?.color as any}>
                          {STAGE_STATUS_META[stage.status]?.text || stage.status}
                        </Tag>
                      </Space>
                    ),
                    children: (
                      <div style={{ padding: '8px 4px' }}>
                        {/* 阶段错误消息 */}
                        {stage.detail?.error_message && (
                          <Alert
                            type="error"
                            message={stage.detail.error_message}
                            showIcon
                            style={{ marginBottom: 12 }}
                          />
                        )}
                        {/* 阶段状态描述 */}
                        {stage.detail?.description && (
                          <Text type="secondary" style={{ fontSize: '12px', marginBottom: 8, display: 'block' }}>
                            {stage.detail.description}
                          </Text>
                        )}
                        {/* 阶段时间信息 */}
                        {stage.detail?.started_at && (
                          <Space size="small" style={{ marginBottom: 8 }}>
                            <Text type="secondary" style={{ fontSize: '12px' }}>
                              开始时间: {formatDateTime(stage.detail.started_at)}
                            </Text>
                            {stage.detail?.finished_at && (
                              <Text type="secondary" style={{ fontSize: '12px' }}>
                                耗时: {formatDuration(stage.detail.duration_seconds || 0)}
                              </Text>
                            )}
                          </Space>
                        )}
                        {/* 显示阶段内的步骤详情 */}
                        {stage.detail?.steps && Array.isArray(stage.detail.steps) && stage.detail.steps.length > 0 ? (
                          <Collapse
                            ghost
                            size="small"
                            style={{ marginTop: 8 }}
                            items={stage.detail.steps.map((step: any) => ({
                              key: step.step_id || step.step_name,
                              label: (
                                <Space size="small">
                                  <Text style={{ fontSize: '12px' }}>
                                    {step.display_name || step.step_name || step.step_id}
                                  </Text>
                                  <Tag color={STEP_STATUS_META[mapStepStatus(step.status)]?.color as any} style={{ fontSize: '11px' }}>
                                    {STEP_STATUS_META[mapStepStatus(step.status)]?.text || step.status}
                                  </Tag>
                                  {step.risk_level && step.risk_level !== 'low' && (
                                    <Tag color="orange" style={{ fontSize: '11px' }}>
                                      {step.risk_level.toUpperCase()}
                                    </Tag>
                                  )}
                                </Space>
                              ),
                              children: (
                                <div style={{ padding: '8px 12px' }}>
                                  {step.description && (
                                    <Text type="secondary" style={{ fontSize: '12px', display: 'block', marginBottom: 4 }}>
                                      {step.description}
                                    </Text>
                                  )}
                                  {step.error_message && (
                                    <Alert
                                      type="error"
                                      message={step.error_message}
                                      showIcon
                                      style={{ marginBottom: 8, fontSize: '12px' }}
                                    />
                                  )}
                                  {step.stdout && (
                                    <pre
                                      style={{
                                        background: 'var(--bg3)',
                                        padding: '8px',
                                        borderRadius: '4px',
                                        fontSize: '11px',
                                        color: 'var(--text2)',
                                        overflow: 'auto',
                                        maxHeight: '200px',
                                      }}
                                    >
                                      {typeof step.stdout === 'string' ? step.stdout.slice(0, 500) : JSON.stringify(step.stdout)}
                                    </pre>
                                  )}
                                  {step.stderr && (
                                    <Alert
                                      type="warning"
                                      message="标准错误输出"
                                      description={
                                        <pre style={{ margin: 0, fontSize: '11px', maxHeight: '150px', overflow: 'auto' }}>
                                          {typeof step.stderr === 'string' ? step.stderr.slice(0, 300) : JSON.stringify(step.stderr)}
                                        </pre>
                                      }
                                      style={{ marginTop: 8 }}
                                    />
                                  )}
                                </div>
                              ),
                            }))}
                          />
                        ) : (
                          <Text type="secondary" style={{ fontSize: '12px' }}>
                            暂无步骤详情
                          </Text>
                        )}
                        {/* 兼容旧的 detail 结构 */}
                        {!stage.detail?.steps && renderLegacyStepDetail(stage.detail)}
                      </div>
                    ),
                  }))}
                />
              </Card>

              <Card
                bordered={false}
                style={{
                  background: 'var(--bg2)',
                  border: '1px solid var(--border)',
                  borderRadius: '10px',
                }}
              >
                <Space
                  align="center"
                  style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}
                >
                  <Space>
                    <FileTextOutlined style={{ color: 'var(--primary)' }} />
                    <Title level={5} style={{ margin: 0 }}>实时日志</Title>
                  </Space>
                  <Space size="small">
                    {LOG_LEVEL_TAGS.map((level) => (
                      <Tag.CheckableTag
                        key={level.value}
                        checked={logLevel === level.value}
                        onChange={() => setLogLevel(level.value)}
                        style={{ borderRadius: '16px' }}
                      >
                        {level.label}
                      </Tag.CheckableTag>
                    ))}
                  </Space>
                </Space>
                <div
                  ref={logContainerRef}
                  style={{
                    maxHeight: '360px',
                    overflowY: 'auto',
                    background: 'var(--bg3)',
                    borderRadius: '8px',
                    padding: '12px',
                    border: '1px solid var(--border)',
                  }}
                >
                  {filteredLogs.length === 0 ? (
                    <div style={{ textAlign: 'center', color: 'var(--text2)', padding: '40px 0' }}>
                      暂无日志
                    </div>
                  ) : (
                    filteredLogs.map((log, index) => (
                      <div
                        key={`${log.timestamp}-${index}`}
                        style={{
                          borderBottom: '1px solid var(--border)',
                          padding: '8px 0',
                        }}
                      >
                        <Space align="start" size="middle" style={{ width: '100%' }}>
                          <Tag color={getLogColor(log.level)} style={{ fontSize: '11px' }}>
                            {(log.level || 'info').toUpperCase()}
                          </Tag>
                          <div style={{ flex: 1 }}>
                            <Space size="small" style={{ marginBottom: 4 }}>
                              <Text type="secondary" style={{ fontSize: '11px' }}>
                                {formatDateTime(log.timestamp)}
                              </Text>
                              {log.step && (
                                <Tag color="blue" style={{ fontSize: '11px' }}>
                                  {log.step}
                                </Tag>
                              )}
                            </Space>
                            <Text style={{ color: 'var(--text)', fontSize: '13px' }}>
                              {log.message}
                            </Text>
                          </div>
                        </Space>
                      </div>
                    ))
                  )}
                </div>
              </Card>

              <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
                <Button
                  onClick={() => navigate('/')}
                  style={{ borderRadius: '8px', padding: '7px 20px' }}
                >
                  返回 Dashboard
                </Button>
                <Button
                  type="primary"
                  onClick={() => navigate('/tasks')}
                  icon={<FileTextOutlined />}
                  style={{
                    background: 'var(--primary)',
                    borderColor: 'var(--primary)',
                    borderRadius: '8px',
                    padding: '7px 20px',
                  }}
                >
                  查看历史任务
                </Button>
              </Space>
            </>
          ) : (
            <Card
              bordered={false}
              style={{
                background: 'var(--bg2)',
                border: '1px solid var(--border)',
                borderRadius: '10px',
              }}
            >
              <div style={{ textAlign: 'center', padding: '60px 0' }}>
                <Spin size="large" />
                <div style={{ marginTop: 16, color: 'var(--text2)' }}>
                  正在获取搭建任务信息...
                </div>
              </div>
            </Card>
          )}
        </Spin>
      </Space>
    </div>
  )
}

function normalizeStageStatus(status: string): SetupStepStatus {
  const normalized = status?.toLowerCase()
  // 兼容后端返回的状态值
  if (normalized === 'success' || normalized === 'completed') return 'completed'
  if (normalized === 'failed' || normalized === 'error') return 'error'
  if (normalized === 'running') return 'running'
  if (normalized === 'skipped') return 'skipped'
  if (normalized === 'warning') return 'warning'
  if (normalized === 'pending') return 'pending'
  return 'pending'
}

// 映射后端步骤状态到前端状态
function mapStepStatus(status?: string): SetupStepStatus {
  if (!status) return 'pending'
  const normalized = status.toLowerCase()
  if (normalized === 'success') return 'completed'
  if (normalized === 'failed') return 'error'
  if (normalized === 'running') return 'running'
  if (normalized === 'skipped') return 'skipped'
  if (normalized === 'warning') return 'warning'
  return 'pending'
}

function mapAntStepStatus(status: SetupStepStatus): 'wait' | 'process' | 'finish' | 'error' {
  switch (status) {
    case 'completed':
    case 'success':
    case 'skipped':
      return 'finish'
    case 'error':
    case 'failed':
      return 'error'
    case 'warning':
    case 'running':
      return 'process'
    default:
      return 'wait'
  }
}

function getLogColor(level?: string) {
  const normalized = (level || 'info').toLowerCase()
  if (normalized === 'error') return 'red'
  if (normalized === 'warning' || normalized === 'warn') return 'orange'
  if (normalized === 'success') return 'green'
  return 'blue'
}

function renderLegacyStepDetail(detail: Record<string, any>) {
  if (!detail || typeof detail !== 'object') {
    return (
      <Text type="secondary" style={{ fontSize: '12px' }}>
        暂无更多详细信息
      </Text>
    )
  }

  const displayKeys = Object.keys(detail).filter(
    (key) => !['status', 'message', 'error', 'steps', 'error_message', 'description', 'started_at', 'finished_at', 'duration_seconds'].includes(key)
  )

  if (displayKeys.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: '12px' }}>
        暂无更多详细信息
      </Text>
    )
  }

  return (
    <pre
      style={{
        margin: 0,
        background: 'var(--bg3)',
        padding: '12px',
        borderRadius: '8px',
        fontSize: '12px',
        color: 'var(--text2)',
        overflowX: 'auto',
        fontFamily: "'JetBrains Mono', monospace",
      }}
    >
      {JSON.stringify(
        displayKeys.reduce<Record<string, any>>((acc, key) => {
          acc[key] = detail[key]
          return acc
        }, {}),
        null,
        2
      )}
    </pre>
  )
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0))
  const hrs = Math.floor(total / 3600)
  const mins = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hrs > 0) {
    return `${hrs}h ${mins}m ${secs}s`
  }
  if (mins > 0) {
    return `${mins}m ${secs}s`
  }
  return `${secs}s`
}

function buildSetupDetail(task: TaskStatus): SetupProgressDetail {
  const payload = (task.result || {}) as Record<string, any>
  const logs = extractLogItems(payload?.logs, task.logs)
  // 后端返回 setup_progress，前端使用 progress 字段兼容
  const setupProgress = payload?.setup_progress || payload?.progress || null
  return {
    task,
    progress: setupProgress,
    results: (payload?.results as SetupProgressDetail['results']) || {},
    logs,
  }
}

function extractLogItems(rawLogs: unknown, fallbackLogs: string[] = []): SetupLogItem[] {
  if (Array.isArray(rawLogs) && rawLogs.length > 0) {
    return rawLogs.map((log) => normalizeLogItem(log))
  }
  if (Array.isArray(fallbackLogs) && fallbackLogs.length > 0) {
    return fallbackLogs.map((line) => normalizeLogItem(line))
  }
  return []
}

function normalizeLogItem(entry: any): SetupLogItem {
  if (entry && typeof entry === 'object' && 'message' in entry) {
    return {
      timestamp: typeof entry.timestamp === 'string' ? entry.timestamp : new Date().toISOString(),
      step: typeof entry.step === 'string' ? entry.step : undefined,
      level: typeof entry.level === 'string' ? entry.level.toLowerCase() : undefined,
      message: typeof entry.message === 'string' ? entry.message : JSON.stringify(entry),
      result: typeof entry.result === 'string' ? entry.result : undefined,
    }
  }

  if (typeof entry === 'string') {
    const match = entry.match(/^([^:]+):\s*(.+)$/)
    const timestamp = match ? match[1] : new Date().toISOString()
    const messagePart = match ? match[2] : entry
    const levelMatch = messagePart.match(/^(ERROR|WARNING|INFO|DEBUG):\s*(.+)$/i)
    const level = levelMatch ? levelMatch[1].toLowerCase() : undefined
    const message = levelMatch ? levelMatch[2] : messagePart

    return {
      timestamp,
      level,
      message,
    }
  }

  return {
    timestamp: new Date().toISOString(),
    message: String(entry ?? ''),
  }
}

export default SetupProgress
