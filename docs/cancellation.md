# 停演、漏录与上座预测口径（第四轮扩展）

本文件补充 README 的预测口径。核心变化：**停演不再写成 0 人验票**。

## 1. 场务登记停演，而不是补零

|项目|规则|
|---|---|
|登记入口|`POST /performances/{id}/cancellation`（已有场次）或 `POST /productions/{pid}/cancellations`（按日期，含没有场次记录的漏录日）|
|权限|admin / steward；viewer 只读|
|原因|至少 2 个非空字符，记录登记人与 UTC 时间|
|可登记对象|未录验票、未确认点名的场次；已录验票（包括 0 人）的场次拒绝，因为那已经是真实开演|
|效果|`Performance.status = cancelled`，清空 `forecast`/`raw_forecast`；关联的未确认 `CrewCall` 变 `invalidated`|
|不可恢复|本轮未提供撤销停演；登记前请确认日期|
|历史冻结|历史区间内补登停演会改变训练日历。若剧目已有确认点名，拒绝（与验票冻结一致）|

停演是独立记录 `performance_cancellations`（一场一条，主键 `performance_id` 唯一），不改写
`performances.arrivals`。这样"没开演"和"开演但零观众"在数据上可区分。

## 2. 旧数据升级

采用纯增量升级：新增一张表，原表无新列、无回填。

- 已录的 `arrivals = 0`：照旧是真实开演日，计入训练。
- `arrivals IS NULL` 且没有停演记录：仍是漏录，继续阻止预测。
- `arrivals IS NULL` 但有停演记录：视为停演日，关闭缺口但不作为 0 人观测。
- 完全没有场次记录的日期：同样是漏录；可通过按日期登记停演关闭。
- `status` 新增取值 `cancelled`，旧行的 `observed/scheduled/forecasted/crew_confirmed` 不变。

## 3. 训练与回测

- 训练序列仍是逐日索引，星期节奏不变。停演日保留在索引中，值用**同星期最近观测**
  （两侧都有时线性插值）填充，而不是 0。
- 回测：更早的 49 天拟合，最近 7 天只做验证，**留出周的验票不进入训练拟合**。
- 误差 `backtest_mae` 只对留出周里有验票的开演日求平均；停演日/无验票日标记
  `counted=false` 并给出原因，不计入误差。若留出周全部是停演，预测失败并说明原因。
- 完整历史重拟合后预测未来 7 天。未来日期仍按**原日历日期**连续排布；
  停演日保留日期占位，但不产生 `forecast`，也不产生点名，列入 `skipped_future`。
- 证据字段新增：`error_days`、`cancelled_days`、`missing_days`、`train_recorded_days`、
  `backtest[].counted/error/reason`、`skipped_future`。

## 4. 并发：停演 vs 预测发布

- 预测 worker 与停演登记都以 `productions` 行为发布锁（`SELECT … FOR UPDATE`），
  并遵循同一锁序：production → performance → crew_call。
- worker 发布前会重新读取 `forecast_state`；若排队期间已被停演或输入修订改为非
  `calculating`，整次结果丢弃（`StaleForecast`），不写回旧预测。
- 停演落库后：
  - 若影响历史日历：清空整周未确认预测与草案，`forecast_state = idle`。
  - 若只影响未来一周内某天：从已发布证据中移除该日（`future` 删除、`skipped_future`
    追加），其余日期保留；该场次 `forecast` 置空。
- 因此"刚停演的场次"在两种提交顺序下都不会被旧预测恢复成可排班状态；
  确认点名、编制点名、实名排班都会复核 `cancelled`。

## 5. 页面

|页面|新增内容|
|---|---|
|演出日历|未来七场按原日期展示；停演日灰化并标注"不出预测"；登记停演表单|
|验票历史|"停演与漏录核对"表：漏录日期可一键登记停演；停演记录含原因/登记人/时间|
|上座预测|留出周逐日表（实际/预测/误差/是否计误差）；停演日与计误差日期清单；未来表含停演行|
|场务点名|可编班场次排除停演日；确认/编制入口均受后端 `cancelled` 约束|

`GET /cancellations`、`GET /calendar-status`（按剧目）与 `GET /productions/{pid}/calendar-status`
为页面提供停演、漏录与预测可用性状态，三角色可读。

## 6. 已知范围

- 本轮没有撤销停演、没有"停演后补演改期"，也不自动顺延到下一周。
- 停演原因只留本地数据库，不通知票务或外部系统。
- 预测仍为单剧目每日一场的既有假设。
