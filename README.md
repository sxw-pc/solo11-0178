# 婚姻登记跨区预约履约采证台

面向真实业务采录的离线样例项目，包名 `marrydesk`。全部样例均为虚构身份和资源；运行不需要外部服务。

## 安装与运行

`python -m pip install -e . --no-deps`

`python -m marrydesk init --db local.db`

`python -m marrydesk demo --db local.db`

`python -m marrydesk audit --db local.db`

`python -m unittest discover -s tests -v`

## 三个独立入口

### 资格与材料核验

`python -m marrydesk applicant --db applicant.db --fixture fixtures/applicant_normal.json`

失败证据：`fixtures/applicant_failure.json`。材料版本与户籍声明是否满足当前受理规则。持久化对象包括 申请人档案、材料快照，领域模块位于 `marrydesk/applicant.py`。

### 时段与窗口调度

`python -m marrydesk slot --db slot.db --fixture fixtures/slot_normal.json`

失败证据：`fixtures/slot_failure.json`。可用窗口与预约冲突下是否确认或候补。持久化对象包括 窗口日历、预约占位，领域模块位于 `marrydesk/slot.py`。

### 到场办理与回执

`python -m marrydesk visit --db visit.db --fixture fixtures/visit_normal.json`

失败证据：`fixtures/visit_failure.json`。到场身份和材料核验后是否办结或退件。持久化对象包括 签到凭证、办理回执，领域模块位于 `marrydesk/visit.py`。

## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照五类表，可发现被篡改的裁定。

## 业务能力

- **材料版本冻结**：新材料取代旧件时冻结原预约所见版本，改变最终业务可执行状态。

- **双方身份配对**：证件号重复或关系冲突时拒绝资格确认，改变最终业务可执行状态。

- **跨区属地路由**：按双方声明与受理窗口约束确定受理地，改变最终业务可执行状态。

- **窗口容量占位**：同一时段并发请求不得超发确认，改变最终业务可执行状态。

- **候补递补**：取消后按候补顺序及资格有效性递补，改变最终业务可执行状态。

- **停办改约**：窗口停办时保留已确认权益并提供改约结论，改变最终业务可执行状态。

- **到场签到裁定**：时限与身份对应后才允许启动办理，改变最终业务可执行状态。

- **办理回执闭环**：材料撤回或重试时不重复办结，改变最终业务可执行状态。

## 跨系统案例

- 申请人资格在预约确认后追溯失效，资格账本撤销预约占位，到场办理必须拒绝启动并报告该席位释放给候补者。异常：材料撤回事件晚于确认到达时，不能保留原确认或把候补视为超员。

- 窗口停办变更调度承诺，到场签到转为免责改约而非缺席，资格有效期据改约时刻重新核验。异常：停办通知迟到于签到时，必须保留原签到和停办证据并重算责任。

- 办理退件留下材料缺陷和原到场凭证，补正后资格重新批准，调度释放旧占位并保留优先重约权益。异常：退件后重复提交旧回执不能再次办结或抢占新时段。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
