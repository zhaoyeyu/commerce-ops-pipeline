# 店有序 · 运行与交接说明

## 输入与输出

供应商商品CSV → `pipeline.py` → 标准目录、草稿导入文件、异常清单与SQLite商品记录。
订单JSON → 订单去重 → 整单库存校验 → 预留 → 履约任务 → 适配器回执 → 扣减与状态回写。
结算JSON → 订单金额/币种/费用/实收核对 → 匹配或差异清单。
客户文本 → AI结构化分类 → 对应店铺订单快照 → 待确认工单 → 操作记录。

`python -m commerce_ops export <folder>`导出`orders.csv`、`inventory.csv`、`exceptions.csv`、`settlements.csv`、`cases.csv`与`summary.json`。商品导入另外保留逐行异常及批次清单。

## API

除`GET /health`外均需要`Authorization: Bearer <COMMERCE_OPS_TOKEN>`。

|方法|路径|输入或结果|
|---|---|---|
|POST|`/inventory`|`sku`、`on_hand`、非空`snapshot_id`，可选`warehouse`|
|POST|`/orders`|`store`、`external_id`、`lines`、可选`currency`；每行含SKU、正整数数量与十进制单价|
|GET|`/orders/<id>`|当前订单|
|POST|`/orders/<id>/reserve`|补充库存后重试整单预留|
|POST|`/orders/<id>/queue`|创建或返回履约任务|
|POST|`/orders/<id>/cancel`|撤销未执行订单并释放预留|
|GET|`/tasks/<id>`|任务结果和回执|
|POST|`/settlements`|`ref`、店铺/订单号、`gross`、`fee`、`net`、`currency`|
|POST|`/cases`|`store`、工单外部号、`ticket`、`classification`|
|POST|`/cases/<id>/resolve`|`note`：实际处理说明|
|GET|`/report`|当前工作区报表|

当前令牌代表一个运营工作区管理员。多店铺订单隔离使用店铺与外部订单号，仓库库存可以共享；这不等于多个独立租户的权限系统。

## 状态与恢复

订单：NEW → RESERVED或EXCEPTION；RESERVED → FULFILLED或CANCELLED。

任务：READY → RUNNING → DONE、RETRY、UNKNOWN或FAILED。只有明确的对端未受理结果允许RETRY，最多5次。UNKNOWN不再自动提交。过期进程租约通过`recover --before <UTC时间>`转UNKNOWN，再按原任务ID查询对端回执。

已完成回执在同一事务中扣减实物库存与预留量。重放库存快照不会把已出库商品重新补回。运行中和结果未知的订单先对账再取消。已履约订单取消会要求单独的退货流程；当前没有退货或退款执行功能。

缺货：查看异常明细 → 导入新的实际库存快照 → 对原订单`reserve` → `queue`。
结算差异：核对来源参考号、币种、订单总额、平台费用及实收 → 修正输入来源后保留新的调整记录。

## 履约适配器

`POST /fulfillments`接收订单，并使用`Idempotency-Key`作为持久化业务请求标识。
`GET /fulfillments/<key>`独立返回执行状态。

- `COMPLETED`必须有`receipt_id`。
- `ACCEPTED`表示已经受理但尚无最终结果。
- `NOT_ACCEPTED`仅用于明确没有执行的提交响应。
- `NOT_FOUND`查询结果不自动触发重新提交，避免复制延迟或在途请求造成重复。

适配器需在执行前持久化唯一标识，可封装平台API、ERP服务或由RPA执行器领取。当前通用HTTP协议已验收；未声称已认证某一商业ERP、影刀或n8n专用插件。

## 验证

`python -m unittest -v`覆盖事务、金额、跨店铺标识、并发预留、取消、回执、快照重放、工单和结算。
`python acceptance.py --output <全新目录>`运行实际双HTTP链路与500单并发场景，输出逐项验收结果和业务报表。已运行的目录不会覆盖，便于保留前后对比。

AI分类的实际结果在示例中。分类器使用只读临时目录和数据输出协议，不把生成内容当作订单取消或资金操作的授权。
