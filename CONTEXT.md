# Token-Board V2 领域上下文

## 边界

Token-Board 的实时配置、历史事实和 Dashboard 存档是三个不同边界：

- **实时配置**是当前仍可被路由或计费解析器读取的物理行。不存在物理行就不存在配置；删除不写墓碑、收据或排除标记。
- **历史事实**包括请求日志、请求尝试、稳定账户身份、冻结费用、分摊和账单导出事件。这些记录描述已经发生的事情，不拥有实时配置的生命周期。
- **Dashboard 存档**只保存已经由导出水位线确认的用量和周期费用事实。已固化的金额是不可变展示事实，不因 Token-Board 实时配置删除而减少；删除 Dashboard 用户会物理删除其账户、用量和周期费用，不生成替代行。

## 生命周期

- `enabled=1/0` 只表达启用或暂停。暂停的 Key、上游、软件和订阅实例仍是有效订阅，继续产生订阅费用。
- `ends_at` 只表达已经确认的未来合同结束时间。到期后由生命周期模块物理删除实时行；它不是通用删除状态。
- API 账户、客户端 Key、Agent 软件和绑定立即硬删除。Plan 和 Agent 订阅可以把每个计费单元安排到自己的周期末结束。
- 删除实时配置不会追溯删除冻结费用。没有新的事实时，已删除配置不会因为同步或报表查询重新出现；另一台机器后来导出的新事实可以重新建立身份和 Dashboard 账户。当前消费报表只把仍能通过实时配置关联的历史事实计入。

## 计费

`BillingUnitResolver` 是当前计费单元的唯一读取接口：Plan 使用每把 credential，Agent 使用每个 subscription instance。账单自然键为 `billing_unit_id + period_start`，常规运行只物化当前周期，不循环补生成过去周期。

冻结费用是不可变账务事实。费用从 pending 变为 finalized 时，在同一事务追加一个 `billing_export_event`；相同自然事件和相同载荷重复应用是幂等操作，载荷冲突必须失败。

Agent 订阅的源费用可以在没有软件绑定时存在，但没有 allocation，因此不进入实际消费，也不导出到 Dashboard。

## 报表窗口

Token-Board 当前消费统计使用 UTC 滚动窗口。冻结账单只提供金额和周期；当前统计是否仍有资格计入由实时物理行判断。实际消费由三项相加：

```text
SUM(仍关联当前账户/凭据/软件的 request_log.billed_usage_cost，requested_at 在窗口内)
+ SUM(仍存在账户、上游、credential、recurring/credential 合同的 finalized proxy normalized_recurring_cost，period_start 在窗口内)
+ SUM(仍存在订阅、实例、软件和有效绑定的 finalized agent allocations，period_start 在窗口内)
```

`enabled=0` 不影响订阅计费；`ends_at` 过期后不再计入。`account_identities`、`billing_export_events` 和冻结账单本身不能证明实时对象仍存在。周期费用归属到 `period_start` 当日，Dashboard 固化卡片与每日图表按存档事实保持一致。

Dashboard 的历史卡片、月度和每日固化账单读取 Dashboard 存档本身，不重新用实时配置过滤；Token-Board 当前统计接口与 Dashboard 固化展示是两个独立口径。

## 导出水位线

Dashboard 导出维护独立的请求水位线和账单水位线。一次导出先恢复 pending、捕获两个最大 ID，再读取各自 `(mark, max]` 闭区间事实，按需写身份、写用量和周期费用，发布候选后原子推进水位线。

删除事务会先纳入本机水位线以前的增量，再物理删除 Dashboard 行。相同水位线重试不会复活；真正的新请求或新账单是新的事实，可以重新出现。多机写入同一账单自然键使用绝对值幂等，不累加；载荷不一致报告冲突。

## V2 约束

V2 节点只读写 V2 结构。V2 manifest 发布后，后来的 V1 产物被拒绝，V1 节点进入只读/暂停写入；不提供 V1/V2 双写或字段兼容层。SQLite 自增序列和水位线必须保留，升级通过 shadow copy、自动备份和外键检查完成。
