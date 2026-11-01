# 店账清

**订单、库存与结算核对**

商品资料、订单、库存、系统回执、结算与客服工单的可追踪业务工作流。支持CLI与JSON API，可供Python脚本、RPA流程和业务系统适配层调用。

## 业务能力

- **商品资料**：供应商中英文表头映射、字段校验、币种与毛利率计算、商品目录及Shopify草稿CSV准备、异常清单和批次去重。
- **订单与库存**：多店铺订单标识、重复输入与冲突识别、共享仓库库存预留、整单原子校验、缺货修复重跑、取消释放。
- **系统集成**：持久化履约任务、唯一请求标识、HTTP适配器、独立回执查询、进程中断恢复；区分明确拒绝与结果未知。
- **结算核对**：订单金额、币种、平台费用及实收对比，按结算参考号去重，输出差异。
- **AI工单辅助**：通过已有Codex登录调用指定模型，提取意图和订单号，生成待确认回复；结合对应店铺的订单状态建立处理工单。
- **运营交接**：CSV报表、异常清单、审计事件、处理记录与可复现验收。

## 快速运行

Python 3.11+，业务引擎和HTTP服务均使用标准库。AI功能另需已登录的Codex CLI及可用模型。

```shell
python -m unittest -v
python acceptance.py --output output/my-first-run
```

验收使用合成业务数据，并实际启动操作API与履约适配器两个HTTP服务。运行结束导出订单、库存、结算和异常报表，随后关闭服务。

[已验证运行结果](examples/validated_run.json)：501笔订单，其中500单由20个并发线程提交；覆盖库存约束、重复提交、对端已完成但回执暂未确认、重启查询和结算差异。数据规模及耗时仅代表该次验证运行。

## 处理自己的业务输入

```shell
python -m commerce_ops stock SKU-001 10 --snapshot-id supplier-stock-001
python -m commerce_ops import-orders examples/orders.json
python -m commerce_ops report
python -m commerce_ops queue <order-id>
python -m commerce_ops dispatch <task-id> --connector https://your-adapter.example
python -m commerce_ops reconcile <task-id> --connector https://your-adapter.example
python -m commerce_ops export output/business-report
```

默认数据库为`data/operations.sqlite`。不同批次通过稳定订单号与库存快照号去重，不能用新的请求号重放同一业务动作。金额使用十进制输入和整数最小货币单位存储。

原商品导入仍可独立使用：

```shell
python pipeline.py examples/supplier.csv
python -m commerce_ops import-catalog examples/supplier.csv --stock-snapshot-id approved-stock-001
```

库存同步由明确的物理库存快照触发。只改售价配置时不应创建新的库存快照号。

## AI客服工单

```shell
python -m commerce_ops.ai_assist examples/customer_ticket.txt --model YOUR_AVAILABLE_MODEL --output output/ticket.json --db data/operations.sqlite --store demo-store --case-id ticket-001
```

分类调用在临时工作目录中运行，关闭Shell、统一命令执行和继承的MCP服务；结果经JSON字段校验后写入工单。取消请求进入人工处理队列，订单状态由业务操作接口维护。

[实际分类结果](examples/ai_ticket_result.json)记录了模型输出及零工具调用。可以更换模型，不需要把业务流程绑定到某个模型。

## HTTP接入与运行维护

设置`COMMERCE_OPS_TOKEN`后启动：

```shell
python -m commerce_ops.api --port 8769
```

服务监听`127.0.0.1`，业务路由使用Bearer令牌。`COMMERCE_CONNECTOR_TOKEN`单独用于外部适配器。令牌通过环境变量传入，不进入示例文件。

完整路由、状态转换、故障恢复和适配器约定见[运行说明](IMPLEMENTATION.md)。HTTP适配层已通过实际回环服务验证；具体商家平台需实现其认证、字段映射与回执协议。现有实现不包含商业平台认证或支付执行。
