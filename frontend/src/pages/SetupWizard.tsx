import { ReactNode, useEffect, useMemo, useState } from 'react'
import {
  Steps,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
  Card,
  Row,
  Col,
  Button,
  Tag,
  Badge,
  Tooltip,
  Table,
  Alert,
  Collapse,
  Spin,
  Checkbox,
  Space,
  Typography,
  Modal,
  Divider,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined,
  SafetyCertificateOutlined,
  WarningOutlined,
  ThunderboltOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DeploymentUnitOutlined,
  ProfileOutlined,
  EditOutlined,
  InfoCircleOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import api from '@/api/client'
import { clusterApi } from '@/api'
import type { NamePath } from 'rc-field-form/lib/interface'
import type {
  APIResponse,
  PreviewResponse,
  PrecheckResult,
  FieldOverrideInfo,
  PrecheckResultStatus,
  RiskLevel,
  DiscoveryStatus,
  DiscoveryInfo,
} from '@/api/types'
import { formatDateTime } from '@/utils'

const { Step } = Steps
const { Option } = Select
const { Text, Title } = Typography
const { Panel } = Collapse
const { Group: CheckboxGroup } = Checkbox
const { CheckableTag } = Tag

const SETUP_STEPS = [
  { key: 'basic_info', title: '基础信息', description: '填写连接和 Oracle 配置' },
  { key: 'discovery', title: '自动探测', description: '探测环境信息' },
  { key: 'precheck', title: '预检查与风险', description: '检查兼容性和资源' },
  { key: 'plan_preview', title: '执行计划', description: '查看即将执行的步骤' },
  { key: 'confirm', title: '确认并提交', description: '确认风险后提交' },
]

type AuthType = 'key' | 'password'

type StorageType = 'asm' | 'fs'

type DuplicateMode = 'active' | 'backup'

type LogTransportMode = 'ASYNC' | 'SYNC'

type PathStrategy = 'same' | 'convert' | 'custom'

type ArchiveCleanupPolicy = 'none' | 'days' | 'size' | 'files'

interface SetupFormData {
  primaryHost: string
  primaryPort: number
  primarySshUser: string
  primarySshAuthType: AuthType
  primarySshKeyPath?: string
  primarySshPassword?: string
  standbyHost: string
  standbyPort: number
  standbySshUser: string
  standbySshAuthType: AuthType
  standbySshKeyPath?: string
  standbySshPassword?: string
  primarySid: string
  primaryOracleHome: string
  primaryDbUniqueName: string
  primaryOracleBase?: string
  primarySysPassword?: string
  primaryListenerPort: number
  primaryServiceName?: string
  primaryStorageType: StorageType
  primaryIsCdb: boolean
  standbySid: string
  standbyOracleHome: string
  standbyDbUniqueName: string
  standbyOracleBase?: string
  standbyListenerPort: number
  standbyServiceName?: string
  standbyStorageType: StorageType
  standbyIsCdb: boolean
  duplicateMode: DuplicateMode
  protectionMode: string
  logTransportMode: LogTransportMode
  enableRealtimeApply: boolean
  autoCreateSrl: boolean
  dataFilePathStrategy: PathStrategy
  redoFilePathStrategy: PathStrategy
  standbyArchivePath?: string
  archiveCleanupPolicy: ArchiveCleanupPolicy
  archiveCleanupParam?: number
  primaryDataFilePath?: string
  standbyDataFilePath?: string
  primaryRedoFilePath?: string
  standbyRedoFilePath?: string
}

const DEFAULT_FORM_VALUES: Partial<SetupFormData> = {
  // Keep only opinionated defaults (端口/认证/存储类型)，移除了 ORACLE_HOME、SID、CDB 等“看似已填好”的值。
  primaryPort: 22,
  primarySshAuthType: 'password',
  standbyPort: 22,
  standbySshAuthType: 'password',
  primaryListenerPort: 1521,
  standbyListenerPort: 1521,
  primaryStorageType: 'fs',
  standbyStorageType: 'fs',
}

const PROTECTION_MODE_OPTIONS = [
  'MAXIMUM PERFORMANCE',
  'MAXIMUM AVAILABILITY',
  'MAXIMUM PROTECTION',
] as const

const LOG_TRANSPORT_MODE_OPTIONS: readonly LogTransportMode[] = ['ASYNC', 'SYNC'] as const

const PATH_STRATEGY_OPTIONS: { value: PathStrategy; label: string }[] = [
  { value: 'same', label: '与主库一致' },
  { value: 'convert', label: '自动转换' },
  { value: 'custom', label: '自定义' },
]

const ARCHIVE_CLEANUP_OPTIONS: { value: ArchiveCleanupPolicy; label: string }[] = [
  { value: 'none', label: '不自动清理' },
  { value: 'days', label: '保留最近 N 天' },
  { value: 'size', label: '保留最近 N GB' },
  { value: 'files', label: '保留最近 N 个归档文件' },
]

const ARCHIVE_CLEANUP_PARAM_LABELS: Record<Exclude<ArchiveCleanupPolicy, 'none'>, string> = {
  days: '保留天数 (N)',
  size: '保留容量 (GB)',
  files: '保留归档文件数 (N)',
}

const RESULT_FILTERS: { key: 'all' | PrecheckResultStatus; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'pass', label: '通过' },
  { key: 'warn', label: '警告' },
  { key: 'fail', label: '失败' },
]

const RISK_COLORS: Record<RiskLevel, string> = {
  low: 'var(--green)',
  medium: 'var(--primary)',
  high: 'var(--orange)',
  critical: 'var(--red)',
}

const RESULT_COLORS: Record<PrecheckResultStatus, string> = {
  pass: 'var(--green)',
  warn: 'var(--orange)',
  fail: 'var(--red)',
}

const CONNECTION_ERROR_TYPES = new Set([
  'host_unreachable',
  'ssh_connection_failed',
  'ssh_auth_failed',
  'oracle_env_invalid',
  'sqlplus_unreachable',
])

const CONNECTION_ERROR_STATUS_CODES = new Set([401, 502, 504])

type ValidationErrorMap = Record<string, string>

// Provide human-friendly labels so the aggregated errors can explain exactly which field blocks previewing.
const FORM_FIELD_LABELS: Record<string, string> = {
  primaryHost: '主库主机地址',
  primaryPort: '主库 SSH 端口',
  primarySshUser: '主库 SSH 用户',
  primarySshAuthType: '主库认证方式',
  primarySshKeyPath: '主库私钥路径',
  primarySshPassword: '主库 SSH 密码/私钥密码',
  standbyHost: '备库主机地址',
  standbyPort: '备库 SSH 端口',
  standbySshUser: '备库 SSH 用户',
  standbySshAuthType: '备库认证方式',
  standbySshKeyPath: '备库私钥路径',
  standbySshPassword: '备库 SSH 密码/私钥密码',
  primarySid: '主库 SID',
  primaryOracleHome: '主库 ORACLE_HOME',
  primaryDbUniqueName: '主库 DB_UNIQUE_NAME',
  primaryOracleBase: '主库 ORACLE_BASE',
  primarySysPassword: '主库 SYS 密码',
  primaryListenerPort: '主库监听端口',
  primaryServiceName: '主库 SERVICE_NAME',
  primaryStorageType: '主库存储类型',
  primaryIsCdb: '主库 CDB',
  standbySid: '备库 SID',
  standbyOracleHome: '备库 ORACLE_HOME',
  standbyDbUniqueName: '备库 DB_UNIQUE_NAME',
  standbyOracleBase: '备库 ORACLE_BASE',
  standbyListenerPort: '备库监听端口',
  standbyServiceName: '备库 SERVICE_NAME',
  standbyStorageType: '备库存储类型',
  standbyIsCdb: '备库 CDB',
  duplicateMode: '搭建方式',
  protectionMode: '保护模式',
  logTransportMode: '日志传输模式',
  enableRealtimeApply: 'Real-Time Apply',
  autoCreateSrl: 'Standby Redo Log 配置',
  dataFilePathStrategy: '数据文件路径策略',
  redoFilePathStrategy: '联机日志路径策略',
  standbyArchivePath: '备库归档目录',
  archiveCleanupPolicy: '归档清理策略',
  archiveCleanupParam: '归档清理参数',
  primaryDataFilePath: '主库数据文件路径前缀',
  standbyDataFilePath: '备库数据文件路径前缀',
  primaryRedoFilePath: '主库联机日志路径前缀',
  standbyRedoFilePath: '备库联机日志路径前缀',
}

const getFieldLabel = (field: string): string => FORM_FIELD_LABELS[field] || field

const normalizeFieldName = (name: NamePath): string => {
  if (Array.isArray(name)) {
    return name.map((item) => item?.toString()).join('.')
  }
  return name?.toString() || ''
}

// Build a { field: message } map so Step 1 can surface grouped validation errors with localized labels.
const buildValidationErrorMap = (errorFields: { name: NamePath; errors?: string[] }[]): ValidationErrorMap =>
  errorFields.reduce((acc, field) => {
    const key = normalizeFieldName(field.name)
    if (!key) {
      return acc
    }
    const value = Array.isArray(field.errors) && field.errors.length
      ? field.errors.join('; ')
      : '填写有误'
    acc[key] = value
    return acc
  }, {} as ValidationErrorMap)

// 预览 API 依赖的字段清单；在前端收紧校验可提前阻断 500 错误 (Requirements #1/#5)。
const PREVIEW_BASE_REQUIRED_FIELDS: (keyof SetupFormData)[] = [
  'primaryHost',
  'primaryPort',
  'primarySshUser',
  'primarySshAuthType',
  'primarySid',
  'primaryOracleHome',
  'primarySysPassword',
  'primaryDbUniqueName',
  'primaryListenerPort',
  'primaryStorageType',
  'primaryIsCdb',
  'standbyHost',
  'standbyPort',
  'standbySshUser',
  'standbySshAuthType',
  'standbySid',
  'standbyOracleHome',
  'standbyDbUniqueName',
  'standbyListenerPort',
  'standbyStorageType',
  'standbyIsCdb',
  'duplicateMode',
  'protectionMode',
  'logTransportMode',
  'dataFilePathStrategy',
  'redoFilePathStrategy',
]

const buildPreviewRequiredFields = (values: SetupFormData): (keyof SetupFormData)[] => {
  const requirements: (keyof SetupFormData)[] = [...PREVIEW_BASE_REQUIRED_FIELDS]
  if (values.primarySshAuthType === 'key') {
    requirements.push('primarySshKeyPath')
  } else {
    requirements.push('primarySshPassword')
  }
  if (values.standbySshAuthType === 'key') {
    requirements.push('standbySshKeyPath')
  } else {
    requirements.push('standbySshPassword')
  }
  return requirements
}

const collectPreviewMissingFields = (values: SetupFormData): ValidationErrorMap => {
  const missing: ValidationErrorMap = {}
  buildPreviewRequiredFields(values).forEach((field) => {
    const rawValue = values[field]
    const normalizedValue = typeof rawValue === 'string' ? rawValue.trim() : rawValue
    if (isNil(normalizedValue)) {
      missing[field as string] = '请填写该字段'
    }
  })
  const primaryUnique = values.primaryDbUniqueName?.trim()
  const standbyUnique = values.standbyDbUniqueName?.trim()
  if (primaryUnique && standbyUnique && primaryUnique === standbyUnique) {
    missing.standbyDbUniqueName = '备库 DB_UNIQUE_NAME 需不同于主库'
  }
  return missing
}

const cardStyle = {
  background: 'var(--bg2)',
  borderColor: 'var(--border)',
}

type FieldValueSource =
  | 'auto_success'
  | 'auto_failed'
  | 'user_input'
  | 'user_override'
  | 'inherit_primary'
  | 'planned'

type StatusTagType = DiscoveryStatus | 'warning'

interface FieldDisplayMeta {
  value: any
  source: FieldValueSource
  originalValue?: any
}

interface FieldValueRowProps {
  label: string
  value: any
  source?: FieldValueSource
  status?: StatusTagType
  editableKey?: OverrideFieldKey
  extra?: ReactNode
  rowKey?: string
  customValue?: ReactNode
}

interface FieldOverrideConfig {
  label: string
  formField: keyof SetupFormData
  path: string[]
  type: 'text' | 'select' | 'boolean'
  role: 'primary' | 'standby'
  placeholder?: string
  options?: { label: string; value: string }[]
  description?: string
}

const VALUE_SOURCE_TAGS: Record<FieldValueSource, { label: string; color: string }> = {
  auto_success: { label: '自动探测成功', color: 'var(--green)' },
  auto_failed: { label: '自动探测失败', color: 'var(--red)' },
  user_input: { label: '用户输入', color: '#9254de' },
  user_override: { label: '用户输入', color: 'var(--orange)' },
  inherit_primary: { label: '继承主库', color: 'var(--primary)' },
  planned: { label: '计划值', color: 'var(--text2)' },
}

const DISCOVERY_STATUS_META: Record<StatusTagType, { label: string; color: string }> = {
  success: { label: '成功', color: 'var(--green)' },
  failed: { label: '失败', color: 'var(--red)' },
  timeout: { label: '超时', color: 'var(--orange)' },
  unavailable: { label: '不可用', color: 'var(--text2)' },
  missing: { label: '缺失', color: 'var(--text2)' },
  partial: { label: '部分', color: 'var(--primary)' },
  skipped: { label: '跳过', color: 'var(--border)' },
  unknown: { label: '未知', color: 'var(--border)' },
  warning: { label: '告警', color: 'var(--yellow)' },
}

const STORAGE_SELECT_OPTIONS: { label: string; value: string }[] = [
  { label: 'ASM', value: 'asm' },
  { label: '文件系统 (FS)', value: 'fs' },
]

const FIELD_OVERRIDE_CONFIG = {
  primary_oracle_home: {
    label: '主库 ORACLE_HOME',
    formField: 'primaryOracleHome',
    path: ['primary_oracle', 'oracle_home'],
    type: 'text',
    role: 'primary',
    placeholder: '/u01/app/oracle/product/19c/dbhome_1',
  },
  primary_service_name: {
    label: '主库 SERVICE_NAME',
    formField: 'primaryServiceName',
    path: ['primary_oracle', 'service_name'],
    type: 'text',
    role: 'primary',
    placeholder: 'orclpdb1',
  },
  primary_db_unique_name: {
    label: '主库 DB_UNIQUE_NAME',
    formField: 'primaryDbUniqueName',
    path: ['primary_oracle', 'db_unique_name'],
    type: 'text',
    role: 'primary',
    placeholder: 'DG_PRIMARY',
  },
  primary_storage_type: {
    label: '主库存储类型',
    formField: 'primaryStorageType',
    path: ['primary_oracle', 'storage_type'],
    type: 'select',
    role: 'primary',
    options: STORAGE_SELECT_OPTIONS,
  },
  primary_is_cdb: {
    label: '主库 CDB',
    formField: 'primaryIsCdb',
    path: ['primary_oracle', 'is_cdb'],
    type: 'boolean',
    role: 'primary',
    description: '若主库为多租户 (CDB)，请开启该选项以便生成正确的后续命令。',
  },
  standby_oracle_home: {
    label: '备库 ORACLE_HOME',
    formField: 'standbyOracleHome',
    path: ['standby_oracle', 'oracle_home'],
    type: 'text',
    role: 'standby',
    placeholder: '/u01/app/oracle/product/19c/dbhome_1',
  },
  standby_service_name: {
    label: '备库 SERVICE_NAME',
    formField: 'standbyServiceName',
    path: ['standby_oracle', 'service_name'],
    type: 'text',
    role: 'standby',
    placeholder: 'orclstdby',
  },
  standby_db_unique_name: {
    label: '备库 DB_UNIQUE_NAME',
    formField: 'standbyDbUniqueName',
    path: ['standby_oracle', 'db_unique_name'],
    type: 'text',
    role: 'standby',
    placeholder: 'DG_STANDBY',
  },
  standby_storage_type: {
    label: '备库存储类型',
    formField: 'standbyStorageType',
    path: ['standby_oracle', 'storage_type'],
    type: 'select',
    role: 'standby',
    options: STORAGE_SELECT_OPTIONS,
  },
  standby_is_cdb: {
    label: '备库 CDB',
    formField: 'standbyIsCdb',
    path: ['standby_oracle', 'is_cdb'],
    type: 'boolean',
    role: 'standby',
    description: '若备库为 CDB，请同步开启以保持角色一致。',
  },
} as const satisfies Record<string, FieldOverrideConfig>

type OverrideFieldKey = keyof typeof FIELD_OVERRIDE_CONFIG

const FEATURE_FLAG_KEYS = ['data_file_path_strategy', 'redo_file_path_strategy', 'archive_cleanup_policy'] as const

const REQUIRED_DISCOVERY_FIELDS = Object.values(FIELD_OVERRIDE_CONFIG).map((config) => ({
  formField: config.formField,
  label: config.label,
  type: config.type,
}))
// NOTE:
// 1) renderDiscoveryStep/handlePreview/handleRefetchPreview/handleNext were updated to drive the confirmation UX.
// 2) Added helpers (renderHostDiscoveryCard, renderOracleDiscoveryCard, renderNetworkStorageCard, renderFieldOverrideModal,
//    renderFieldValueRow, getFieldDisplayMeta, updateDiscoveredInfoValue) to keep discovery cards composable.
// 3) handleFieldOverride writes overrides back to both the Ant Design form (for later steps) and previewData for immediate feedback.
// 4) Backend could enrich DiscoveryInfo with explicit SSH/network statuses + directory permission outputs to eliminate placeholders.

const formatDuration = (seconds?: number | null): string => {
  if (!seconds || seconds <= 0) {
    return '-'
  }
  const mins = Math.floor(seconds / 60)
  const secs = seconds % 60
  if (mins > 0) {
    return secs ? `${mins} 分 ${secs} 秒` : `${mins} 分`
  }
  return `${secs} 秒`
}

const formatValue = (value: any): string => {
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  if (typeof value === 'boolean') {
    return value ? '是' : '否'
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch (error) {
      return '[object]'
    }
  }
  return String(value)
}

const isNil = (value: any): boolean => value === null || value === undefined || value === ''

const STORAGE_DIR_LABELS: Record<string, string> = {
  not_exists: '目录不存在',
  not_writable: '目录不可写',
  ok: '可写',
  ok_exists_files: '存在且有文件',
}

const formatWritable = (value: any, status?: string): string | undefined => {
  if (status && STORAGE_DIR_LABELS[status]) {
    return STORAGE_DIR_LABELS[status]
  }
  if (isNil(value)) return undefined
  return value ? '可写' : '不可写'
}

const getDiscoveryValue = (info: DiscoveryInfo | undefined, path: string[]): any => {
  if (!info || !path.length) return undefined
  return path.reduce((current: any, segment) => (current ? current[segment] : undefined), info as any)
}

const setDiscoveryValue = (info: DiscoveryInfo, path: string[], nextValue: any): DiscoveryInfo => {
  if (!path.length) {
    return info
  }
  const cloned: DiscoveryInfo = {
    ...info,
    primary_host: { ...info.primary_host },
    standby_host: { ...info.standby_host },
    primary_oracle: { ...info.primary_oracle },
    standby_oracle: { ...info.standby_oracle },
    primary_db: { ...info.primary_db },
    network: { ...info.network },
    storage: { ...info.storage },
    conflicts: [...(info.conflicts || [])],
  }
  let cursor: any = cloned
  path.forEach((segment, idx) => {
    if (idx === path.length - 1) {
      cursor[segment] = nextValue
      return
    }
    cursor[segment] = { ...(cursor[segment] || {}) }
    cursor = cursor[segment]
  })
  return cloned
}

const deriveSharedDbName = (values: SetupFormData): string => {
  const primaryUnique = values.primaryDbUniqueName?.trim()
  if (primaryUnique) {
    const normalized = primaryUnique.replace(/[-_]*(primary|prim|main)$/i, '')
    return (normalized || primaryUnique).toUpperCase()
  }
  if (values.primarySid?.trim()) {
    return values.primarySid.trim().toUpperCase()
  }
  if (values.standbySid?.trim()) {
    return values.standbySid.trim().toUpperCase()
  }
  return 'ORCL'
}

const buildClusterId = (values: SetupFormData): string => {
  const derivedName = deriveSharedDbName(values)
  const raw = values.primaryDbUniqueName || derivedName || `${values.primaryHost}-${values.standbyHost}`
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
    || 'oracle-adg-cluster'
}

const buildPreviewPayload = (values: SetupFormData) => {
  const sharedDbName = deriveSharedDbName(values)
  const normalizedPrimaryUniqueName = values.primaryDbUniqueName?.trim() || values.primaryDbUniqueName
  const normalizedStandbyUniqueName = values.standbyDbUniqueName?.trim() || values.standbyDbUniqueName
  const primaryBasePath = values.primaryStorageType === 'asm'
    ? `+DATA/${values.primaryDbUniqueName || sharedDbName}`
    : `/u01/oradata/${sharedDbName}`
  const defaultDataFilesPath = values.primaryDataFilePath || (values.primaryStorageType === 'asm'
    ? `${primaryBasePath}/DATAFILE`
    : primaryBasePath)
  const payload: Record<string, any> = {
    primary_host: values.primaryHost,
    primary_ssh_port: values.primaryPort,
    primary_ssh_user: values.primarySshUser,
    primary_ssh_auth_type: values.primarySshAuthType,
    standby_host: values.standbyHost,
    standby_ssh_port: values.standbyPort,
    standby_ssh_user: values.standbySshUser,
    standby_ssh_auth_type: values.standbySshAuthType,
    oracle_sid: values.primarySid,
    oracle_home: values.primaryOracleHome,
    sys_password: values.primarySysPassword,
    db_name: sharedDbName,
    db_unique_name_primary: normalizedPrimaryUniqueName,
    db_unique_name_standby: normalizedStandbyUniqueName,
    storage_type: values.primaryStorageType,
    duplicate_mode: values.duplicateMode,
    protection_mode: values.protectionMode,
    log_transport_mode: values.logTransportMode,
    enable_realtime_apply: values.enableRealtimeApply,
    auto_create_srl: values.autoCreateSrl,
    data_file_path_strategy: values.dataFilePathStrategy,
    redo_file_path_strategy: values.redoFilePathStrategy,
    archivelog_path: values.standbyArchivePath
      || (values.primaryStorageType === 'asm' ? `${primaryBasePath}/ARCH` : `${primaryBasePath}/archivelog`),
    data_files_path: defaultDataFilesPath,
    standby_archive_path: values.standbyArchivePath,
    archive_cleanup_policy: values.archiveCleanupPolicy,
    archive_cleanup_param: values.archiveCleanupParam,
    primary_sid: values.primarySid,
    primary_oracle_home: values.primaryOracleHome,
    primary_oracle_base: values.primaryOracleBase,
    primary_db_unique_name: normalizedPrimaryUniqueName,
    primary_listener_port: values.primaryListenerPort,
    primary_service_name: values.primaryServiceName || sharedDbName,
    primary_storage_type: values.primaryStorageType,
    primary_is_cdb: values.primaryIsCdb,
    standby_sid: values.standbySid,
    standby_oracle_home: values.standbyOracleHome,
    standby_oracle_base: values.standbyOracleBase,
    standby_db_unique_name: normalizedStandbyUniqueName,
    standby_listener_port: values.standbyListenerPort,
    standby_service_name: values.standbyServiceName || sharedDbName,
    standby_storage_type: values.standbyStorageType,
    standby_is_cdb: values.standbyIsCdb,
  }

  if (values.primarySshAuthType === 'key') {
    payload.primary_ssh_key_path = values.primarySshKeyPath
    payload.primary_ssh_password = values.primarySshPassword
  } else {
    payload.primary_ssh_password = values.primarySshPassword
  }

  if (values.standbySshAuthType === 'key') {
    payload.standby_ssh_key_path = values.standbySshKeyPath
    payload.standby_ssh_password = values.standbySshPassword
  } else {
    payload.standby_ssh_password = values.standbySshPassword
  }

  if (values.primaryDataFilePath) {
    payload.primary_data_file_path = values.primaryDataFilePath
  }
  if (values.standbyDataFilePath) {
    payload.standby_data_file_path = values.standbyDataFilePath
  }
  if (values.primaryRedoFilePath) {
    payload.primary_redo_file_path = values.primaryRedoFilePath
  }
  if (values.standbyRedoFilePath) {
    payload.standby_redo_file_path = values.standbyRedoFilePath
  }

  return payload
}

const buildClusterPayload = (values: SetupFormData) => {
  const clusterId = buildClusterId(values)
  const sharedDbName = deriveSharedDbName(values)
  const primaryBasePath = values.primaryStorageType === 'asm'
    ? `+DATA/${values.primaryDbUniqueName || sharedDbName}`
    : `/u01/oradata/${sharedDbName}`

  return {
    cluster_id: clusterId,
    cluster_name: sharedDbName,
    db_type: 'oracle',
    primary: {
      sid: values.primarySid,
      oracle_sid: values.primarySid,
      oracle_home: values.primaryOracleHome,
      oracle_base: values.primaryOracleBase,
      sys_password: values.primarySysPassword,
      db_unique_name: values.primaryDbUniqueName,
      listener_port: values.primaryListenerPort,
      service_name: values.primaryServiceName || sharedDbName,
      storage_type: values.primaryStorageType,
      is_cdb: values.primaryIsCdb,
    },
    standby: {
      sid: values.standbySid,
      oracle_sid: values.standbySid,
      oracle_home: values.standbyOracleHome,
      oracle_base: values.standbyOracleBase,
      sys_password: values.primarySysPassword,
      db_unique_name: values.standbyDbUniqueName,
      listener_port: values.standbyListenerPort,
      service_name: values.standbyServiceName || sharedDbName,
      storage_type: values.standbyStorageType,
      is_cdb: values.standbyIsCdb,
    },
    primary_ssh: {
      host: values.primaryHost,
      port: values.primaryPort,
      username: values.primarySshUser,
      password: values.primarySshPassword,
      private_key_path: values.primarySshAuthType === 'key' ? values.primarySshKeyPath : undefined,
    },
    standby_ssh: {
      host: values.standbyHost,
      port: values.standbyPort,
      username: values.standbySshUser,
      password: values.standbySshPassword,
      private_key_path: values.standbySshAuthType === 'key' ? values.standbySshKeyPath : undefined,
    },
    data_files_path: values.primaryDataFilePath
      || (values.primaryStorageType === 'asm' ? `${primaryBasePath}/DATAFILE` : primaryBasePath),
    archivelog_path: values.standbyArchivePath
      || (values.primaryStorageType === 'asm' ? `${primaryBasePath}/ARCH` : `${primaryBasePath}/archivelog`),
    data_file_path_strategy: values.dataFilePathStrategy,
    primary_data_file_path: values.primaryDataFilePath,
    standby_data_file_path: values.standbyDataFilePath,
    redo_file_path_strategy: values.redoFilePathStrategy,
    primary_redo_file_path: values.primaryRedoFilePath,
    standby_redo_file_path: values.standbyRedoFilePath,
    log_transport_mode: values.logTransportMode,
    auto_create_srl: values.autoCreateSrl,
    standby_archive_path: values.standbyArchivePath,
    archive_cleanup_policy: values.archiveCleanupPolicy,
    archive_cleanup_param: values.archiveCleanupParam,
  }
}

const buildSetupPayload = (values: SetupFormData) => {
  const sharedDbName = deriveSharedDbName(values)
  const derivedBasePath = values.primaryStorageType === 'asm'
    ? `+DATA/${values.primaryDbUniqueName || sharedDbName}`
    : `/u01/oradata/${sharedDbName}`
  const defaultDataFilesPath = values.primaryDataFilePath || (values.primaryStorageType === 'asm'
    ? `${derivedBasePath}/DATAFILE`
    : derivedBasePath)
  const defaultArchivePath = values.standbyArchivePath || (values.primaryStorageType === 'asm'
    ? `${derivedBasePath}/ARCH`
    : `${derivedBasePath}/archivelog`)

  const payload: Record<string, any> = {
    'db_name': sharedDbName,
    'primary.oracle_sid': values.primarySid,
    'primary.sid': values.primarySid,
    'primary.oracle_home': values.primaryOracleHome,
    'primary.oracle_base': values.primaryOracleBase,
    'primary.db_unique_name': values.primaryDbUniqueName,
    'primary.sys_password': values.primarySysPassword,
    'primary.service_name': values.primaryServiceName || sharedDbName,
    'primary.listener_port': values.primaryListenerPort,
    'primary.storage_type': values.primaryStorageType,
    'primary.is_cdb': values.primaryIsCdb,
    'primary_ssh.host': values.primaryHost,
    'primary_ssh.port': values.primaryPort,
    'primary_ssh.username': values.primarySshUser,
    'primary_ssh.auth_type': values.primarySshAuthType,
    'standby.oracle_sid': values.standbySid,
    'standby.sid': values.standbySid,
    'standby.oracle_home': values.standbyOracleHome,
    'standby.oracle_base': values.standbyOracleBase,
    'standby.db_unique_name': values.standbyDbUniqueName,
    'standby.sys_password': values.primarySysPassword,
    'standby.service_name': values.standbyServiceName || sharedDbName,
    'standby.listener_port': values.standbyListenerPort,
    'standby.storage_type': values.standbyStorageType,
    'standby.is_cdb': values.standbyIsCdb,
    'standby_ssh.host': values.standbyHost,
    'standby_ssh.port': values.standbyPort,
    'standby_ssh.username': values.standbySshUser,
    'standby_ssh.auth_type': values.standbySshAuthType,
    'data_files_path': defaultDataFilesPath,
    'archivelog_path': defaultArchivePath,
    'storage_type': values.primaryStorageType,
    'duplicate_mode': values.duplicateMode,
    'protection_mode': values.protectionMode,
    'log_transport_mode': values.logTransportMode,
    'enable_realtime_apply': values.enableRealtimeApply,
    'auto_create_srl': values.autoCreateSrl,
    'data_file_path_strategy': values.dataFilePathStrategy,
    'redo_file_path_strategy': values.redoFilePathStrategy,
    'standby_archive_path': values.standbyArchivePath,
    'archive_cleanup_policy': values.archiveCleanupPolicy,
    'archive_cleanup_param': values.archiveCleanupParam,
  }

  if (values.primarySshAuthType === 'key') {
    payload['primary_ssh.private_key_path'] = values.primarySshKeyPath
    if (values.primarySshPassword) {
      payload['primary_ssh.private_key_password'] = values.primarySshPassword
    }
  } else {
    payload['primary_ssh.password'] = values.primarySshPassword
  }

  if (values.standbySshAuthType === 'key') {
    payload['standby_ssh.private_key_path'] = values.standbySshKeyPath
    if (values.standbySshPassword) {
      payload['standby_ssh.private_key_password'] = values.standbySshPassword
    }
  } else {
    payload['standby_ssh.password'] = values.standbySshPassword
  }

  if (values.primaryDataFilePath) {
    payload['primary_data_file_path'] = values.primaryDataFilePath
  }
  if (values.standbyDataFilePath) {
    payload['standby_data_file_path'] = values.standbyDataFilePath
  }
  if (values.primaryRedoFilePath) {
    payload['primary_redo_file_path'] = values.primaryRedoFilePath
  }
  if (values.standbyRedoFilePath) {
    payload['standby_redo_file_path'] = values.standbyRedoFilePath
  }

  return payload
}

function SetupWizard() {
  const navigate = useNavigate()
  const [form] = Form.useForm<SetupFormData>()
  const [currentStep, setCurrentStep] = useState(0)
  const [previewData, setPreviewData] = useState<PreviewResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [validationErrors, setValidationErrors] = useState<ValidationErrorMap>({})
  const [resultFilter, setResultFilter] = useState<'all' | PrecheckResultStatus>('all')
  const [reconfirmKeys, setReconfirmKeys] = useState<string[]>([])
  const [submitLoading, setSubmitLoading] = useState(false)
  const [activeStageKeys, setActiveStageKeys] = useState<string[]>([])
  const [testConnectionLoading, setTestConnectionLoading] = useState(false)
  const [testConnectionType, setTestConnectionType] = useState<'primary' | 'standby' | null>(null)
  const [fieldOverrides, setFieldOverrides] = useState<Record<string, FieldOverrideInfo>>({})
  const [editingField, setEditingField] = useState<OverrideFieldKey | null>(null)
  const [overrideModalVisible, setOverrideModalVisible] = useState(false)
  const [overrideForm] = Form.useForm<{ value: any }>()
  const [demoModeEnabled, setDemoModeEnabled] = useState(false)

  const resetFieldOverrideState = () => {
    setFieldOverrides({})
    setEditingField(null)
    setOverrideModalVisible(false)
    overrideForm.resetFields()
  }

  useEffect(() => {
    form.setFieldsValue(DEFAULT_FORM_VALUES as SetupFormData)
  }, [form])

  useEffect(() => {
    if (!previewData?.execution_plan?.stages?.length) {
      return
    }
    const keys = previewData.execution_plan.stages.map((stage, index) => stage.stage_name || `stage-${index}`)
    setActiveStageKeys(keys)
  }, [previewData])

  // Surface field-level errors and guide the user by scrolling/focusing the first problematic input.
  useEffect(() => {
    if (!validationErrors || !Object.keys(validationErrors).length) {
      return
    }
    if (typeof document === 'undefined') {
      return
    }
    const [firstField] = Object.keys(validationErrors)
    if (!firstField) {
      return
    }
    form.scrollToField(firstField as NamePath, {
      behavior: 'smooth',
      block: 'center',
    })
    const element = document.querySelector(`[name="${firstField}"]`) as HTMLElement | null
    element?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    element?.focus?.()
  }, [validationErrors, form])

  const filteredPrechecks = useMemo(() => {
    if (!previewData?.precheck_results) {
      return []
    }
    if (resultFilter === 'all') {
      return previewData.precheck_results
    }
    return previewData.precheck_results.filter((item) => item.result === resultFilter)
  }, [previewData, resultFilter])

  const riskChecklist = useMemo(() => {
    const blocking = previewData?.risk_summary?.blocking_issues?.length ?? 0
    const highRisk = previewData?.risk_summary?.high_risk_steps?.length ?? 0
    return [
      { key: 'discovery', label: '我已核对自动探测信息，并确认主备主机配置准确' },
      { key: 'blocking', label: `我了解当前 ${blocking} 项阻断风险，已准备整改或接受失败风险` },
      { key: 'highRisk', label: `我确认 ${highRisk} 个高风险步骤需要专人关注与审批` },
    ]
  }, [previewData])

  // 预览调用前置校验，避免缺失字段触发后端 500（Requirement #1）。
  const guardPreviewRequirements = (values: SetupFormData): boolean => {
    const missing = collectPreviewMissingFields(values)
    if (Object.keys(missing).length > 0) {
      setValidationErrors(missing)
      setPreviewError('部分必填字段缺失，请补全后再试')
      message.warning({
        content: '请补全必填字段后再生成预览',
        duration: 2,
      })
      return false
    }
    return true
  }

  const presentPreviewError = (error: unknown) => {
    const fallbackMessage = error instanceof Error ? error.message : '预览失败，请检查输入'
    const axiosError = error as any
    const statusCode = axiosError?.response?.status

    const isRecord = (value: unknown): value is Record<string, any> =>
      typeof value === 'object' && value !== null && !Array.isArray(value)

    // FastAPI HTTPException 响应体结构: { detail: {...} } 或 { detail: "error string" }
    // 注意: axios 的 response.data 就是 FastAPI 返回的 JSON
    const responseData = axiosError?.response?.data
    // detailCandidate 兼容 FastAPI 的 { detail: ... } 以及直接返回字符串的情况
    const detailCandidate = isRecord(responseData) ? responseData.detail : responseData

    const structuredDetail = isRecord(detailCandidate) ? detailCandidate : null
    const detailFallback =
      typeof detailCandidate === 'string'
        ? detailCandidate
        : (typeof responseData === 'string' ? responseData : undefined)
    const detailObj: Record<string, any> = structuredDetail ?? { message: detailFallback ?? fallbackMessage }
    const errorType = typeof detailObj.error_type === 'string' ? detailObj.error_type : undefined
    const detailMessage =
      typeof detailObj.message === 'string' && detailObj.message.trim()
        ? detailObj.message
        : detailFallback ?? fallbackMessage

    // 处理结构化的连接/环境错误
    const shouldTreatAsConnectionError =
      (errorType && CONNECTION_ERROR_TYPES.has(errorType))
      || (statusCode && CONNECTION_ERROR_STATUS_CODES.has(statusCode))

    if (shouldTreatAsConnectionError) {
      const hostInfo = detailObj.host ? `（主机：${detailObj.host}）` : ''
      const isAuthFailure =
        errorType === 'ssh_auth_failed'
        || (statusCode === 401 && !errorType)
      setPreviewError(`${detailMessage}${hostInfo}`)
      Modal.error({
        title: isAuthFailure ? 'SSH 认证失败' : '环境不可达',
        content: (
          <div>
            <p>{detailMessage}</p>
            {detailObj.host && <p>主机：{detailObj.host}</p>}
            {detailObj.hint && <p>建议：{detailObj.hint}</p>}
            {detailObj.details?.error && <p>原始错误：{detailObj.details.error}</p>}
          </div>
        ),
      })
      return
    }

    // 处理 500 错误
    if (statusCode === 500) {
      setPreviewError(`服务器错误 (500)：${detailMessage}`)
      Modal.error({
        title: '预览请求失败',
        content: (
          <div>
            <p>服务器返回 500 错误，可能的原因：</p>
            <ul>
              <li>必填字段缺失或格式不正确</li>
              <li>主/备库 DB_UNIQUE_NAME 配置冲突</li>
              <li>网络或凭据异常</li>
              <li>服务器内部错误</li>
            </ul>
            <p>详细信息：{detailMessage}</p>
          </div>
        ),
      })
      return
    }

    // 其他错误
    setPreviewError(detailMessage)
    const rawDetail = structuredDetail
      ? JSON.stringify(structuredDetail)
      : detailFallback ?? null
    Modal.error({
      title: '预览失败',
      content: rawDetail ? `${detailMessage}\n\n详细信息：${rawDetail}` : detailMessage,
    })
  }

  const precheckColumns: ColumnsType<PrecheckResult> = useMemo(() => [
    {
      title: '检查项',
      dataIndex: 'check_name',
      key: 'check_name',
      render: (text: string, record) => (
        <Space size={6}>
          {record.blocking && <WarningOutlined style={{ color: 'var(--red)' }} />}
          <span style={{ color: record.blocking ? 'var(--red)' : 'var(--text)', fontWeight: record.blocking ? 600 : 500 }}>{text}</span>
        </Space>
      ),
    },
    {
      title: '类别',
      dataIndex: 'category',
      key: 'category',
      render: (value: string) => <Tag style={{ borderColor: 'var(--border)', color: 'var(--text2)', margin: 0 }}>{value || '-'}</Tag>,
    },
    {
      title: '结果',
      dataIndex: 'result',
      key: 'result',
      render: (value: PrecheckResultStatus) => (
        <Tag style={{ borderColor: RESULT_COLORS[value], color: RESULT_COLORS[value], margin: 0 }}>{value.toUpperCase()}</Tag>
      ),
    },
    {
      title: '风险级别',
      dataIndex: 'risk_level',
      key: 'risk_level',
      render: (value: RiskLevel) => (
        <Tag style={{ borderColor: RISK_COLORS[value], color: RISK_COLORS[value], margin: 0 }}>{value.toUpperCase()}</Tag>
      ),
    },
    {
      title: '消息',
      dataIndex: 'message',
      key: 'message',
      ellipsis: true,
    },
    {
      title: '建议',
      dataIndex: 'suggestion',
      key: 'suggestion',
      ellipsis: true,
      render: (value: string) => value || '-',
    },
  ], [])

  const handlePreview = async () => {
    type PreviewErrorDetail = {
      error_type?: string
      message?: string
      role?: string
      host?: string
      hint?: string
      details?: { error?: string }
    }
    type PreviewErrorBody = {
      detail: PreviewErrorDetail | string
    }
    type PreviewApiResponse =
      | APIResponse<PreviewResponse>
      | APIResponse<PreviewErrorBody>
      | PreviewErrorBody

    const ensurePreviewErrorDetail = (
      detail: PreviewErrorDetail | string | undefined,
      fallbackMessage?: string,
    ): PreviewErrorDetail => {
      const defaultMessage = fallbackMessage && fallbackMessage.trim()
        ? fallbackMessage
        : '预览失败，请检查输入'
      if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        return {
          ...detail,
          error_type: detail.error_type ?? 'unknown',
          message: detail.message ?? defaultMessage,
        }
      }
      const normalizedMessage = typeof detail === 'string' && detail.trim()
        ? detail
        : defaultMessage
      return {
        error_type: 'unknown',
        message: normalizedMessage,
      }
    }

    const normalizePreviewError = (
      detail: PreviewErrorDetail | string | undefined,
      fallbackMessage?: string,
      baseError?: any,
    ) => {
      const normalizedDetail = ensurePreviewErrorDetail(detail, fallbackMessage)
      const normalizedMessage = normalizedDetail.message ?? (fallbackMessage ?? '预览失败，请检查输入')
      if (baseError) {
        return {
          ...baseError,
          response: {
            ...(baseError.response ?? {}),
            data: { detail: normalizedDetail },
          },
          message: normalizedMessage,
        }
      }
      return {
        response: {
          data: {
            detail: normalizedDetail,
          },
        },
        message: normalizedMessage,
      }
    }

    const isApiResponseShape = (
      payload: PreviewApiResponse,
    ): payload is APIResponse<PreviewResponse | PreviewErrorBody> =>
      typeof payload === 'object' && payload !== null && 'success' in payload

    const isFastApiErrorResponse = (
      payload: PreviewApiResponse,
    ): payload is PreviewErrorBody =>
      typeof payload === 'object'
      && payload !== null
      && 'detail' in payload
      && !('success' in payload)

    try {
      setPreviewLoading(true)
      setPreviewError(null)
      setValidationErrors({})
      const values = await form.validateFields()
      if (!guardPreviewRequirements(values)) {
        return
      }
      const payload = buildPreviewPayload(values)
      const config = demoModeEnabled ? { params: { demo: true } } : undefined

      const response = await api.post<PreviewApiResponse, PreviewApiResponse>(
        '/oracle/adg/preview',
        payload,
        config,
      )
      if (!response) {
        throw new Error('未获取到预览结果')
      }
      if (isFastApiErrorResponse(response)) {
        const normalizedError = normalizePreviewError(response.detail, '预览失败，请检查输入')
        presentPreviewError(normalizedError)
        return
      }
      if (!isApiResponseShape(response)) {
        throw new Error('预览响应格式不正确')
      }
      if (response.success === false) {
        const detailObj = (response.data as PreviewErrorBody | undefined)?.detail
        const normalizedError = normalizePreviewError(detailObj, response.message)
        presentPreviewError(normalizedError)
        return
      }

      const data = response.data as PreviewResponse | undefined
      if (!data) {
        throw new Error('未获取到预览结果')
      }
      setPreviewData(data)
      resetFieldOverrideState()
      if (data.is_demo) {
        message.info({
          content: '演示数据：本次未连接真实主机',
          duration: 3,
        })
      }
      message.success({
        content: '预览生成成功，进入自动探测结果确认页',
        duration: 2,
      })
      setCurrentStep(1)
      setResultFilter('all')
      setReconfirmKeys([])
    } catch (error) {
      // 将表单校验错误合并成摘要，并提示用户哪些字段阻塞了自动探测。
      if ((error as any)?.errorFields) {
        const fieldErrorMap = buildValidationErrorMap((error as any).errorFields)
        setValidationErrors(fieldErrorMap)
        setPreviewError('部分字段填写有误，请检查下方标红的输入框')
        return
      }
      const axiosError = error as any
      const responseData = axiosError?.response?.data as PreviewApiResponse | undefined
      if (
        responseData
        && isApiResponseShape(responseData)
        && responseData.success === false
      ) {
        const errorPayload = responseData.data as PreviewErrorBody | undefined
        const normalizedError = normalizePreviewError(
          errorPayload?.detail,
          responseData.message ?? axiosError?.message,
          axiosError,
        )
        presentPreviewError(normalizedError)
        console.error('Preview error:', normalizedError)
        return
      }
      if (
        responseData
        && isFastApiErrorResponse(responseData)
      ) {
        const normalizedError = normalizePreviewError(
          responseData.detail,
          axiosError?.message,
          axiosError,
        )
        presentPreviewError(normalizedError)
        console.error('Preview error:', normalizedError)
        return
      }
      presentPreviewError(error)
      console.error('Preview error:', error)
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleRefetchPreview = async () => {
    try {
      setPreviewLoading(true)
      const values = await form.validateFields()
      if (!guardPreviewRequirements(values)) {
        return
      }
      resetFieldOverrideState()
      const payload = buildPreviewPayload(values)
      const config = demoModeEnabled ? { params: { demo: true } } : undefined
      const response = await api.post<APIResponse<PreviewResponse>, APIResponse<PreviewResponse>>(
        '/oracle/adg/preview',
        payload,
        config,
      )
      const data = response?.data
      if (!data) {
        throw new Error('未获取到预览结果')
      }
      setPreviewData(data)
      if (data.is_demo) {
        message.info({
          content: '演示数据：本次未连接真实主机',
          duration: 3,
        })
      }
    } catch (error) {
      if ((error as any)?.errorFields) {
        return
      }
      presentPreviewError(error)
      console.error('Refetch preview error:', error)
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleTestConnection = async (type: 'primary' | 'standby') => {
    if (testConnectionLoading) return
    try {
      setTestConnectionLoading(true)
      setTestConnectionType(type)
      const values = await form.validateFields()
      const payload = type === 'primary'
        ? {
            host: values.primaryHost,
            port: values.primaryPort,
            username: values.primarySshUser,
            auth_type: values.primarySshAuthType,
            key_path: values.primarySshKeyPath,
            password: values.primarySshPassword,
          }
        : {
            host: values.standbyHost,
            port: values.standbyPort,
            username: values.standbySshUser,
            auth_type: values.standbySshAuthType,
            key_path: values.standbySshKeyPath,
            password: values.standbySshPassword,
          }

      const result = await api.post<
        APIResponse<{ success: boolean; message: string }>,
        APIResponse<{ success: boolean; message: string }>
      >(
        '/oracle/adg/test-connection',
        payload,
      )
      const isSuccess = result?.success ?? result?.data?.success ?? false
      const message = result?.message ?? result?.data?.message ?? (isSuccess ? '连接测试成功' : '连接测试失败')
      Modal[isSuccess ? 'success' : 'error']({
        title: `${type === 'primary' ? '主库' : '备库'}连接测试`,
        content: message,
      })
    } catch (error) {
      if ((error as any)?.errorFields) {
        return
      }
      Modal.error({
        title: '连接测试失败',
        content: error instanceof Error ? error.message : '未知错误',
      })
    } finally {
      setTestConnectionLoading(false)
      setTestConnectionType(null)
    }
  }

  const handleNext = () => {
    if (currentStep >= SETUP_STEPS.length - 1) return
    if (currentStep > 0 && !previewData) {
      Modal.warning({
        title: '请先完成预览',
        content: '需要完成自动探测后才能继续下一步。',
      })
      return
    }
    if (currentStep === 1 && !ensureRequiredDiscoveryFields()) {
      return
    }
    setCurrentStep((prev) => prev + 1)
  }

  const handlePrev = () => {
    if (currentStep === 0) return
    setCurrentStep((prev) => prev - 1)
  }

  const handleSubmit = async () => {
    if (!previewData) {
      Modal.warning({
        title: '缺少预览数据',
        content: '请先完成预览流程。',
      })
      return
    }
    if (reconfirmKeys.length < riskChecklist.length) {
      Modal.warning({
        title: '请确认风险',
        content: '提交前需勾选所有风险确认项。',
      })
      return
    }

    try {
      setSubmitLoading(true)
      const values = await form.validateFields()
      const clusterPayload = buildClusterPayload(values)
      const clusterResponse = await api.post<
        APIResponse<{ cluster_id: string }>,
        APIResponse<{ cluster_id: string }>
      >('/clusters', clusterPayload)
      const clusterId = clusterResponse?.data?.cluster_id ?? clusterPayload.cluster_id

      const setupPayload = buildSetupPayload(values)
      const setupResponse = await clusterApi.setupCluster(clusterId, setupPayload)
      const setupResult = setupResponse as unknown as APIResponse<{ task_id: string }>
      const taskId = setupResult?.data?.task_id ?? (setupResponse as unknown as { task_id?: string })?.task_id

      if (!taskId) {
        throw new Error('未获取到任务 ID，请稍后重试')
      }

      Modal.success({
        title: '任务已提交',
        content: '搭建任务已进入队列，正在跳转进度页面。',
        centered: true,
      })
      navigate(`/setup/${taskId}`)
    } catch (error) {
      const message = error instanceof Error ? error.message : '提交失败，请稍后重试'
      Modal.error({
        title: '提交失败',
        content: message,
      })
    } finally {
      setSubmitLoading(false)
    }
  }


  const getFieldDisplayMeta = (fieldKey: OverrideFieldKey): FieldDisplayMeta => {
    const config = FIELD_OVERRIDE_CONFIG[fieldKey]
    const overrideInfo = fieldOverrides[fieldKey]
    const discoveryValue = getDiscoveryValue(previewData?.discovered_info, config.path)
    const formValue = form.getFieldValue(config.formField)
    const previewReady = Boolean(previewData)

    if (overrideInfo) {
      return {
        value: overrideInfo.overrideValue,
        source: 'user_override',
        originalValue: overrideInfo.originalValue,
      }
    }

    const hasDiscoveryValue = !isNil(discoveryValue)
    const hasFormValue = !isNil(formValue)
    let value: any = null
    let source: FieldValueSource = 'planned'

    if (hasDiscoveryValue) {
      value = discoveryValue
      source = 'auto_success'
    } else if (hasFormValue) {
      value = formValue
      source = 'user_input'
    } else {
      value = null
      source = previewReady ? 'auto_failed' : 'planned'
    }

    if (config.role === 'standby' && fieldKey.startsWith('standby_')) {
      const primaryKey = `primary_${fieldKey.replace('standby_', '')}` as OverrideFieldKey
      const primaryConfig = FIELD_OVERRIDE_CONFIG[primaryKey]
      if (primaryConfig) {
        const primaryOverride = fieldOverrides[primaryKey]
        let primaryValue = primaryOverride ? primaryOverride.overrideValue : undefined
        if (isNil(primaryValue)) {
          primaryValue = getDiscoveryValue(previewData?.discovered_info, primaryConfig.path)
        }
        if (isNil(primaryValue)) {
          const primaryFormValue = form.getFieldValue(primaryConfig.formField)
          if (!isNil(primaryFormValue)) {
            primaryValue = primaryFormValue
          }
        }
        if (!isNil(value) && !isNil(primaryValue)) {
          const valuesMatch = typeof value === 'string' && typeof primaryValue === 'string'
            ? value.trim() === primaryValue.trim()
            : Object.is(value, primaryValue)
          if (valuesMatch) {
            source = 'inherit_primary'
          }
        }
      }
    }

    return {
      value,
      source,
      originalValue: discoveryValue,
    }
  }

  const normalizeDiscoveryStatus = (raw?: any): StatusTagType => {
    if (!raw) return 'unknown'
    const keys = Object.keys(DISCOVERY_STATUS_META) as StatusTagType[]
    return keys.includes(raw as StatusTagType) ? raw as StatusTagType : 'unknown'
  }

  const mapConnectivityToStatus = (value?: string): DiscoveryStatus => {
    if (!value) return 'unknown'
    const normalized = value.toLowerCase()
    if (['ok', 'success', 'reachable', 'pass'].includes(normalized)) return 'success'
    if (['fail', 'failed', 'error'].includes(normalized)) return 'failed'
    if (normalized.includes('timeout')) return 'timeout'
    return 'partial'
  }

  const mapPathStatusToStatus = (pathStatus?: string): StatusTagType | undefined => {
    if (!pathStatus) return undefined
    if (pathStatus === 'not_exists') return 'failed'
    if (pathStatus === 'not_writable') return 'warning'
    if (pathStatus === 'ok' || pathStatus === 'ok_exists_files') return 'success'
    return 'partial'
  }

  const mapStatusToSource = (baseSource?: FieldValueSource, status?: StatusTagType): FieldValueSource | undefined => {
    if (!baseSource) return undefined
    if (baseSource !== 'auto_success') return baseSource
    if (!status) return 'auto_success'
    if (status === 'failed' || status === 'unknown') return 'auto_failed'
    if (status === 'warning' || status === 'timeout' || status === 'partial') return 'auto_failed'
    return 'auto_success'
  }

  const renderStatusTag = (status: StatusTagType) => {
    const meta = DISCOVERY_STATUS_META[status]
    return (
      <Tag style={{ borderColor: meta.color, color: meta.color, margin: 0 }}>
        {meta.label}
      </Tag>
    )
  }

  const renderFieldValueRow = ({
    label,
    value,
    source,
    status,
    editableKey,
    extra,
    rowKey,
    customValue,
  }: FieldValueRowProps) => {
    const formattedValue = formatValue(value)
    const displayValue = customValue ?? formattedValue
    const computedKey = rowKey || `${label}-${editableKey ?? ''}`
    const actualSource = mapStatusToSource(source, status)
    return (
      <div key={computedKey} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <Space size={4} wrap align="center">
            <span style={{ fontFamily: "'JetBrains Mono', monospace", color: 'var(--text)' }}>{displayValue}</span>
            {actualSource && (
              <Tag style={{ borderColor: VALUE_SOURCE_TAGS[actualSource].color, color: VALUE_SOURCE_TAGS[actualSource].color, margin: 0 }}>
                {VALUE_SOURCE_TAGS[actualSource].label}
              </Tag>
            )}
            {status && renderStatusTag(status)}
            {extra}
          </Space>
          {editableKey && (
            <Tooltip title="编辑字段">
              <Button
                icon={<EditOutlined />}
                type="text"
                size="small"
                onClick={() => handleStartEditField(editableKey)}
                style={{ color: 'var(--text2)' }}
              />
            </Tooltip>
          )}
        </div>
      </div>
    )
  }

  const renderOverrideFieldRow = (fieldKey: OverrideFieldKey) => {
    const config = FIELD_OVERRIDE_CONFIG[fieldKey]
    const meta = getFieldDisplayMeta(fieldKey)
    const overrideInfo = fieldOverrides[fieldKey]
    const extra = overrideInfo ? (
      <Tooltip title={`自动探测值: ${formatValue(overrideInfo.originalValue)}`}>
        <InfoCircleOutlined style={{ color: 'var(--text2)' }} />
      </Tooltip>
    ) : null
    return renderFieldValueRow({
      rowKey: fieldKey,
      label: config.label,
      value: meta.value,
      source: meta.source,
      editableKey: fieldKey,
      extra,
    })
  }

  const handleStartEditField = (fieldKey: OverrideFieldKey) => {
    setEditingField(fieldKey)
    setOverrideModalVisible(true)
  }

  const handleFieldOverride = (fieldKey: OverrideFieldKey, overrideValue: any) => {
    const config = FIELD_OVERRIDE_CONFIG[fieldKey]
    const originalValue = getDiscoveryValue(previewData?.discovered_info, config.path)
    setFieldOverrides((prev) => {
      const next = { ...prev }
      if (Object.is(originalValue, overrideValue) || (isNil(originalValue) && isNil(overrideValue))) {
        delete next[fieldKey]
      } else {
        next[fieldKey] = {
          fieldName: fieldKey,
          originalValue,
          overrideValue,
          source: config.path[config.path.length - 1],
          label: config.label,
        }
      }
      return next
    })
    form.setFieldsValue({ [config.formField]: overrideValue } as Partial<SetupFormData>)
    setPreviewData((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        discovered_info: setDiscoveryValue(prev.discovered_info, config.path, overrideValue),
      }
    })
  }

  const handleOverrideSave = async () => {
    if (!editingField) return
    try {
      const values = await overrideForm.validateFields()
      const config = FIELD_OVERRIDE_CONFIG[editingField]
      const nextValue = config.type === 'boolean' ? Boolean(values.value) : values.value
      handleFieldOverride(editingField, nextValue)
      setOverrideModalVisible(false)
      setEditingField(null)
      overrideForm.resetFields()
    } catch (error) {
      if ((error as any)?.errorFields) {
        return
      }
    }
  }

  const handleOverrideCancel = () => {
    setOverrideModalVisible(false)
    setEditingField(null)
    overrideForm.resetFields()
  }

  const renderFieldOverrideForm = (config: FieldOverrideConfig, meta: FieldDisplayMeta) => (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Form form={overrideForm} layout="vertical">
        <Form.Item
          name="value"
          label={config.label}
          rules={config.type === 'boolean' ? [] : [{ required: true, message: '请输入覆盖值' }]}
          valuePropName={config.type === 'boolean' ? 'checked' : 'value'}
        >
          {config.type === 'select' ? (
            <Select options={config.options} placeholder="请选择" />
          ) : config.type === 'boolean' ? (
            <Switch checkedChildren="是" unCheckedChildren="否" />
          ) : (
            <Input placeholder={config.placeholder || '请输入覆盖值'} />
          )}
        </Form.Item>
      </Form>
      {config.description && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {config.description}
        </Text>
      )}
      <Alert
        type="info"
        showIcon
        message="自动探测值"
        description={formatValue(meta.originalValue)}
        style={{ borderColor: 'var(--border)', background: 'var(--bg3)' }}
      />
    </Space>
  )

  const renderFieldOverrideModal = () => {
    if (!editingField) return null
    const config = FIELD_OVERRIDE_CONFIG[editingField]
    const meta = getFieldDisplayMeta(editingField)
    return (
      <Modal
        title={`覆盖 ${config.label}`}
        open={overrideModalVisible}
        onOk={handleOverrideSave}
        okText="保存覆盖"
        cancelText="取消"
        onCancel={handleOverrideCancel}
        centered
      >
        {renderFieldOverrideForm(config, meta)}
      </Modal>
    )
  }

  const renderHostDiscoveryCard = (role: 'primary' | 'standby') => {
    const info = previewData?.discovered_info
    const hostInfo = role === 'primary' ? info?.primary_host : info?.standby_host
    const status = normalizeDiscoveryStatus(hostInfo?.status)
    const sshUser = role === 'primary' ? form.getFieldValue('primarySshUser') : form.getFieldValue('standbySshUser')
    const hostLabel = role === 'primary' ? '主库主机' : '备库主机'
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space align="center" size={8}>
            <CloudServerOutlined style={{ color: role === 'primary' ? 'var(--primary)' : 'var(--green)' }} />
            <span style={{ color: 'var(--text)', fontWeight: 600 }}>{hostLabel}</span>
          </Space>
          {renderStatusTag(status)}
        </div>
        {renderFieldValueRow({
          rowKey: `${role}-hostname`,
          label: '主机名',
          value: hostInfo?.hostname,
          source: hostInfo?.hostname ? 'auto_success' : 'auto_failed',
        })}
        {renderFieldValueRow({
          rowKey: `${role}-ip`,
          label: 'IP 地址',
          value: hostInfo?.ip_address,
          source: hostInfo?.ip_address ? 'auto_success' : 'auto_failed',
        })}
        {renderFieldValueRow({
          rowKey: `${role}-os`,
          label: '操作系统',
          value: hostInfo?.os_version || hostInfo?.os_type,
          source: (hostInfo?.os_version || hostInfo?.os_type) ? 'auto_success' : 'auto_failed',
        })}
        {renderFieldValueRow({
          rowKey: `${role}-ssh`,
          label: 'SSH 用户',
          value: sshUser,
          source: 'user_input',
        })}
      </Space>
    )
  }

  const renderOracleDiscoveryCard = (role: 'primary' | 'standby') => {
    const info = previewData?.discovered_info
    if (!info) {
      return (
        <Card title={role === 'primary' ? '主库探测确认' : '备库探测确认'} style={cardStyle} headStyle={{ color: 'var(--text)' }}>
          <Text type="secondary">等待自动探测结果...</Text>
        </Card>
      )
    }
    const oracleInfo = role === 'primary' ? info.primary_oracle : info.standby_oracle
    const oracleStatus = normalizeDiscoveryStatus((oracleInfo as any)?.status)
    const overrideFields = (Object.keys(FIELD_OVERRIDE_CONFIG) as OverrideFieldKey[]).filter(
      (field) => FIELD_OVERRIDE_CONFIG[field].role === role,
    )
    const overrideCount = overrideFields.filter((field) => fieldOverrides[field]).length
    return (
      <Card
        title={`${role === 'primary' ? '主库' : '备库'}探测确认`}
        style={cardStyle}
        headStyle={{ color: 'var(--text)' }}
        extra={overrideCount > 0 ? <Badge color="#fa8c16" text={`${overrideCount} 个字段已覆盖`} /> : null}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {renderHostDiscoveryCard(role)}
          <Divider style={{ borderColor: 'var(--border)', margin: '12px 0' }} />
          <Space align="center" size={8}>
            <DatabaseOutlined style={{ color: 'var(--orange)' }} />
            <Text type="secondary">Oracle 环境</Text>
            {renderStatusTag(oracleStatus)}
          </Space>
          {renderFieldValueRow({
            rowKey: `${role}-version`,
            label: 'Oracle 版本',
            value: oracleInfo?.version,
            source: oracleInfo?.version ? 'auto_success' : 'auto_failed',
          })}
          {renderFieldValueRow({
            rowKey: `${role}-base`,
            label: 'ORACLE_BASE',
            value: oracleInfo?.oracle_base,
            source: oracleInfo?.oracle_base ? 'auto_success' : 'auto_failed',
          })}
          {renderFieldValueRow({
            rowKey: `${role}-sid`,
            label: 'SID',
            value: oracleInfo?.oracle_sid,
            source: oracleInfo?.oracle_sid ? 'auto_success' : 'auto_failed',
          })}
          {renderFieldValueRow({
            rowKey: `${role}-listener`,
            label: '监听端口',
            value: oracleInfo?.listener_port,
            source: oracleInfo?.listener_port ? 'auto_success' : 'auto_failed',
          })}
          {role === 'primary' && renderFieldValueRow({
            rowKey: 'primary-archive-mode',
            label: '归档模式',
            value: (info.primary_db as any)?.archive_mode,
            source: (info.primary_db as any)?.archive_mode ? 'auto_success' : 'auto_failed',
            status: oracleStatus,
          })}
          {role === 'primary' && renderFieldValueRow({
            rowKey: 'primary-force-logging',
            label: 'Force Logging',
            value: (info.primary_db as any)?.force_logging,
            source: (info.primary_db as any)?.force_logging != null ? 'auto_success' : 'auto_failed',
            status: oracleStatus,
          })}
          {overrideFields.map((fieldKey) => renderOverrideFieldRow(fieldKey))}
        </Space>
      </Card>
    )
  }

  const renderNetworkStorageCard = () => {
    const info = previewData?.discovered_info
    const network = info?.network || {}
    const storage = info?.storage || {}
    const storageMeta = storage as Record<string, any>
    const primaryDataStatus = storageMeta?.primary_data_path_status ?? storageMeta?.primary_data_dir_status
    const standbyDataStatus = storageMeta?.standby_data_path_status ?? storageMeta?.standby_data_dir_status
    const primaryLogStatus = storageMeta?.primary_log_path_status ?? storageMeta?.primary_log_dir_status
    const standbyLogStatus = storageMeta?.standby_log_path_status ?? storageMeta?.standby_log_dir_status
    const primaryLogPath = storage.primary_redo_path || storage.primary_redo_file_path
    const standbyLogPath = storage.standby_redo_path || storage.standby_redo_file_path
    const conflicts = info?.conflicts || []
    const precheckWarnings = (previewData?.precheck_results || []).filter((item) => item.result !== 'pass')
    const featureFlagWarnings = precheckWarnings.filter((item) => FEATURE_FLAG_KEYS.includes(item.check_name as any))
    return (
      <Card title="环境与网络摘要" style={cardStyle} headStyle={{ color: 'var(--text)' }}>
        <Row gutter={[24, 24]}>
          <Col span={24} md={12}>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Space align="center" size={8}>
                <ThunderboltOutlined style={{ color: 'var(--yellow)' }} />
                <Text type="secondary">网络与监听</Text>
              </Space>
              {renderFieldValueRow({
                rowKey: 'network-primary-listener',
                label: '主库监听端口',
                value: network.primary_listener_port,
                source: 'auto_success',
                status: network.primary_listener_port ? 'success' : 'unknown',
              })}
              {renderFieldValueRow({
                rowKey: 'network-standby-listener',
                label: '备库监听端口',
                value: network.standby_listener_port,
                source: 'auto_success',
                status: network.standby_listener_port ? 'success' : 'unknown',
              })}
              {renderFieldValueRow({
                rowKey: 'network-listener-status',
                label: '主库监听状态',
                value: network.primary_listener_status,
                source: 'auto_success',
                status: mapConnectivityToStatus(network.primary_listener_status),
              })}
              {renderFieldValueRow({
                rowKey: 'network-log-transport',
                label: '日志传输模式',
                value: network.log_transport_mode,
                source: 'auto_success',
                status: network.log_transport_mode ? 'success' : 'unknown',
              })}
              {renderFieldValueRow({
                rowKey: 'network-tns',
                label: 'TNS 检查',
                value: network.tns_check || '等待后端返回',
                source: network.tns_check ? 'auto_success' : 'planned',
                status: mapConnectivityToStatus(network.tns_check),
                extra: !network.tns_check ? (
                  <Tooltip title="后端尚未返回 TNS 检查结果">
                    <InfoCircleOutlined style={{ color: 'var(--text2)' }} />
                  </Tooltip>
                ) : null,
              })}
            </Space>
          </Col>
          <Col span={24} md={12}>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Space align="center" size={8}>
                <ProfileOutlined style={{ color: 'var(--text2)' }} />
                <Text type="secondary">存储与目录</Text>
              </Space>
              {renderFieldValueRow({
                rowKey: 'storage-primary-path',
                label: '主库数据文件路径',
                value: storage.data_files_path,
                source: storage.data_files_path ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(primaryDataStatus),
              })}
              {renderFieldValueRow({
                rowKey: 'storage-primary-writable',
                label: '主库目录可写',
                value: undefined,
                source: storage.data_files_path ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(primaryDataStatus),
                customValue: formatWritable(undefined, primaryDataStatus),
              })}
              {renderFieldValueRow({
                rowKey: 'storage-standby-path',
                label: '备库数据文件路径',
                value: storage.standby_data_files_path,
                source: storage.standby_data_files_path ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(standbyDataStatus),
              })}
              {renderFieldValueRow({
                rowKey: 'storage-standby-writable',
                label: '备库目录可写',
                value: undefined,
                source: storage.standby_data_files_path ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(standbyDataStatus),
                customValue: formatWritable(undefined, standbyDataStatus),
              })}
              {primaryLogPath && renderFieldValueRow({
                rowKey: 'storage-primary-log-path',
                label: '主库日志文件路径',
                value: primaryLogPath,
                source: primaryLogPath ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(primaryLogStatus),
              })}
              {standbyLogPath && renderFieldValueRow({
                rowKey: 'storage-standby-log-path',
                label: '备库日志文件路径',
                value: standbyLogPath,
                source: standbyLogPath ? 'auto_success' : 'auto_failed',
                status: mapPathStatusToStatus(standbyLogStatus),
              })}
              {renderFieldValueRow({
                rowKey: 'storage-primary-usage',
                label: '主库磁盘占用',
                value: storage.primary_disk_usage,
                source: storage.primary_disk_usage ? 'auto_success' : 'auto_failed',
              })}
              {renderFieldValueRow({
                rowKey: 'storage-standby-usage',
                label: '备库磁盘占用',
                value: storage.standby_disk_usage,
                source: storage.standby_disk_usage ? 'auto_success' : 'auto_failed',
              })}
              {renderFieldValueRow({
                rowKey: 'storage-archive-policy',
                label: '归档清理策略',
                value: storage.archive_cleanup_policy || '未配置',
                source: storage.archive_cleanup_policy ? 'auto_success' : 'user_input',
              })}
            </Space>
          </Col>
        </Row>
        {conflicts.length > 0 && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message="发现冲突项"
            description={
              <Space direction="vertical" size={4}>
                {conflicts.map((conflict) => (
                  <Text key={conflict} style={{ color: 'var(--red)' }}>
                    {conflict}
                  </Text>
                ))}
              </Space>
            }
          />
        )}
        {precheckWarnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 16 }}
            message="预检查提示"
            description={
              <Space direction="vertical" size={4}>
                {precheckWarnings.slice(0, 4).map((item) => (
                  <Space key={item.check_name} size={6} wrap>
                    <Tag style={{ borderColor: RESULT_COLORS[item.result], color: RESULT_COLORS[item.result], margin: 0 }}>
                      {item.result.toUpperCase()}
                    </Tag>
                    <span style={{ color: 'var(--text)' }}>{item.check_name}</span>
                    <Text type="secondary">{item.message}</Text>
                  </Space>
                ))}
                {precheckWarnings.length > 4 && (
                  <Text type="secondary">其余 {precheckWarnings.length - 4} 项请在“预检查”步骤查看</Text>
                )}
              </Space>
            }
          />
        )}
        {featureFlagWarnings.length > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 16 }}
            message="Feature Flag 提醒"
            description={
              <Space direction="vertical" size={4}>
                {featureFlagWarnings.map((item) => (
                  <Space key={item.check_name} size={6} wrap>
                    <ExclamationCircleOutlined style={{ color: 'var(--orange)' }} />
                    <span>{item.message}</span>
                  </Space>
                ))}
              </Space>
            }
          />
        )}
      </Card>
    )
  }

  const ensureRequiredDiscoveryFields = () => {
    const missing = REQUIRED_DISCOVERY_FIELDS
      .filter((item) => item.type !== 'boolean')
      .filter((item) => isNil(form.getFieldValue(item.formField)))
      .map((item) => item.label)
    if (missing.length) {
      Modal.warning({
        title: '请完善关键字段',
        content: `以下字段仍为空：${missing.join('、')}`,
      })
      return false
    }
    return true
  }

  useEffect(() => {
    if (!overrideModalVisible || !editingField) {
      return
    }
    const config = FIELD_OVERRIDE_CONFIG[editingField]
    const meta = getFieldDisplayMeta(editingField)
    let nextValue = meta.value
    if (config.type === 'boolean') {
      nextValue = Boolean(nextValue)
    } else if (isNil(nextValue)) {
      nextValue = undefined
    }
    overrideForm.setFieldsValue({ value: nextValue })
  }, [editingField, overrideModalVisible, overrideForm, previewData, fieldOverrides])

  const renderDemoBanner = () => {
    if (!previewData?.is_demo) {
      return null
    }
    return (
      <Alert
        type="info"
        showIcon
        message="演示数据"
        description="当前展示的数据来自 mock，未连接真实主机。请在正式环境关闭演示模式。"
      />
    )
  }

  const renderDiscoveryStep = () => (
    <>
      <Spin spinning={previewLoading} tip="正在探测环境...">
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <Title level={4} style={{ color: 'var(--text)', marginBottom: 4 }}>
                自动探测结果确认
              </Title>
              <Text type="secondary">核对主备主机、Oracle 环境和网络信息，可对关键字段进行覆盖。</Text>
            </div>
            <Button onClick={handleRefetchPreview} icon={<ReloadOutlined />} disabled={previewLoading}>
              重新探测
            </Button>
          </div>
          {renderDemoBanner()}
          {previewData && (
            <Alert
              type="success"
              showIcon
              message="自动探测已完成"
              description="以下信息来自环境探测，可直接确认或在关键字段上进行覆盖后重新探测。"
              style={{ marginTop: 8 }}
            />
          )}
          {previewError && <Alert type="error" message={previewError} showIcon closable />}
          {previewData?.missing_inputs?.length ? (
            <Alert
              type="warning"
              showIcon
              message="缺失的输入项"
              description={previewData.missing_inputs.join('、')}
            />
          ) : null}
          <Row gutter={[16, 16]}>
            <Col span={24} md={12}>
              {renderOracleDiscoveryCard('primary')}
            </Col>
            <Col span={24} md={12}>
              {renderOracleDiscoveryCard('standby')}
            </Col>
          </Row>
          <Row gutter={[16, 16]}>
            <Col span={24}>{renderNetworkStorageCard()}</Col>
          </Row>
          {previewData?.preview_generated_at && (
            <Alert
              type="info"
              showIcon
              message={`预览生成于 ${formatDateTime(previewData.preview_generated_at)}`}
              style={{ borderColor: 'var(--border)', background: 'var(--bg3)' }}
            />
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, flexWrap: 'wrap' }}>
            <Button onClick={handlePrev}>上一步</Button>
            <Button onClick={handleRefetchPreview} icon={<ReloadOutlined />} disabled={previewLoading}>
              重新探测
            </Button>
            <Button type="primary" onClick={handleNext} disabled={!previewData}>
              继续下一步
            </Button>
          </div>
        </Space>
      </Spin>
      {renderFieldOverrideModal()}
    </>
  )

  const renderPrecheckStep = () => (
    <Spin spinning={previewLoading} tip="正在刷新预检查...">
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Title level={4} style={{ color: 'var(--text)', marginBottom: 4 }}>预检查与风险</Title>
            <Text type="secondary">梳理兼容性、存储和网络风险，阻断项需整改</Text>
          </div>
          <Button onClick={handleRefetchPreview} icon={<ReloadOutlined />} disabled={previewLoading}>
            重新探测
          </Button>
        </div>
        {renderDemoBanner()}
        {previewData?.error_type === 'precheck_failed' && (
          <Alert
            type="error"
            showIcon
            message="预检查存在阻塞项"
            description={previewData?.risk_summary?.blocking_issues?.length
              ? previewData.risk_summary.blocking_issues.join('、')
              : '请根据预检查表中的 FAIL 项进行整改后再提交'}
          />
        )}
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {RESULT_FILTERS.map((filter) => (
            <CheckableTag
              key={filter.key}
              checked={resultFilter === filter.key}
              onChange={() => setResultFilter(filter.key)}
              style={{
                border: '1px solid var(--border)',
                borderRadius: 16,
                padding: '4px 12px',
                cursor: 'pointer',
              }}
            >
              {filter.label}
            </CheckableTag>
          ))}
        </div>
        <Row gutter={16}>
          <Col span={16}>
            <Card style={cardStyle} headStyle={{ color: 'var(--text)' }} title="检查详情">
              <Table
                dataSource={filteredPrechecks}
                columns={precheckColumns}
                rowKey="check_name"
                pagination={false}
                size="middle"
                style={{ background: 'transparent' }}
              />
            </Card>
          </Col>
          <Col span={8}>
            <Card style={cardStyle} headStyle={{ color: 'var(--text)' }} title="风险摘要">
              <Space direction="vertical" size="large" style={{ width: '100%' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <Text type="secondary">总检查</Text>
                  <Text>{previewData?.risk_summary?.total_checks ?? 0}</Text>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <Tag style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>
                    通过 {previewData?.risk_summary?.passed ?? 0}
                  </Tag>
                  <Tag style={{ color: 'var(--orange)', borderColor: 'var(--orange)' }}>
                    警告 {previewData?.risk_summary?.warned ?? 0}
                  </Tag>
                  <Tag style={{ color: 'var(--red)', borderColor: 'var(--red)' }}>
                    失败 {previewData?.risk_summary?.failed ?? 0}
                  </Tag>
                </div>
                {previewData?.risk_summary?.blocking_issues?.length ? (
                  <Alert
                    type="error"
                    showIcon
                    message="阻断项"
                    description={previewData.risk_summary.blocking_issues.join('、')}
                  />
                ) : (
                  <Alert type="success" showIcon message="未发现阻断项" />
                )}
                {previewData?.risk_summary?.high_risk_steps?.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message="高风险步骤"
                    description={previewData.risk_summary.high_risk_steps.join('、')}
                  />
                ) : null}
              </Space>
            </Card>
          </Col>
        </Row>
      </Space>
    </Spin>
  )

  const renderPlanStep = () => (
    <Spin spinning={previewLoading} tip="正在刷新执行计划...">
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Title level={4} style={{ color: 'var(--text)', marginBottom: 4 }}>执行计划预览</Title>
            <Text type="secondary">按阶段分组审阅命令、审批点与耗时估算</Text>
          </div>
          <Button onClick={handleRefetchPreview} icon={<ReloadOutlined />} disabled={previewLoading}>
            重新探测
          </Button>
        </div>
        {renderDemoBanner()}
        <Card style={cardStyle}>
          <Space size="large" wrap>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <CheckCircleOutlined style={{ color: 'var(--green)', fontSize: 20 }} />
              <div>
                <Text type="secondary">步骤数</Text>
                <div style={{ fontSize: 18, fontWeight: 600 }}>{previewData?.execution_plan?.total_steps ?? 0}</div>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <ClockCircleOutlined style={{ color: 'var(--primary)', fontSize: 20 }} />
              <div>
                <Text type="secondary">总耗时估算</Text>
                <div style={{ fontSize: 18, fontWeight: 600 }}>
                  {formatDuration(previewData?.execution_plan?.estimated_total_duration)}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <SafetyCertificateOutlined style={{ color: 'var(--orange)', fontSize: 20 }} />
              <div>
                <Text type="secondary">是否需审批</Text>
                <div style={{ fontSize: 18, fontWeight: 600 }}>
                  {previewData?.execution_plan?.approval_required ? '是' : '否'}
                </div>
              </div>
            </div>
          </Space>
        </Card>
        <Collapse
          activeKey={activeStageKeys}
          onChange={(keys) => setActiveStageKeys(Array.isArray(keys) ? (keys as string[]) : [])}
          expandIconPosition="end"
          style={{ background: 'transparent' }}
        >
          {(previewData?.execution_plan?.stages || []).map((stage, index) => {
            const key = stage.stage_name || `stage-${index}`
            return (
              <Panel
                key={key}
                header={
                  <Space direction="vertical" size={0}>
                    <span style={{ color: 'var(--text)', fontWeight: 600 }}>
                      {stage.display_name || stage.stage_name || `阶段 ${index + 1}`}
                    </span>
                    {stage.description && (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {stage.description}
                      </Text>
                    )}
                  </Space>
                }
                style={cardStyle}
              >
                <Space direction="vertical" style={{ width: '100%' }} size="middle">
                  {(stage.steps || []).map((step) => {
                    const isHighRisk = step.risk_level === 'high' || step.risk_level === 'critical'
                    return (
                      <Card
                        key={step.step_id}
                        style={{
                          ...cardStyle,
                          borderColor: isHighRisk ? 'var(--red)' : 'var(--border)',
                          boxShadow: isHighRisk ? '0 0 10px rgba(255,59,92,0.2)' : 'none',
                        }}
                        bodyStyle={{ display: 'flex', flexDirection: 'column', gap: 12 }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                          <div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                              {isHighRisk ? (
                                <WarningOutlined style={{ color: 'var(--red)' }} />
                              ) : (
                                <ThunderboltOutlined style={{ color: 'var(--primary)' }} />
                              )}
                              <span style={{ fontWeight: 600 }}>{step.title}</span>
                            </div>
                            <Text type="secondary">{step.description}</Text>
                          </div>
                          <Tag style={{ borderColor: RISK_COLORS[step.risk_level], color: RISK_COLORS[step.risk_level] }}>
                            {step.risk_level.toUpperCase()}
                          </Tag>
                        </div>
                        <Space size={8} wrap>
                          <Tag icon={<CloudServerOutlined />} style={{ borderColor: 'var(--border)', color: 'var(--text2)' }}>
                            {step.target_host}
                          </Tag>
                          <Tag icon={<DeploymentUnitOutlined />} style={{ borderColor: 'var(--border)', color: 'var(--text2)' }}>
                            {step.executor_type}
                          </Tag>
                          <Tag
                            icon={<SafetyCertificateOutlined />}
                            style={{
                              borderColor: step.requires_approval ? 'var(--orange)' : 'var(--border)',
                              color: step.requires_approval ? 'var(--orange)' : 'var(--text2)',
                            }}
                          >
                            {step.requires_approval ? '需审批' : '自动执行'}
                          </Tag>
                          <Tag
                            icon={<CheckCircleOutlined />}
                            style={{
                              borderColor: step.rollback_capability ? 'var(--green)' : 'var(--border)',
                              color: step.rollback_capability ? 'var(--green)' : 'var(--text2)',
                            }}
                          >
                            {step.rollback_capability ? '可回滚' : '不可回滚'}
                          </Tag>
                          <Tag icon={<ClockCircleOutlined />} style={{ borderColor: 'var(--border)', color: 'var(--text2)' }}>
                            {formatDuration(step.estimated_duration)}
                          </Tag>
                        </Space>
                        <div>
                          <Text type="secondary">命令预览</Text>
                          <pre
                            style={{
                              marginTop: 8,
                              background: 'var(--bg3)',
                              border: '1px solid var(--border)',
                              borderRadius: 8,
                              padding: 12,
                              fontFamily: "'JetBrains Mono', monospace",
                              color: 'var(--text2)',
                              whiteSpace: 'pre-wrap',
                            }}
                          >
                            {step.command_preview || '-'}
                          </pre>
                        </div>
                      </Card>
                    )
                  })}
                </Space>
              </Panel>
            )
          })}
        </Collapse>
      </Space>
    </Spin>
  )

  const renderConfirmStep = () => (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Title level={4} style={{ color: 'var(--text)', marginBottom: 0 }}>确认并提交</Title>
      <Text type="secondary">请再次确认风险摘要，勾选同意后提交搭建任务</Text>
      {renderDemoBanner()}
      <Row gutter={16}>
        <Col span={14}>
          <Card style={cardStyle} title="风险确认">
            <CheckboxGroup
              value={reconfirmKeys}
              onChange={(checked) => setReconfirmKeys(checked as string[])}
              style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
            >
              {riskChecklist.map((item) => (
                <Checkbox key={item.key} value={item.key} style={{ color: 'var(--text)' }}>
                  {item.label}
                </Checkbox>
              ))}
            </CheckboxGroup>
          </Card>
        </Col>
        <Col span={10}>
          <Card style={cardStyle} title="总体估算">
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <Text type="secondary">总耗时</Text>
                <Text style={{ fontWeight: 600 }}>
                  {formatDuration(previewData?.execution_plan?.estimated_total_duration)}
                </Text>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <Text type="secondary">审批节点</Text>
                <Text style={{ fontWeight: 600 }}>
                  {previewData?.execution_plan?.approval_required ? '存在' : '无需审批'}
                </Text>
              </div>
              <Alert
                type="info"
                showIcon
                message="提交后将立即在后台创建任务，可在“搭建进度”页面实时查看。"
              />
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  )

  const renderFormStep = () => {
    // 将字段级错误集中成摘要，配合上面的滚动逻辑一起引导用户修复。
    const validationErrorEntries = Object.entries(validationErrors)
    const hasValidationErrors = validationErrorEntries.length > 0

    return (
      <Form
        layout="vertical"
        form={form}
        initialValues={DEFAULT_FORM_VALUES}
        style={{ width: '100%' }}
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Alert
            type={demoModeEnabled ? 'warning' : 'info'}
            showIcon
            message={(
              <Space style={{ width: '100%', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>演示模式 / 使用演示数据预览</span>
                  <Switch
                    checked={demoModeEnabled}
                    onChange={(checked) => {
                      if (checked) {
                        Modal.confirm({
                          title: '确认启用演示模式？',
                          content: '启用演示模式后，系统将返回模拟数据，不会连接真实主机。这仅用于演示和测试目的。',
                          okText: '确认启用',
                          cancelText: '取消',
                          onOk: () => setDemoModeEnabled(true),
                        })
                      } else {
                        setDemoModeEnabled(false)
                      }
                    }}
                    size="small"
                  />
                </Space>
              )}
              description={demoModeEnabled
                ? '警告：当前将直接返回 mock 数据，不会连接真实主机。这仅用于演示和测试目的。'
                : '关闭演示模式后，预览会尝试连接真实主机执行探测。适合在没有真实环境时预览功能。'}
          />
          {previewError && (
            <Alert
              type="error"
              showIcon
              message="预览失败"
              description={previewError}
              closable
            />
          )}
          {hasValidationErrors && (
            <Alert
              type="error"
              showIcon
              message="以下字段填写有误"
              description={(
                <Space direction="vertical" size={4}>
                  {validationErrorEntries.map(([field, errorMsg]) => (
                    <Text key={field}>
                      <Text strong>{getFieldLabel(field)}：</Text>
                      {errorMsg}
                    </Text>
                  ))}
                </Space>
              )}
            />
          )}
          <Row gutter={16}>
            <Col span={12}>
              <Card
                style={cardStyle}
                title="主库信息"
                extra={(
                  <Button
                    size="small"
                    icon={<SafetyCertificateOutlined />}
                    onClick={() => handleTestConnection('primary')}
                    loading={testConnectionLoading && testConnectionType === 'primary'}
                  >
                    测试连接
                  </Button>
                )}
              >
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Text type="secondary">角色</Text>
                    <Tag style={{ borderColor: 'var(--primary)', color: 'var(--primary)', background: 'transparent', margin: 0 }}>
                      主库
                    </Tag>
                  </div>
                  <div>
                    <Text style={{ fontWeight: 600, color: 'var(--text)' }}>A. 主机连接信息</Text>
                    <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>提供 SSH 连接参数，可随时测试网络连通性。</Text>
                  </div>
                  <Form.Item
                    name="primaryHost"
                    label="主机地址"
                    rules={[{ required: true, message: '请输入主库主机名或 IP' }]}
                  >
                    <Input placeholder="192.168.1.10" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primaryPort"
                    label="SSH 端口"
                    rules={[{ required: true, message: '请输入端口' }]}
                  >
                    <InputNumber min={1} max={65535} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="primarySshUser"
                    label="SSH 用户"
                    rules={[{ required: true, message: '请输入用户' }]}
                  >
                    <Input placeholder="oracle" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primarySshAuthType"
                    label="认证方式"
                    rules={[{ required: true, message: '请选择' }]}
                  >
                    <Select>
                      <Option value="key">私钥</Option>
                      <Option value="password">密码</Option>
                    </Select>
                  </Form.Item>
                  <Form.Item noStyle shouldUpdate>
                    {({ getFieldValue }) =>
                      getFieldValue('primarySshAuthType') === 'key' ? (
                        <>
                          <Form.Item
                            name="primarySshKeyPath"
                            label="私钥路径"
                            rules={[{ required: true, message: '请输入私钥路径' }]}
                          >
                            <Input placeholder="/home/oracle/.ssh/id_rsa" allowClear />
                          </Form.Item>
                          <Form.Item name="primarySshPassword" label="私钥密码">
                            <Input.Password placeholder="如无可留空" />
                          </Form.Item>
                        </>
                      ) : (
                        <Form.Item
                          name="primarySshPassword"
                          label="SSH 密码"
                          rules={[{ required: true, message: '请输入 SSH 密码' }]}
                        >
                          <Input.Password placeholder="请输入密码" />
                        </Form.Item>
                      )
                    }
                  </Form.Item>
                  <div>
                    <Text style={{ fontWeight: 600, color: 'var(--text)' }}>B. Oracle 实例信息</Text>
                    <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>可通过自动探测回填，也支持手工覆盖。</Text>
                  </div>
                  <Form.Item
                    name="primarySid"
                    label="Oracle SID"
                    rules={[{ required: true, message: '请输入 SID' }]}
                  >
                    <Input placeholder="PRIMDB" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primaryOracleHome"
                    label="ORACLE_HOME"
                    rules={[{ required: true, message: '请输入 ORACLE_HOME' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Input placeholder="/u01/app/oracle/product/19c/dbhome_1" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primaryDbUniqueName"
                    label="DB_UNIQUE_NAME"
                    rules={[{ required: true, message: '请输入主库唯一名' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Input placeholder="ADGPROD_PRIM" allowClear />
                  </Form.Item>
                  <Form.Item name="primaryOracleBase" label="ORACLE_BASE">
                    <Input placeholder="/u01/app/oracle" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primarySysPassword"
                    label="SYS 密码"
                    rules={[{ required: true, message: '请输入 SYS 密码' }]}
                  >
                    <Input.Password placeholder="SYS 用户密码" />
                  </Form.Item>
                  <Form.Item
                    name="primaryListenerPort"
                    label="监听端口"
                    rules={[{ required: true, message: '请输入监听端口' }]}
                  >
                    <InputNumber min={1} max={65535} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="primaryServiceName"
                    label="SERVICE_NAME"
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Input placeholder="PRIMDB" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="primaryStorageType"
                    label="存储类型"
                    rules={[{ required: true, message: '请选择存储类型' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Select>
                      <Option value="fs">文件系统</Option>
                      <Option value="asm">ASM</Option>
                    </Select>
                  </Form.Item>
                  <Form.Item
                    name="primaryIsCdb"
                    label="是否 CDB"
                    rules={[{ required: true, message: '请选择是否为 CDB' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Select>
                      <Option value={true}>是</Option>
                      <Option value={false}>否</Option>
                    </Select>
                  </Form.Item>
                </Space>
              </Card>
            </Col>
            <Col span={12}>
              <Card
                style={cardStyle}
                title="备库信息"
                extra={(
                  <Button
                    size="small"
                    icon={<SafetyCertificateOutlined />}
                    onClick={() => handleTestConnection('standby')}
                    loading={testConnectionLoading && testConnectionType === 'standby'}
                  >
                    测试连接
                  </Button>
                )}
              >
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Text type="secondary">角色</Text>
                    <Tag style={{ borderColor: 'var(--orange)', color: 'var(--orange)', background: 'transparent', margin: 0 }}>
                      备库
                    </Tag>
                  </div>
                  <div>
                    <Text style={{ fontWeight: 600, color: 'var(--text)' }}>A. 主机连接信息</Text>
                    <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>填写备库 SSH 连接参数，保持与主库一致的口令策略。</Text>
                  </div>
                  <Form.Item
                    name="standbyHost"
                    label="主机地址"
                    rules={[{ required: true, message: '请输入备库主机名或 IP' }]}
                  >
                    <Input placeholder="192.168.1.11" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="standbyPort"
                    label="SSH 端口"
                    rules={[{ required: true, message: '请输入端口' }]}
                  >
                    <InputNumber min={1} max={65535} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="standbySshUser"
                    label="SSH 用户"
                    rules={[{ required: true, message: '请输入用户' }]}
                  >
                    <Input placeholder="oracle" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="standbySshAuthType"
                    label="认证方式"
                    rules={[{ required: true, message: '请选择' }]}
                  >
                    <Select>
                      <Option value="key">私钥</Option>
                      <Option value="password">密码</Option>
                    </Select>
                  </Form.Item>
                  <Form.Item noStyle shouldUpdate>
                    {({ getFieldValue }) =>
                      getFieldValue('standbySshAuthType') === 'key' ? (
                        <>
                          <Form.Item
                            name="standbySshKeyPath"
                            label="私钥路径"
                            rules={[{ required: true, message: '请输入私钥路径' }]}
                          >
                            <Input placeholder="/home/oracle/.ssh/id_rsa" allowClear />
                          </Form.Item>
                          <Form.Item name="standbySshPassword" label="私钥密码">
                            <Input.Password placeholder="如无可留空" />
                          </Form.Item>
                        </>
                      ) : (
                        <Form.Item
                          name="standbySshPassword"
                          label="SSH 密码"
                          rules={[{ required: true, message: '请输入 SSH 密码' }]}
                        >
                          <Input.Password placeholder="请输入密码" />
                        </Form.Item>
                      )
                    }
                  </Form.Item>
                  <div>
                    <Text style={{ fontWeight: 600, color: 'var(--text)' }}>B. Oracle 实例信息</Text>
                    <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>按照规划填写拟创建的备库实例参数。</Text>
                  </div>
                  <Form.Item
                    name="standbySid"
                    label="Oracle SID"
                    rules={[{ required: true, message: '请输入备库 SID' }]}
                  >
                    <Input placeholder="STBYDB" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="standbyOracleHome"
                    label="ORACLE_HOME"
                    rules={[{ required: true, message: '请输入 ORACLE_HOME' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Input placeholder="/u01/app/oracle/product/19c/dbhome_1" allowClear />
                  </Form.Item>
                  {/* Requirement #3: 明确要求用户手工提供备库唯一名，杜绝依赖自动探测造成的错误配置。 */}
                  <Form.Item
                    name="standbyDbUniqueName"
                    label="备库 DB_UNIQUE_NAME"
                    rules={[
                      { required: true, message: '请输入备库唯一数据库名' },
                      ({ getFieldValue }) => ({
                        validator(_, value) {
                          if (!value || !value.trim()) {
                            return Promise.resolve()
                          }
                          if (value.trim() === getFieldValue('primaryDbUniqueName')?.trim()) {
                            return Promise.reject(new Error('备库 DB_UNIQUE_NAME 需不同于主库'))
                          }
                          return Promise.resolve()
                        },
                      }),
                    ]}
                    extra="请填写备库的目标唯一数据库名，不再依赖自动探测。"
                  >
                    <Input placeholder="ADGPROD_STBY" allowClear />
                  </Form.Item>
                  <Form.Item name="standbyOracleBase" label="ORACLE_BASE">
                    <Input placeholder="/u01/app/oracle" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="standbyListenerPort"
                    label="监听端口"
                    rules={[{ required: true, message: '请输入监听端口' }]}
                  >
                    <InputNumber min={1} max={65535} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="standbyServiceName"
                    label="SERVICE_NAME"
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Input placeholder="STBYDB" allowClear />
                  </Form.Item>
                  <Form.Item
                    name="standbyStorageType"
                    label="存储类型"
                    rules={[{ required: true, message: '请选择存储类型' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Select>
                      <Option value="fs">文件系统</Option>
                      <Option value="asm">ASM</Option>
                    </Select>
                  </Form.Item>
                  <Form.Item
                    name="standbyIsCdb"
                    label="是否 CDB"
                    rules={[{ required: true, message: '请选择是否为 CDB' }]}
                    extra="自动探测回填，支持手工覆盖"
                  >
                    <Select>
                      <Option value={true}>是</Option>
                      <Option value={false}>否</Option>
                    </Select>
                  </Form.Item>
                </Space>
              </Card>
            </Col>
          </Row>
          <Card style={cardStyle} title="数据库标识与全局配置">
            <Space direction="vertical" size="large" style={{ width: '100%' }}>
              <div>
                <Text style={{ fontWeight: 600, color: 'var(--text)' }}>数据库标识</Text>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginTop: 12 }}
                  message="DB_NAME 自动推导"
                  description="系统会依据主库 DB_UNIQUE_NAME 或 SID 自动推导 DB_NAME，无需重复填写。若需覆盖，请直接编辑实例信息。"
                />
              </div>
              <div>
                <Text style={{ fontWeight: 600, color: 'var(--text)' }}>复制策略</Text>
                <Row gutter={16} style={{ marginTop: 12 }}>
                  <Col span={8}>
                    <Form.Item
                      name="duplicateMode"
                      label="搭建方式"
                      rules={[{ required: true, message: '请选择搭建方式' }]}
                    >
                      <Select>
                        <Option value="backup">备份片 Duplicate</Option>
                        <Option value="active">Active Duplicate</Option>
                      </Select>
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="protectionMode"
                      label="保护模式"
                      rules={[{ required: true, message: '请选择保护模式' }]}
                    >
                      <Select showSearch optionFilterProp="children">
                        {PROTECTION_MODE_OPTIONS.map((mode) => (
                          <Option key={mode} value={mode}>
                            {mode}
                          </Option>
                        ))}
                      </Select>
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="logTransportMode"
                      label="日志传输模式"
                      rules={[{ required: true, message: '请选择日志传输模式' }]}
                    >
                      <Select>
                        {LOG_TRANSPORT_MODE_OPTIONS.map((mode) => (
                          <Option key={mode} value={mode}>{mode}</Option>
                        ))}
                      </Select>
                    </Form.Item>
                  </Col>
                </Row>
                <Row gutter={16}>
                  <Col span={12}>
                    <Form.Item name="enableRealtimeApply" label="启用 Real-Time Apply" valuePropName="checked">
                      <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item name="autoCreateSrl" label="自动创建 Standby Redo Log" valuePropName="checked">
                      <Switch checkedChildren="自动创建" unCheckedChildren="手动配置" />
                    </Form.Item>
                  </Col>
                </Row>
              </div>
              <div>
                <Text style={{ fontWeight: 600, color: 'var(--text)' }}>路径与策略</Text>
                <Row gutter={16} style={{ marginTop: 12 }}>
                  <Col span={12}>
                    <Form.Item
                      name="dataFilePathStrategy"
                      label="备库数据文件路径策略"
                      rules={[{ required: true, message: '请选择策略' }]}
                    >
                      <Select>
                        {PATH_STRATEGY_OPTIONS.map((item) => (
                          <Option key={item.value} value={item.value}>
                            {item.label}
                          </Option>
                        ))}
                      </Select>
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name="redoFilePathStrategy"
                      label="备库联机日志路径策略"
                      rules={[{ required: true, message: '请选择策略' }]}
                    >
                      <Select>
                        {PATH_STRATEGY_OPTIONS.map((item) => (
                          <Option key={item.value} value={item.value}>
                            {item.label}
                          </Option>
                        ))}
                      </Select>
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item shouldUpdate noStyle>
                  {({ getFieldValue }) =>
                    getFieldValue('dataFilePathStrategy') === 'custom' ? (
                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item
                            name="primaryDataFilePath"
                            label="主库数据文件路径前缀"
                            rules={[{ required: true, message: '请输入主库路径前缀' }]}
                          >
                            <Input placeholder="/u01/oradata/PRIM" allowClear />
                          </Form.Item>
                        </Col>
                        <Col span={12}>
                          <Form.Item
                            name="standbyDataFilePath"
                            label="备库数据文件路径前缀"
                            rules={[{ required: true, message: '请输入备库路径前缀' }]}
                          >
                            <Input placeholder="/u02/oradata/STBY" allowClear />
                          </Form.Item>
                        </Col>
                      </Row>
                    ) : null
                  }
                </Form.Item>
                <Form.Item shouldUpdate noStyle>
                  {({ getFieldValue }) =>
                    getFieldValue('redoFilePathStrategy') === 'custom' ? (
                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item
                            name="primaryRedoFilePath"
                            label="主库联机日志路径前缀"
                            rules={[{ required: true, message: '请输入主库联机日志路径前缀' }]}
                          >
                            <Input placeholder="/u01/oradata/PRIM/redo" allowClear />
                          </Form.Item>
                        </Col>
                        <Col span={12}>
                          <Form.Item
                            name="standbyRedoFilePath"
                            label="备库联机日志路径前缀"
                            rules={[{ required: true, message: '请输入备库联机日志路径前缀' }]}
                          >
                            <Input placeholder="/u02/oradata/STBY/redo" allowClear />
                          </Form.Item>
                        </Col>
                      </Row>
                    ) : null
                  }
                </Form.Item>
              </div>
              <div>
                <Text style={{ fontWeight: 600, color: 'var(--text)' }}>备库归档与清理</Text>
                <Row gutter={16} style={{ marginTop: 12 }}>
                  <Col span={12}>
                    <Form.Item
                      name="standbyArchivePath"
                      label="备库本地归档目录"
                      rules={[{ required: true, message: '请输入备库归档目录' }]}
                    >
                      <Input placeholder="/u02/arch" allowClear />
                    </Form.Item>
                  </Col>
                  <Col span={12}>
                    <Form.Item
                      name="archiveCleanupPolicy"
                      label="归档清理策略"
                      rules={[{ required: true, message: '请选择策略' }]}
                    >
                      <Select>
                        {ARCHIVE_CLEANUP_OPTIONS.map((item) => (
                          <Option key={item.value} value={item.value}>
                            {item.label}
                          </Option>
                        ))}
                      </Select>
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item shouldUpdate noStyle>
                  {({ getFieldValue }) => {
                    const policy: ArchiveCleanupPolicy = getFieldValue('archiveCleanupPolicy')
                    if (!policy || policy === 'none') {
                      return null
                    }
                    const label = ARCHIVE_CLEANUP_PARAM_LABELS[policy]
                    return (
                      <Form.Item
                        name="archiveCleanupParam"
                        label={label}
                        rules={[{ required: true, message: `请输入${label}` }]}
                      >
                        <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    )
                  }}
                </Form.Item>
              </div>
            </Space>
          </Card>
        </Space>
      </Form>
    )
  }

  const renderStepContent = () => {
    switch (currentStep) {
      case 0:
        return renderFormStep()
      case 1:
        return renderDiscoveryStep()
      case 2:
        return renderPrecheckStep()
      case 3:
        return renderPlanStep()
      case 4:
        return renderConfirmStep()
      default:
        return null
    }
  }

  return (
    <div
      style={{
        width: '100%',
        height: 'calc(100vh - 100px)',
        overflowY: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 24,
      }}
    >
      <div>
        <Title level={3} style={{ color: 'var(--text)', marginBottom: 12 }}>
          Oracle ADG 搭建向导
        </Title>
        <Text type="secondary">按照 5 个步骤完成自动探测、预检查与任务提交</Text>
      </div>
      <Card style={cardStyle}>
        <Steps current={currentStep} responsive>
          {SETUP_STEPS.map((step) => (
            <Step key={step.key} title={step.title} description={step.description} />
          ))}
        </Steps>
      </Card>
      <Card style={cardStyle} bodyStyle={{ padding: 24 }}>
        {renderStepContent()}
      </Card>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Space>
          {currentStep > 0 && (
            <Button onClick={handlePrev}>上一步</Button>
          )}
        </Space>
        <Space>
          {currentStep === 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
              <Button type="primary" onClick={handlePreview} loading={previewLoading}>
                生成预览并进入下一步
              </Button>
              <Text type="secondary" style={{ fontSize: 12, textAlign: 'right' }}>
                💡 填写必要信息后点击此按钮，将自动探测主备环境并跳转至“结果确认”。
              </Text>
            </div>
          )}
          {currentStep > 0 && currentStep < SETUP_STEPS.length - 1 && (
            <Button type="primary" onClick={handleNext}>
              下一步
            </Button>
          )}
          {currentStep === SETUP_STEPS.length - 1 && (
            <Button type="primary" onClick={handleSubmit} loading={submitLoading}>
              提交搭建
            </Button>
          )}
        </Space>
      </div>
    </div>
  )
}

export default SetupWizard
