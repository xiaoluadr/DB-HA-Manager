// 标准联调样例配置
export interface SetupPresetData {
  // 主库信息
  primaryHost: string
  primaryPort: number
  primarySshUser: string
  primarySshAuthType: 'key' | 'password'
  primarySshKeyPath?: string
  primarySshPassword?: string

  // 主库 Oracle 信息
  primaryOracleHome: string
  primaryDbUniqueName: string
  primaryOracleBase?: string
  primarySysPassword?: string
  primaryListenerPort: number
  primaryServiceName?: string
  primaryStorageType: 'fs' | 'asm'
  primaryIsCdb: boolean
  primarySid?: string

  // 备库信息
  standbyHost: string
  standbyPort: number
  standbySshUser: string
  standbySshAuthType: 'key' | 'password'
  standbySshKeyPath?: string
  standbySshPassword?: string

  // 备库 Oracle 信息
  standbyOracleHome: string
  standbySid: string
  standbyDbUniqueName: string
  standbyListenerPort: number
  standbyServiceName?: string
  standbyStorageType: 'fs' | 'asm'
  standbyIsCdb: boolean

  // 搭建配置
  duplicateMode: 'active' | 'backup'
  protectionMode: 'MAXIMUM PERFORMANCE' | 'MAXIMUM AVAILABILITY' | 'MAXIMUM PROTECTION'
  logTransportMode: 'ASYNC' | 'SYNC'
  enableRealtimeApply: boolean
  autoCreateSrl: boolean

  // 路径策略
  dataFilePathStrategy: 'custom' | 'auto_same' | 'auto_append'
  redoFilePathStrategy: 'custom' | 'auto_same' | 'auto_append'

  // 路径
  primaryDataFilePath?: string
  standbyDataFilePath?: string
  primaryRedoFilePath?: string
  standbyRedoFilePath?: string

  // 归档
  standbyArchivePath: string
  archiveCleanupPolicy: 'none' | 'days' | 'size' | 'files'
  archiveCleanupParam?: number
}

// 标准联调样例配置
export const STANDARD_PRESET: SetupPresetData = {
  // 主库连接信息
  primaryHost: '192.168.123.10',
  primaryPort: 22,
  primarySshUser: 'oracle',
  primarySshAuthType: 'password',
  primarySshPassword: 'oracle',

  // 主库 Oracle 信息
  primaryOracleHome: '/u01/app/oracle/product/11.2.0/db_1',
  primaryDbUniqueName: 'orcl',
  primaryOracleBase: '/u01/app/oracle',
  primarySysPassword: 'oracle',
  primaryListenerPort: 1521,
  primaryServiceName: 'orcl',
  primaryStorageType: 'fs',
  primaryIsCdb: false,
  primarySid: 'orcl',

  // 备库连接信息
  standbyHost: '192.168.123.10',
  standbyPort: 22,
  standbySshUser: 'oracle',
  standbySshAuthType: 'password',
  standbySshPassword: 'oracle',

  // 备库 Oracle 信息
  standbyOracleHome: '/u01/app/oracle/product/11.2.0/db_1',
  standbySid: 'orcl_std',
  standbyDbUniqueName: 'orcl_std',
  standbyListenerPort: 1521,
  standbyServiceName: 'orcl_std',
  standbyStorageType: 'fs',
  standbyIsCdb: false,

  // 搭建配置
  duplicateMode: 'active',
  protectionMode: 'MAXIMUM PERFORMANCE',
  logTransportMode: 'ASYNC',
  enableRealtimeApply: true,
  autoCreateSrl: false,

  // 路径策略
  dataFilePathStrategy: 'custom',
  redoFilePathStrategy: 'custom',

  // 路径
  primaryDataFilePath: '/u01/app/oracle/oradata/ORCL/datafile',
  standbyDataFilePath: '/u01/app/oracle/oradata/ORCLSTD/datafile',
  primaryRedoFilePath: '/u01/app/oracle/oradata/ORCL/onlinelog',
  standbyRedoFilePath: '/u01/app/oracle/oradata/ORCLSTD/onlinelog',

  // 归档
  standbyArchivePath: '/u02/arch',
  archiveCleanupPolicy: 'none',
}
