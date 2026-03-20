export const SETUP_WIZARD_PRESET = {
  // 主备库配置
  primaryHost: '192.168.123.10',
  standbyHost: '192.168.123.10',

  // SSH 配置
  primarySshUser: 'oracle',
  primarySshPassword: 'oracle',
  primarySshAuthType: 'password' as const,
  standbySshUser: 'oracle',
  standbySshPassword: 'oracle',
  standbySshAuthType: 'password' as const,

  // 主库 Oracle 配置
  primarySid: 'orcl',
  primaryDbUniqueName: 'orcl',
  primaryOracleHome: '/u01/app/oracle/product/11.2.0/db_1',
  primaryListenerPort: 1521,

  // 备库 Oracle 配置
  standbySid: 'orcl_std',
  standbyDbUniqueName: 'orcl_std',
  standbyOracleHome: '/u01/app/oracle/product/11.2.0/db_1',
  standbyListenerPort: 1521,

  // 全局配置
  duplicateMode: 'active_duplicate' as const,
  protectionMode: 'MAXIMUM PERFORMANCE',
  logTransportMode: 'ASYNC' as const,
  enableRealtimeApply: true,
  autoCreateSrl: false,

  // 路径配置
  dataFilePathStrategy: 'custom' as const,
  primaryDataFilePath: '/u01/app/oracle/oradata/ORCL/datafile',
  standbyDataFilePath: '/u01/app/oracle/oradata/ORCLSTD/datafile',

  redoFilePathStrategy: 'custom' as const,
  primaryRedoFilePath: '/u01/app/oracle/oradata/ORCL/onlinelog',
  standbyRedoFilePath: '/u01/app/oracle/oradata/ORCLSTD/onlinelog',

  // 归档配置
  standbyArchivePath: '/u01/arch',
  archiveCleanupPolicy: 'none' as const,
} as const
