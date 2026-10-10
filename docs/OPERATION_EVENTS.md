# 原生 operation 完成提示

`codepier.operation.completed.v1` 在用户明确订阅后，将原生 operation 的当前终态摘要放入既有 MCP Events outbox。它不创建 operation、不重放命令、不自动续聊，也不是执行授权。仍依赖协作及 Events 功能开关、现有原生宿主订阅流程和独立 webhook challenge；本实现没有打开生产开关或创建真实订阅。

## 订阅范围

参数必须包含不可变 `project_id`、`environment_id` 和 1–16 个互不重复的原 `operation_ids`。可选 `slot_id` 保留现有加入位置的成员、队列与独立回调校验。无 slot 的订阅是明确支持的独立 grant 订阅；它仍通过专用 operation 校验，不能省略订阅授权。

每个 ID 必须已经存在，属于当前 grant、用户、Space、项目，并能通过 task_query 使用的实时 operation 读取权限检查。即使记录的 visibility 为 space，也不能订阅其他 grant 的 operation。拥有 read/write/execute 的 grant 可以订阅自己可读的原操作；通知保留原 operation reader 的工具专用检查（例如桌面结果的 computer 权限），不另外授予执行能力。

environment_id 只表示用户选择的通知上下文，不证明原操作是在该环境执行。workspace_id 来自原 operation 的受控 args_summary，不从通知环境推导。事件不包含命令、路径、日志、输出、结果正文、请求 payload、回调或凭据。项目映射仅在私有订阅快照中保存摘要，不进入事件。

## 完成与重连语义

Runtime 的真实终态 UPDATE 与 operation_completed hook 在同一 Store transaction 内完成。hook 同步执行，不访问网络；outbox/contract 写入失败会回滚这次完成更新，防止提交结果后丢失相应提示。没有匹配的有效订阅、开关关闭或当前授权不再允许通知时不产生事件。通知失败不会重放原业务 operation。

订阅 challenge 成功后的提交事务会对最多 16 个指定 ID 做一次当前终态核对，覆盖 challenge 期间刚好完成的竞态。它不是历史扫描。每个订阅按原 operation ID、终态、output_seq 生成稳定去重键，并在同一事务登记精确 delivery。普通续订、重新建立相同订阅和重复 completion 不重发已有 accepted 或 abandoned 回执；只有原 Events 明确游标重放机制能请求受限历史重放。不同通知上下文或不同回调订阅有独立 delivery。

支持 succeeded、failed、cancelled、needs_review、interrupted 提示。needs_review 和 interrupted 表示应核查原回执，不表示执行成功；后续可靠结果升级为 succeeded 等状态会形成独立提示。unknown、queued、running、reconnecting 和 cancelling 不产生完成提示。取消请求或取消 ACK 本身不是已停止执行的证据，以原 operation 的 durable cancelled 终态为准。

订阅验证前后、完成写入、投递预留与发送前都检查当前权限。身份快照、profile、角色、项目映射、原工作目录或 owner/Space 变化会停止旧订阅提示；撤权、房间/订阅暂停、过期也受既有约束。预留后原状态升级时，过时提示会被放弃，读取当前原回执才是依据。

提交新的 operation delivery 后，通过 after_commit 合并唤醒既有 Events dispatcher，无需等待固定轮询间隔。回滚不唤醒；同一 dispatcher 串行运行，不为每个通知创建新任务。保留 2 秒超时扫描作为丢失提示、重启及退避的兜底。唤醒只表示待投递数据已提交，不保证网络或宿主处理延迟。

## 宿主读取

真实 payload 包含 schema_version、project_id、environment_id、operation_id、state、output_seq、recipient_grant_id、workspace_id 和固定的只读 next_call。next_call 是：

- name: task_query
- operation: get
- operation_ids: 原 ID 的单元素列表
- include_output / include_result: true
- output_limit: 8000

宿主可沿用自己已保存的 after_output_seq 读取增量，不要把事件里的最新 output_seq 当作已消费的输出位置。必须使用原 operation ID，不能重新调用 exec/write/edit，也不能把事件视作自动续聊许可。测试 payload 明确 test=true，next_call=null，不创建新工作。

事件继续使用既有签名、二次验权、fenced retry、订阅版本、游标、outbox 与过期机制。HTTP 2xx 的状态严格为 accepted，仅证明接收端 HTTP 接受，不证明宿主消费、聊天收到或用户已看到。

## 验证与限制

测试使用临时 SQLite、真实 Store/Runtime 权限与事务，以及注入的内存 receiver；不访问公网、不调用浏览器、不创建真实宿主订阅或回调凭据。覆盖精确 ID、跨 grant/owner/Space/project 拒绝、challenge 竞态、bounded catch-up、角色/profile/映射变化、暂停/到期/撤权、取消未知结果、needs_review 升级、事务回滚、续订去重和面板测试事件。

本地 mock 通过不代表 ChatGPT 或其他宿主已发现事件、建立订阅、消费通知或自动续聊。真实宿主兼容性与目标聊天验收仍需在用户明确授权后独立验证。
