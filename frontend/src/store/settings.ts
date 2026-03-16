import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type Environment = 'PROD' | 'DEV' | 'TEST'
export type ThemeMode = 'dark' | 'light'
export type RefreshInterval = 30000 | 60000 | 120000

interface SettingsState {
  environment: Environment
  refreshInterval: RefreshInterval
  theme: ThemeMode
  setEnvironment: (environment: Environment) => void
  setRefreshInterval: (interval: RefreshInterval) => void
  setTheme: (theme: ThemeMode) => void
}

export const DEFAULT_REFRESH_INTERVAL: RefreshInterval = 30000

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      environment: 'PROD',
      refreshInterval: DEFAULT_REFRESH_INTERVAL,
      theme: 'dark',
      setEnvironment: (environment) => set({ environment }),
      setRefreshInterval: (refreshInterval) => set({ refreshInterval }),
      setTheme: (theme) => set({ theme }),
    }),
    {
      name: 'dbha-settings',
      version: 1,
    }
  )
)
