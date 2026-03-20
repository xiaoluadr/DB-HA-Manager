当与用户交流时，使用中文。

# DB-HA-Manager 项目协作规则

本文件用于指导 Claude Code 在当前仓库中工作，目标是在保留项目现有技术约束和上下文的前提下，让 Claude 与 Codex MCP 稳定协同。

## 1. 项目目标与边界

DB-HA-Manager 是统一的数据库高可用管理平台。当前阶段最重要目标是补齐 **Oracle ADG 一键搭建闭环**。

当前优先实现：
- Oracle ADG setup
- Oracle 状态查询
- Preview / 预检查 / 执行计划 / TaskDetail 闭环

暂缓：
- 完整 switchover
- 完整 failover
- 大而全监控系统
- 复杂权限系统
- Data Guard Broker 依赖

明确约束：
- 所有 Oracle 相关操作均通过 SSH 远程执行原生 Oracle 工具（SQL*Plus、RMAN）和操作系统命令
- 当前版本 **不依赖 Data Guard Broker**
- 不在 UI 或文档中暗示当前版本必须依赖 Broker

## 2. 核心架构与目录

### 核心抽象
所有数据库 HA 实现都应继承统一抽象基类 `DatabaseHADriver`。

Oracle 当前优先实现：
- `setup`
- `status`

后续逐步扩展：
- `switchover`
- `failover`
- `manage`

### 关键目录
- `backend/app/api/`：FastAPI 路由
- `backend/app/core/`：RemoteExecutor、SqlExecutor、ConfigManager、Logger
- `backend/app/drivers/`：各数据库 HA Driver
- `backend/app/models/`：Pydantic 模型
- `backend/app/tasks/`：后台任务处理
- `frontend/src/pages/`：SetupWizard、TaskDetail 等页面
- `frontend/src/api/`：前端 API 客户端
- `frontend/src/store/`：状态管理

## 3. 当前产品闭环要求

### 一键搭建闭环
必须围绕以下流程实现：
1. 用户输入最少必要参数
2. 系统自动探测主备环境
3. 执行预检查
4. 生成结构化 Preview 执行计划
5. 前端展示风险、步骤、差异项
6. 用户确认风险后提交 setup
7. 后端返回 `task_id`
8. 前端轮询任务状态
9. 展示阶段、步骤、日志
10. 失败时定位到具体阶段和步骤

### Preview 返回必须结构化
至少包含：
- `discovered_info`
- `precheck_results`
- `execution_plan`
- `missing_inputs`
- `risk_summary`

禁止把 Preview 做成纯字符串说明。

### Setup 建议阶段
至少拆为：
- `collect_input_and_validate`
- `remote_discovery`
- `precheck`
- `generate_plan`
- `prepare_primary`
- `prepare_standby`
- `duplicate_standby`
- `start_managed_recovery`
- `verify_result`
- `finalize_report`

每个 step 至少包含：
- `step_id`
- `stage_name`
- `step_name`
- `target_host`
- `status`
- `started_at`
- `finished_at`
- `stdout/stderr` 摘要
- `risk_level`
- `retryable`
- `rollback_capability`

## 4. 前后端实现要求

### 前端
当前优先补齐：
- `SetupWizard`
- `TaskDetail / 执行监控页`
- 风险确认区
- 预检查结果表格
- 执行计划 Preview 区域

`SetupWizard` 最少包含 5 步：
1. 基础信息输入
2. 自动探测结果
3. 预检查与风险
4. 执行计划 Preview
5. 确认并提交

`TaskDetail` 最少展示：
- `task_id`
- 当前阶段
- 总体状态
- 阶段进度
- 步骤列表
- 日志输出
- 失败原因定位

当前状态更新优先使用轮询：
- `GET /api/tasks/{task_id}`

### 后端
- 所有远程操作必须做异常处理
- 所有关键步骤必须记录日志
- 优先保证步骤幂等
- 优先返回结构化数据，不要只返回字符串
- 不要把 `setup` 写成单个黑盒长函数
- 失败时必须能定位到具体步骤
- 命令执行应检查退出码
- 尽量使用绝对路径

## 5. Claude 与 Codex 分工

### Claude 负责
- 理解需求
- 搜索和阅读仓库上下文
- 分析现有实现与缺口
- 拆解任务
- 判断是否需要调用 Codex
- 审查 Codex 产出的代码
- 检查兼容性、约束、风险
- 汇总结果并向用户说明

### Codex 负责
- 编写和修改代码
- 重构逻辑
- 实现 API / schema / 页面 / 组件 / store
- 修复 bug
- 补测试
- 修复 lint / 类型 / 构建问题

原则：Claude = 脑，Codex = 手。

## 6. Codex 调用规则（强制）

### 必须先调用 Codex 的任务
只要命中任意一条，就必须先调用 Codex：
- 修改任何 `.py`、`.ts`、`.tsx`、`.js`、`.jsx` 等代码文件
- 修改测试文件
- 修改 API、schema、Pydantic models、TypeScript types
- 修改前端组件、hooks、store、路由、状态流
- 修改后端业务逻辑、异步流程、任务编排、重试逻辑
- 涉及跨文件改动
- 涉及新功能开发
- 涉及 bug 修复
- 代码改动预计超过 20 行
- 需要补测试或修复 lint / 类型 / 构建问题
- 任何 Oracle ADG 主链路相关实现

### 可以不调用 Codex 的任务
仅限：
- README / Markdown 文档修改
- 纯文案调整
- 纯注释修改
- typo 修复
- 非逻辑性的小配置文本调整
- 小于 20 行且不涉及逻辑的文本级修改

### 默认策略
如果 Claude 对是否需要调用 Codex 存在犹豫，默认调用 Codex。

### Codex 固定参数
调用 Codex MCP 时默认使用：
- `model: "gpt-5-codex"`
- `sandbox: "danger-full-access"`
- `approval-policy: "on-failure"`

首次调用保存会话；后续优先沿用同一会话继续修订。

## 7. Codex 修改后的审查规则（强制）

调用 Codex 修改后，Claude 必须按顺序执行：
1. `Read` 查看修改后的关键函数
2. `git diff` 查看实际改动
3. 运行最小验证（如 build / typecheck / pytest / curl）
4. 再输出审查结论

### 禁止行为
- 禁止凭印象宣布“有 typo / 有小 bug / 有严重问题”
- 禁止在没有 `Read + diff + 验证` 之前就下结论
- 如果只是怀疑某处有问题，只能说“怀疑，需要核实”，不能直接说“已确认有 bug”

### 审查时必须检查
- 是否符合用户目标
- 是否破坏现有 API
- 是否破坏 `setup -> task_id -> task query` 主流程
- 是否违反“不依赖 Broker”约束
- 是否改动了不必要的文件
- 是否补齐类型、异常处理、状态结构
- 是否符合现有目录结构和代码风格
- 前端是否符合深色主题与 Ant Design 使用规范
- 是否引入过度设计或明显兼容性风险

如果存在问题，Claude 必须继续驱动 Codex 修正，而不是草率接受。

## 8. 浏览器 / MCP 联调规则

### 总原则
浏览器 MCP 只用于：
- 真实复现用户在页面上的问题
- 查看 Network / Console / 页面状态
- 验证修改结果

### 禁止事项
- 不要为了填复杂表单而长时间卡在下拉框操作上
- 不要在已经有“一键填充 / 预设 / form.setFieldsValue”能力时继续手工逐项填写
- 不要同时混用多个浏览器上下文导致页面错乱
- 不要在没有必要时重新打开新页面

### 推荐做法
- 若项目已有联调预设 / 一键填充，优先使用
- 若问题已在用户当前页面复现，优先基于当前页面继续分析
- 浏览器联调的重点应放在：
  - 请求是否发出
  - 请求体 / 响应体是什么
  - 页面是否正确展示
  - Console / Network 是否有真实报错

## 9. Oracle / SSH / SQL 排障规则

### 禁止的错误路径
后续禁止：
- 使用裸 ssh 命令做密码认证调试，例如 `ssh oracle@host ...`
- 任何依赖交互输入密码的 bash ssh 调试方式
- 因为 `ssh_askpass`、`Permission denied` 等失败就跳过问题

### 正确排查顺序
1. 优先走项目现有链路：
   - `/api/oracle/adg/preview`
   - `OraclePreviewExecutor`
   - `RemoteExecutor / SqlExecutor`
2. 优先看后端日志，再决定是否改代码
3. 如需定位 SQL 问题，先加最小必要日志，再触发真实 preview
4. 未拿到完整 stdout/stderr 前，不要猜测是“解析问题”

### SQL 执行建议
在当前无代理模式下，优先采用更稳定的方式执行 SQL，例如：
- 使用标准 sql 脚本文件 + `sqlplus @file.sql`
- 或其他经过验证、能稳定返回 stdout 的方式

不要继续使用：
- `echo "...sql..." | sqlplus`
- `cat file | sqlplus /nolog`
- `/nolog + CONNECT / AS SYSDBA` 这类不稳定组合

### 排查结论格式
排查 SQL 相关问题时，最终必须明确回答：
- SQL 有没有执行到
- stdout / stderr 分别是什么
- 是“没执行 / 执行失败 / 解析失败 / 未赋值 / 未调用”中的哪一类

## 10. 执行顺序

Claude 接到开发任务后，按以下顺序执行：
1. 阅读相关代码与现有实现
2. 判断是否命中 Codex 路由条件
3. 若命中，则先调用 Codex
4. 收到结果后做代码审查
5. 若有问题，继续让 Codex 修订
6. 最终再总结修改内容、风险与待验证项

不要在未阅读上下文的情况下盲目重写；不要无理由大规模重构全项目。

## 11. 常见禁止项

- 不要引入 Data Guard Broker 作为当前版本依赖
- 不要把 Preview 做成纯文字说明
- 不要把 setup 结果只做成“成功/失败”两个状态
- 不要省略失败定位
- 不要忽略前端产品闭环
- 不要无理由改动公共 API
- 不要在没有上下文的情况下替换现有核心抽象
