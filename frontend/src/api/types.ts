/**
 * API 类型定义
 * 根据 OpenAPI 规范生成
 */

// 共享枚举
export type DuplicateMode = 'backup' | 'active_duplicate'
export type ProtectionMode =
  | 'MAXIMUM PERFORMANCE'
  | 'MAXIMUM AVAILABILITY'
  | 'MAXIMUM PROTECTION'
export type LogTransportMode = 'ASYNC' | 'SYNC'
export type ArchiveCleanupPolicy = 'none' | 'days' | 'size' | 'files'

// 基础信息
export interface ClusterInfo {
  cluster_id: string
  cluster_name: string
  db_type: string
  created_at: string
  description?: string
}

export interface PrimaryStatus {
  role: string
  host: string
  connected: boolean
  database_role?: string
  open_mode?: string
  protection_mode?: string
  last_archived_sequence?: string
}

export interface StandbyStatus {
  role: string
  host: string
  connected: boolean
  database_role?: string
  open_mode?: string
  protection_mode?: string
  mrp_status?: string
  applied_sequence?: string
  last_applied_time?: string
}

export interface SyncStatus {
  status: string
  lag_seconds: number
  lag_sequence: number
  gap: boolean
}

export interface SyncHistoryItem {
  timestamp: string
  lag_seconds: number
  status: string
}

export interface ClusterStatus {
  cluster_id: string
  cluster_name: string
  timestamp: string
  primary: PrimaryStatus
  standby: StandbyStatus
  sync: SyncStatus
  error?: string
}

export interface TablespaceUsage {
  tablespace_name: string
  mb_used: number
  mb_max: number
  usage_percent: number
}

export interface ArchiveLogRetention {
  name: string
  size_mb: number
}

export interface ResourceTotals {
  tablespace_used_mb: number
  tablespace_total_mb: number
  archive_total_mb: number
}

export interface ResourceStatus {
  cluster_id: string
  collected_at: string
  tablespaces: TablespaceUsage[]
  archive_logs: ArchiveLogRetention[]
  totals: ResourceTotals
}

export interface TaskStatus {
  task_id: string
  action: string
  cluster_id: string
  status: string
  progress: number
  created_at: string
  updated_at: string
  completed_at?: string
  result?: Record<string, any>
  logs: string[]
}

export interface APIResponse<T = any> {
  success: boolean
  message?: string
  data?: T
}

// 请求类型
export interface SwitchoverRequest {
  dry_run: boolean
  comment?: string
}

export interface FailoverRequest {
  dry_run: boolean
  comment?: string
  confirm: boolean
}

export interface ManageRequest {
  action: 'sync' | 'cleanup_archive' | 'backup' | 'recovery'
  parameters: Record<string, any>
}

// 搭建命令相关类型
export interface SetupCommand {
  command: string
  description: string
  type: 'sql' | 'ssh' | 'rman'
  target: 'primary' | 'standby'
}

export interface SetupCommands {
  [stepName: string]: SetupCommand[]
}

export interface SetupRequest {
  cluster_id: string
  config: Record<string, any>
}

export interface ApproveSetupRequest {
  steps: string[]
}

// 阶段化搭建相关类型
export type SetupStageStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped'

export interface SetupStepStatusMeta {
  step_id: string
  step_name: string
  display_name: string
  description?: string
  target_host?: string
  status: SetupStageStatus
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  stdout?: string
  stderr?: string
  exit_code?: number
  risk_level?: 'low' | 'medium' | 'high' | 'critical'
  is_idempotent?: boolean
  retry_count?: number
  error_message?: string
  checkpoint_data?: Record<string, any>
}

export interface SetupStageDetail {
  stage_name: string
  display_name: string
  description?: string
  status: SetupStageStatus
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  steps: SetupStepStatusMeta[]
  error_message?: string
  summary?: Record<string, any>
}

export interface SetupTaskProgress {
  current_stage?: string
  total_stages?: number
  completed_stages?: number
  stages?: Record<string, SetupStageDetail>
  progress_percent?: number
  elapsed_seconds?: number
  overall_status?: SetupStageStatus
}

// 旧的步骤相关类型（兼容）
export type LegacySetupStepStatus = 'pending' | 'running' | 'completed' | 'warning' | 'error' | 'skipped'

export interface SetupLogItem {
  timestamp: string
  step?: string
  level?: string
  message: string
  result?: string
}

// 兼容旧的步骤状态类型（扩展以支持后端返回的状态值）
export type SetupStepStatus = LegacySetupStepStatus | 'success' | 'failed'

export interface SetupProgressSummary {
  // 新的阶段化结构字段
  current_stage?: string
  total_stages?: number
  completed_stages?: number
  stages?: Record<string, SetupStageDetail>
  progress_percent?: number
  elapsed_seconds?: number
  overall_status?: SetupStageStatus

  // 兼容旧的字段
  current_step?: string
  step_status?: Record<string, SetupStepStatus>
}

export interface SetupStepDetail {
  step?: string
  status?: SetupStepStatus
  message?: string
  error?: string
  [key: string]: any
}

export interface SetupProgressDetail {
  task: TaskStatus
  progress?: SetupProgressSummary | null
  results?: Record<string, SetupStepDetail>
  logs: SetupLogItem[]
}

export type SetupTask = SetupProgressDetail

// ----- Preview & Discovery Types -----

export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'

export type PrecheckResultStatus = 'pass' | 'warn' | 'fail'

export type DiscoveryStatus =
  | 'success'
  | 'failed'
  | 'timeout'
  | 'unavailable'
  | 'missing'
  | 'partial'
  | 'skipped'
  | 'unknown'

export interface OracleEnvInfo {
  version?: string
  oracle_home?: string
  oracle_sid?: string
  oracle_base?: string
  storage_type?: string
  listener_port?: number
  is_cdb?: boolean
  db_unique_name?: string
  service_name?: string
}

export interface DiscoveryInfo {
  primary_host: Record<string, any>
  standby_host: Record<string, any>
  primary_oracle: OracleEnvInfo
  standby_oracle: OracleEnvInfo
  primary_db: Record<string, any>
  network: Record<string, any>
  storage: Record<string, any>
  conflicts: string[]
}

export interface PrecheckResult {
  check_name: string
  category: string
  result: PrecheckResultStatus
  message: string
  evidence?: Record<string, any>
  suggestion?: string
  blocking: boolean
  risk_level: RiskLevel
}

export interface PlanStep {
  step_id: string
  title: string
  description: string
  target_host: string
  executor_type: string
  risk_level: RiskLevel
  requires_approval: boolean
  rollback_capability: boolean
  estimated_duration?: number
  command_preview: string
}

export interface PlanStage {
  stage_name?: string
  display_name?: string
  description?: string
  steps?: PlanStep[]
}

export interface ExecutionPlan {
  stages: PlanStage[]
  total_steps: number
  estimated_total_duration?: number
  approval_required: boolean
}

export interface RiskSummary {
  total_checks: number
  passed: number
  warned: number
  failed: number
  blocking_issues: string[]
  high_risk_steps: string[]
}

export interface PreviewResponse {
  discovered_info: DiscoveryInfo
  precheck_results: PrecheckResult[]
  execution_plan: ExecutionPlan
  missing_inputs: string[]
  risk_summary: RiskSummary
  preview_generated_at: string
  is_demo?: boolean
  error_type?: string
}

export interface PreviewRequest {
  primary_host: string
  primary_ssh_port: number
  primary_ssh_user: string
  primary_ssh_auth_type: string
  primary_ssh_key_path?: string
  primary_ssh_password?: string
  standby_host: string
  standby_ssh_port: number
  standby_ssh_user: string
  standby_ssh_auth_type: string
  standby_ssh_key_path?: string
  standby_ssh_password?: string
  oracle_sid: string
  oracle_home: string
  sys_password?: string
  db_name: string
  db_unique_name_primary?: string
  db_unique_name_standby?: string
  storage_type?: string
  duplicate_mode: DuplicateMode
  protection_mode: ProtectionMode
  log_transport_mode?: LogTransportMode
  enable_realtime_apply?: boolean
  auto_create_srl?: boolean
  data_files_path?: string
  data_file_path_strategy?: string
  redo_file_path_strategy?: string
  primary_data_file_path?: string
  standby_data_file_path?: string
  primary_redo_file_path?: string
  standby_redo_file_path?: string
  archivelog_path: string
  standby_archive_path: string
  archive_cleanup_policy: ArchiveCleanupPolicy
  archive_cleanup_param?: number
  primary_sid?: string
  primary_oracle_home?: string
  primary_oracle_base?: string
  primary_db_unique_name?: string
  primary_listener_port?: number
  primary_service_name?: string
  primary_storage_type?: string
  primary_is_cdb?: boolean
  standby_sid?: string
  standby_oracle_home?: string
  standby_oracle_base?: string
  standby_db_unique_name?: string
  standby_listener_port?: number
  standby_service_name?: string
  standby_storage_type?: string
  standby_is_cdb?: boolean
}

export interface FieldOverrideInfo {
  fieldName: string
  originalValue: any
  overrideValue: any
  source: string
  label?: string
}
