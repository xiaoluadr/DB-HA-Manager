import api from './client'
import type {
  ClusterInfo,
  ClusterStatus,
  APIResponse,
  SwitchoverRequest,
  FailoverRequest,
  ManageRequest,
  TaskStatus,
  SetupCommands,
  ApproveSetupRequest,
  SyncHistoryItem,
  ResourceStatus,
} from './types'

// Re-export types for convenience
export type { SetupCommands } from './types'

/**
 * 集群相关 API
 */
export const clusterApi = {
  // 获取集群列表
  listClusters: () =>
    api.get<APIResponse<ClusterInfo[]>>('/clusters'),

  // 获取集群状态
  getClusterStatus: (clusterId: string) =>
    api.get<APIResponse<ClusterStatus>>(`/clusters/${clusterId}/status`),

  // 获取同步延迟历史
  getSyncHistory: (clusterId: string, params?: { start_time?: string; end_time?: string }) =>
    api.get<APIResponse<SyncHistoryItem[]>>(`/clusters/${clusterId}/sync-history`, { params }),

  // 获取系统资源
  getResources: (clusterId: string) =>
    api.get<APIResponse<ResourceStatus>>(`/clusters/${clusterId}/resources`),

  // 获取搭建命令
  getSetupCommands: (clusterId: string) =>
    api.get<APIResponse<SetupCommands>>(`/clusters/${clusterId}/setup/commands`),

  // 审批并执行搭建
  approveSetup: (clusterId: string, request: ApproveSetupRequest) =>
    api.post<APIResponse<{ task_id: string }>>(`/clusters/${clusterId}/setup/approve`, request),

  // 搭建集群
  setupCluster: (clusterId: string, config: Record<string, any>) =>
    api.post<APIResponse<{ task_id: string }>>(`/clusters/${clusterId}/setup`, config),

  // 执行切换
  executeSwitchover: (clusterId: string, request: SwitchoverRequest) =>
    api.post<APIResponse<Record<string, any>>>(`/clusters/${clusterId}/switchover`, request),

  // 执行接管
  executeFailover: (clusterId: string, request: FailoverRequest) =>
    api.post<APIResponse<Record<string, any>>>(`/clusters/${clusterId}/failover`, request),

  // 管理操作
  manageCluster: (clusterId: string, request: ManageRequest) =>
    api.post<APIResponse<Record<string, any>>>(`/clusters/${clusterId}/manage`, request),
}

/**
 * 任务相关 API
 */
export const taskApi = {
  // 获取任务状态
  getTaskStatus: (taskId: string) =>
    api.get<APIResponse<TaskStatus>>(`/tasks/${taskId}`),

  // 获取任务列表
  listTasks: (params?: { cluster_id?: string; status?: string }) =>
    api.get<APIResponse<TaskStatus[]>>('/tasks', { params }),
}

/**
 * 搭建任务进度 API
 */
export const setupTaskApi = {
  // 使用任务状态接口获取搭建进度
  getSetupProgress: (taskId: string) =>
    taskApi.getTaskStatus(taskId),

  // 取消搭建任务
  cancelSetupTask: (taskId: string) =>
    api.delete<APIResponse<TaskStatus>>(`/tasks/${taskId}`),
}

/**
 * 健康检查 API
 */
export const healthApi = {
  check: () => api.get('/health'),
}
