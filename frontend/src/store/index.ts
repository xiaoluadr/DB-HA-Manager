import { create } from 'zustand'
import { taskApi } from '@/api'
import type {
  ClusterInfo,
  ClusterStatus,
  TaskStatus,
  SetupProgressDetail,
  SetupLogItem,
} from '@/api/types'

interface TaskCounts {
  running_count: number
  pending_count: number
}

interface ClusterStore {
  clusters: ClusterInfo[]
  clusterStatusMap: Record<string, ClusterStatus>
  loading: boolean
  searchKeyword: string
  taskCounts: TaskCounts
  setupTask: SetupProgressDetail | null
  setupLogs: SetupLogItem[]

  setClusters: (clusters: ClusterInfo[]) => void
  setClusterStatus: (clusterId: string, status: ClusterStatus) => void
  setLoading: (loading: boolean) => void
  updateCluster: (clusterId: string, info: Partial<ClusterInfo>) => void
  setSearchKeyword: (keyword: string) => void
  fetchTaskCounts: () => Promise<void>
  setSetupTask: (task: SetupProgressDetail | null) => void
  setSetupLogs: (logs: SetupLogItem[]) => void
}

export const useClusterStore = create<ClusterStore>((set) => ({
  clusters: [],
  clusterStatusMap: {},
  loading: false,
  searchKeyword: '',
  taskCounts: {
    running_count: 0,
    pending_count: 0,
  },
  setupTask: null,
  setupLogs: [],

  setClusters: (clusters) => set({ clusters }),
  setClusterStatus: (clusterId, status) =>
    set((state) => ({
      clusterStatusMap: { ...state.clusterStatusMap, [clusterId]: status },
    })),
  setLoading: (loading) => set({ loading }),
  updateCluster: (clusterId, info) =>
    set((state) => ({
      clusters: state.clusters.map((c) =>
        c.cluster_id === clusterId ? { ...c, ...info } : c
      ),
    })),
  setSearchKeyword: (keyword) => set({ searchKeyword: keyword }),
  fetchTaskCounts: async () => {
    try {
      const response = await taskApi.listTasks()
      const payload = response.data?.data ?? []
      const tasks: TaskStatus[] = Array.isArray(payload) ? payload : []

      const counts = tasks.reduce<TaskCounts>(
        (acc, task) => {
          const status = task.status?.toLowerCase()
          if (status === 'running') acc.running_count += 1
          if (status === 'pending') acc.pending_count += 1
          return acc
        },
        { running_count: 0, pending_count: 0 }
      )

      set({ taskCounts: counts })
    } catch (error) {
      console.error('Failed to fetch task counts', error)
    }
  },
  setSetupTask: (task) => set({ setupTask: task }),
  setSetupLogs: (logs) => set({ setupLogs: logs }),
}))
