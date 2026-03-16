当与用户交流时，使用中文。

# DB-HA-Manager 项目协作规则

本文件用于指导 Claude Code 在当前仓库中工作。
目标：在保留项目现有技术约束和上下文的前提下，让 Claude 与 Codex MCP 稳定协同。
## 1. 项目概述

DB-HA-Manager 是一个统一的 Web 管理平台，用于管理多种数据库（Oracle、MySQL、SQL Server、PostgreSQL）的高可用架构。

当前阶段：
- 项目初始化已完成
- 正在根据 UI 参考设计进行前端重构
- 当前最重要的目标是补齐 Oracle ADG 一键搭建的产品闭环

核心实现约束：
- 所有 Oracle 相关操作均通过 SSH 远程执行原生 Oracle 工具（SQL*Plus、RMAN）和操作系统命令
- 当前版本不依赖 Data Guard Broker
## 2. 核心架构
### 核心模式：DatabaseHADriver 接口

所有数据库 HA 实现都应继承统一抽象基类：

```python
class DatabaseHADriver(ABC):
    @abstractmethod
    def setup(self, config: dict) -> str:          # 搭建主备（异步，返回 task_id）

    @abstractmethod
    def status(self) -> dict:                      # 获取状态（同步）

    @abstractmethod
    def switchover(self, dry_run: bool = False) -> dict:  # 切换演练/真实切换

    @abstractmethod
    def failover(self, dry_run: bool = False) -> dict:    # 应急接管演练/真实接管

    @abstractmethod
    def manage(self, action: str, **kwargs) -> any:       # 其他管理（清理归档、手动同步、备份）
Oracle 当前优先实现：

 -setup

 -status

后续逐步扩展：

 -switchover

 -failover

 -manage

```md
## 3. 项目结构（Monorepo）

```text
db_ha_manager/
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI 路由
│   │   ├── core/             # RemoteExecutor (SSH)、SqlExecutor、ConfigManager、Logger
│   │   ├── drivers/          # DatabaseHADriver 基类、OracleDataGuardDriver、MySQLDriver（未来）
│   │   ├── models/           # Pydantic 模型
│   │   ├── tasks/            # 后台任务处理
│   │   └── main.py           # 应用入口
│   ├── scripts/              # RMAN 模板等
├── frontend/
│   └── src/
│       ├── api/              # TypeScript API 客户端
│       ├── components/       # React 组件
│       ├── pages/            # Dashboard、ClusterDetail、SetupWizard、TaskHistory
│       ├── store/            # Zustand 状态管理
│       └── styles/           # 全局样式（深色主题）
├── docker-compose.yml
├── CLAUDE.md
├── PROGRESS.md
└── TROUBLESHOOTING.md

```md
## 4. 技术栈
### 后端
- Python 3.9+
- FastAPI
- python-oracledb（仅用于轻量查询）
- paramiko / asyncssh（SSH 远程执行）
- Pydantic + YAML
- cryptography（敏感信息加密）
- loguru
- 当前后台任务可基于 TaskManager / 线程池，后续可演进到更完整队列
### 前端
- React 18
- TypeScript
- Ant Design 5.x
- Zustand
- React Router v6
- Vite
### API 风格
- RESTful + OpenAPI 3.0
- 异步操作返回 `202 Accepted` 和 `task_id`
- 前端轮询 `GET /api/tasks/{task_id}`
- 后续可兼容 WebSocket 实时推送
## 5. Oracle 实现边界

所有 Oracle Data Guard 操作必须基于：
- SSH
- SQL*Plus
- RMAN
- 操作系统命令

明确禁止：
- 不要引入 Data Guard Broker 作为当前版本依赖
- 不要把状态检查和搭建流程改造成必须依赖 dgmgrl
- 不要在 UI 文案中暗示“当前版本依赖 Broker”

当前 Oracle ADG 关注能力：
- 一键搭建
- 状态查询

后续再做：
- 主备切换
- failover
- 日常监控
- 空间整理
## 6. Claude 与 Codex 的分工
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
- 编写代码
- 修改代码
- 重构逻辑
- 实现 API
- 实现前端组件 / 页面 / store
- 补测试
- 修复 lint / 类型 / 构建问题

原则：
- Claude = 脑
- Codex = 手
## 7. Codex 路由规则（项目内强制）

在开始任何代码修改前，Claude 必须先做一次路由判断。
### 必须先调用 Codex MCP 的任务
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
- 任何 Oracle ADG 一键搭建主链路相关实现
### 可以不调用 Codex、由 Claude 直接处理的任务
仅限以下情况：
- README / Markdown 文档修改
- 纯文案调整
- 纯注释修改
- typo 修复
- 非逻辑性的小配置文本调整
- 小于 20 行且不涉及逻辑的文本级修改
### 默认策略
如果 Claude 对是否需要调用 Codex 存在犹豫，默认调用 Codex。
### 强制说明
如果 Claude 对“代码相关任务”没有调用 Codex，必须明确说明为什么该任务属于极小非逻辑修改。
## 8. Codex MCP 调用要求
### 固定要求
调用 Codex MCP 时，必须使用项目约定参数：

- `model: "gpt-5-codex"`
- `sandbox: "danger-full-access"`
- `approval-policy: "on-failure"`

禁止：
- 使用其他 model 名称
- 漏掉 sandbox
- 漏掉 approval-policy

如果旧文档、示例、记忆中的参数与当前 MCP tool schema 冲突：
- 以当前已注册的 MCP 工具 schema 为准
- 但项目默认目标值仍应是以上三项
### 会话要求
首次调用保存 conversationId。
后续修订优先使用 reply / continuation 机制继续同一会话。
## 9. Claude 对 Codex 结果的审查要求

Codex 完成修改后，Claude 必须检查：

- 是否符合用户目标
- 是否破坏现有 API
- 是否破坏 `setup -> task_id -> task query` 主流程
- 是否违反“Oracle 当前不依赖 Broker”约束
- 是否改动了不必要的文件
- 是否补齐类型、异常处理、状态结构
- 是否符合现有目录结构和代码风格
- 前端是否符合深色主题与 Ant Design 使用规范
- 是否引入过度设计
- 是否存在明显兼容性风险

如果存在问题，Claude 必须继续驱动 Codex 修正，而不是草率接受。
## 10. 当前最优先的产品目标

当前不要发散式开发，优先把“一键搭建 Oracle ADG”做成完整产品链路。
### 优先级最高
1. 自动探测与预检查
2. Preview 执行计划
3. SetupWizard 前端向导页
4. setup 分阶段异步任务编排
5. TaskDetail / 执行监控页
6. 风险确认与失败定位
### 暂缓优先级
- 完整 switchover
- 完整 failover
- 大而全监控系统
- 复杂权限系统
- 大规模持久化与平台化改造
## 11. Oracle ADG 一键搭建实现要求
### 产品流程
一键搭建 Oracle ADG 应围绕以下闭环：

1. 用户输入最少必要参数
2. 系统自动探测主备环境
3. 执行预检查
4. 生成结构化 Preview 执行计划
5. 前端展示风险、步骤、差异项
6. 用户确认风险后提交 setup
7. 后端返回 task_id
8. 前端轮询任务状态
9. 展示阶段、步骤、日志
10. 失败时定位到具体阶段和步骤
### setup 后端阶段建议
至少拆为：

- collect_input_and_validate
- remote_discovery
- precheck
- generate_plan
- prepare_primary
- prepare_standby
- duplicate_standby
- start_managed_recovery
- verify_result
- finalize_report
### 每个 step 至少应包含
- step_id
- stage_name
- step_name
- target_host
- status
- started_at
- finished_at
- stdout / stderr 摘要
- risk_level
- retryable
- rollback_capability
### Preview 接口必须返回结构化数据
至少包含：
- discovered_info
- precheck_results
- execution_plan
- missing_inputs
- risk_summary

不要把 Preview 做成纯字符串说明。
## 12. 前端实现要求
### 当前前端优先补齐的页面与区域
- SetupWizard
- TaskDetail / 执行监控页
- 风险确认区
- 预检查结果表格
- 执行计划 Preview 区域
### SetupWizard 最少包含 5 步
1. 基础信息输入
2. 自动探测结果
3. 预检查与风险
4. 执行计划 Preview
5. 确认并提交
### TaskDetail 页面最少展示
- task_id
- 当前阶段
- 总体状态
- 阶段进度
- 步骤列表
- 日志输出
- 失败原因定位
### 状态更新方式
当前优先使用轮询对接：
- `GET /api/tasks/{task_id}`

后续再演进 WebSocket。
## 13. 前端开发注意事项
### JSX / Ant Design 注意事项
- JSX 属性中必须使用英文标点
- 中文展示内容尽量用组件包裹
- 使用 Ant Design 组件前先核对属性写法
- 深色主题优先使用现有变量和 style 覆盖方式
- 不要把页面写成单页超大表单
- 优先使用向导式多步骤页面
- 页面、组件、store、api 分层清晰
## 14. 后端开发注意事项

- 所有远程操作必须做 try-except 异常处理
- 所有关键步骤必须记录日志
- 尽量保证步骤幂等
- 优先返回结构化数据，不要只返回字符串
- 不要把 setup 写成单个黑盒长函数
- 优先拆成 stage + step
- 失败时必须能定位到具体步骤
- 命令执行应检查退出码
- 尽量使用绝对路径
## 15. 故障排查要求

遇到开发问题前，优先阅读：
- `TROUBLESHOOTING.md`
- 当前相关模块已有实现
- 现有页面 / API / store 结构

不要在未阅读项目上下文的情况下盲目重写。
## 16. 执行方式

Claude 接到开发任务后，按以下顺序执行：

1. 阅读相关代码与现有实现
2. 判断是否命中 Codex 路由条件
3. 若命中，则先调用 Codex
4. 收到结果后做代码审查
5. 若有问题，继续让 Codex 修订
6. 最终再总结修改内容、风险与待验证项

Claude 不得跳过 Codex 直接完成复杂代码实现。
## 17. 常见禁止项

- 不要引入 Data Guard Broker
- 不要把 Preview 做成纯文字说明
- 不要把 setup 结果只做成“成功/失败”两个状态
- 不要省略失败定位
- 不要忽略前端产品闭环
- 不要无理由改动公共 API
- 不要无理由大规模重构全项目
- 不要在没有上下文的情况下替换现有核心抽象






## 浏览器 MCP 调试规则

对于前端页面、交互、联调和 API 调试类任务，Claude 必须优先使用浏览器 MCP（Playwright MCP / Chrome DevTools MCP）进行真实复现，而不是只做静态代码审查。

### 必须优先使用浏览器 MCP 的场景
包括但不限于：

- 页面点击后无响应
- 表单校验异常
- 步骤切换异常
- 控件显示/隐藏异常
- Demo / Mock 模式异常
- Console 报错
- Network 请求失败
- API 4xx / 5xx
- 页面状态与代码预期不一致
- 前后端联调问题
- 用户提供了页面截图、Console 报错、Network 报错，希望定位原因

### 默认执行顺序
遇到上述问题时，Claude 必须按以下顺序执行：

1. 使用 Playwright MCP 打开页面并真实复现问题
2. 使用 Chrome DevTools MCP 查看：
   - Console
   - Network
   - Request Payload
   - Response Body
   - 状态码
3. 基于真实浏览器行为和真实请求响应定位问题
4. 修改代码
5. 修改后再次使用浏览器 MCP 验证
6. 最后再输出结论

### 输出要求
前端调试任务完成后，Claude 必须明确说明：

- 浏览器中真实复现到了什么现象
- Console 里看到了什么报错
- Network 中哪个请求失败了
- 请求参数和响应体是什么
- 根因是前端、后端，还是两者都有
- 修改了哪些文件
- 修复后浏览器里是否已验证通过

### 禁止事项
对于前端交互类问题，Claude 不应：

- 只靠读代码推断问题
- 只给“理论上应该可以”的结论
- 在未实际复现页面行为的情况下宣布修复完成
- 将 `/api/tasks` 等独立报错与当前页面主问题混为一谈，除非已确认它们是同一根因

### 特殊说明
如果浏览器 MCP 无法访问页面，Claude 才可以退回到静态代码分析；但必须明确说明：

- 浏览器 MCP 未能使用的原因
- 因此结论仅为代码级推断，未经浏览器实测验证




## 浏览器 MCP 使用纪律（补充）

当 Claude 使用 Playwright MCP / Chrome DevTools MCP 调试前端时，必须遵守以下纪律，避免把时间浪费在脆弱的 UI 自动化上。

### 1. 浏览器 MCP 的首要用途
浏览器 MCP 优先用于：

- 复现页面现象
- 查看 Console 报错
- 查看 Network 请求
- 检查 Request Payload / Response Body
- 验证修复结果

而不是优先用于：

- 手工录入大量复杂表单
- 反复点击不稳定的下拉框
- 用脆弱 selector 硬拼完整交互流程

### 2. 对复杂表单的处理原则
如果页面表单字段很多（如 SetupWizard 第 1 步）：

- 不要先尝试用浏览器把整张表单完整填完再调试
- 优先使用最小有效输入来复现问题
- 优先直接抓取接口请求和响应
- 必要时可通过更稳定方式设置状态或直接调用接口
- 如果问题的核心在 API / 响应 / 错误处理链路，不要把时间浪费在 UI 录入上

### 3. Ant Design 控件的特殊规则
对于 Ant Design 的 Select / Dropdown / Modal：

- 必须先等待控件展开稳定
- 必须限制在当前打开的 dropdown 容器内查找选项
- 选中后必须确认值已回填，再继续下一步
- 不要使用模糊标题匹配导致命中多个元素
- 不要依赖不稳定的索引 (`nth(...)`) 作为首选方案
- 如果下拉框选择开始频繁失败，应立即停止继续“硬点 UI”

### 4. 何时应停止 UI 自动化
如果出现以下情况，Claude 必须停止继续用 UI 乱点，并切换到更稳定的调试方式：

- Select / Dropdown 多次 strict mode 冲突
- 下拉框未稳定展开就继续点击
- React/Ant Design 事件未正确触发
- 表单尚未稳定填写完成就急于复现主问题
- 自动化步骤明显比问题定位本身更耗时

此时应改为：

- 用 DevTools MCP 看 Network / Response
- 直接调用接口复现
- 用最小输入复现问题
- 通过更稳定方式设置必要状态
- 先修 API / 错误链路，再回浏览器做最小验证

### 5. 调试顺序要求
涉及前端联调时，默认按以下顺序执行：

1. 用浏览器 MCP 观察页面现象
2. 用 DevTools MCP 抓真实请求与响应
3. 基于真实 Network / Response 判断根因
4. 修改代码
5. 回浏览器做最小验证
6. 最后才汇报结论

不要在表单尚未稳定完成时，就急着得出“问题已经复现”或“修复已完成”的结论。

### 6. 输出要求
如果浏览器调试过程中卡在复杂 UI 操作上，Claude 必须明确说明：

- 卡在哪个控件
- 为什么该自动化路径不稳定
- 改成了什么更可靠的调试路径（例如直接抓接口、直接调接口、最小输入复现）
- 最终结论是否来自真实请求/响应，而不是来自一次失败的自动化尝试



## 浏览器现场保护规则

当用户明确说明“当前页面已经手动复现好问题”时，Claude 必须进入“现场勘查模式”，不得重新打开页面、不得重新填写表单、不得重新复现流程。

现场勘查模式下只允许：
1. 使用当前已有页面
2. 读取当前页面的 Console
3. 读取当前页面中已有的 Network 请求与 Response
4. 查看当前页面上已经出现的错误弹窗、DOM 状态、表单状态
5. 基于这些现成诊断数据定位问题
6. 修改代码后，再回到同一个页面做最小验证

严格禁止：
- 新开浏览器页面
- 再次 navigate 到同一路径
- 重新填写复杂表单
- 重新跑整套复现流程
- 因切换工具导致当前页面上下文丢失
- 在已有问题现场之外重新制造一个新现场

如果当前页面现场已经足够诊断问题，Claude 必须优先读取现场数据，而不是重新制造现场。

---

## Playwright 与 Chrome DevTools 职责隔离

默认职责如下：

### Playwright
用于：
- 打开页面（仅在没有现成页面时）
- 最小必要操作
- 点击关键按钮
- 修复后的最终回归验证

不应用于：
- 复杂 Ant Design 表单的大量录入
- 在问题已复现后重跑整套流程
- 与 Chrome DevTools 来回切换导致页面状态丢失

### Chrome DevTools
用于：
- 读取当前页面的 Console
- 读取当前页面的 Network
- 查看 Request Payload / Response Body
- 查看错误弹窗对应的真实接口返回

不应用于：
- 新开空白页再重新调试
- 替代 Playwright 做复杂 UI 操作
- 在已有问题现场之外重新制造一个新现场

---

## 页面结果优先原则

对于前端联调问题，最终验收标准必须是“页面真实最终结果”，而不是代码层、接口层或理论分析结果。

例如：
- 如果目标是修复错误弹窗文案，则只有当页面最终弹窗内容发生预期变化时，才算修复成功。
- 接口单独返回正确 JSON、类型守卫生效、代码路径理论正确，都不能单独作为修复完成依据。
- 只要页面最终弹窗、页面状态、步骤跳转结果仍然不符合预期，就不允许宣称修复成功。

---

## 浏览器 MCP 使用纪律（补充）

当 Claude 使用 Playwright MCP / Chrome DevTools MCP 调试前端时，必须遵守以下纪律，避免把时间浪费在脆弱的 UI 自动化上。

### 1. 浏览器 MCP 的首要用途
浏览器 MCP 优先用于：
- 复现页面现象
- 查看 Console 报错
- 查看 Network 请求
- 检查 Request Payload / Response Body
- 验证修复结果

而不是优先用于：
- 手工录入大量复杂表单
- 反复点击不稳定的下拉框
- 用脆弱 selector 硬拼完整交互流程

### 2. 对复杂表单的处理原则
如果页面表单字段很多（如 SetupWizard 第 1 步）：
- 不要先尝试用浏览器把整张表单完整填完再调试
- 优先使用最小有效输入来复现问题
- 优先直接抓取接口请求和响应
- 必要时可通过更稳定方式设置状态或直接调用接口
- 如果问题的核心在 API、响应或错误处理链路，不要把时间浪费在 UI 录入上

### 3. Ant Design 控件的特殊规则
对于 Ant Design 的 Select / Dropdown / Modal：
- 必须先等待控件展开稳定
- 必须限制在当前打开的 dropdown 容器内查找选项
- 选中后必须确认值已回填，再继续下一步
- 不要使用模糊标题匹配导致命中多个元素
- 不要依赖不稳定的索引 `nth(...)` 作为首选方案
- 如果下拉框选择开始频繁失败，应立即停止继续硬点 UI

### 4. 何时应停止 UI 自动化
如果出现以下情况，Claude 必须停止继续用 UI 乱点，并切换到更稳定的调试方式：
- Select / Dropdown 多次 strict mode 冲突
- 下拉框未稳定展开就继续点击
- React / Ant Design 事件未正确触发
- 表单尚未稳定填写完成就急于复现主问题
- 自动化步骤明显比问题定位本身更耗时

此时应改为：
- 用 DevTools MCP 看 Network / Response
- 直接调用接口复现
- 用最小输入复现问题
- 通过更稳定方式设置必要状态
- 先修 API / 错误链路，再回浏览器做最小验证

### 5. 调试顺序要求
涉及前端联调时，默认按以下顺序执行：
1. 用浏览器 MCP 观察页面现象
2. 用 DevTools MCP 抓真实请求与响应
3. 基于真实 Network / Response 判断根因
4. 修改代码
5. 回浏览器做最小验证
6. 最后才汇报结论

不要在表单尚未稳定完成时，就急着得出“问题已经复现”或“修复已完成”的结论。

### 6. 输出要求
如果浏览器调试过程中卡在复杂 UI 操作上，Claude 必须明确说明：
- 卡在哪个控件
- 为什么该自动化路径不稳定
- 改成了什么更可靠的调试路径（例如直接抓接口、直接调接口、最小输入复现）
- 最终结论是否来自真实请求/响应，而不是来自一次失败的自动化尝试
