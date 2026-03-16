import { useEffect, useState } from 'react'
import { clusterApi } from '@/api'
import { useClusterStore } from '@/store'
import type { ClusterStatus } from '@/api/types'

export function useClusterStatus(clusterId: string) {
  const [status, setStatus] = useState<ClusterStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { setClusterStatus } = useClusterStore()

  const fetchStatus = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await clusterApi.getClusterStatus(clusterId)
      if (response.data?.data) {
        setStatus(response.data.data)
        setClusterStatus(clusterId, response.data.data)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取状态失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchStatus()
  }, [clusterId])

  return { status, loading, error, refetch: fetchStatus }
}
