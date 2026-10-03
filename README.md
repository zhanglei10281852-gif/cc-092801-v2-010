# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。暴雨等突发情况下的仪式订单批量改期接口使用 `/api/reschedule` 前缀。

## 批量改期（预演 → 双人确认 → 原子切换）

针对暴雨预警等需要把同一天多场婚礼、追思仪式整体改期的场景，服务提供先预演后确认的改期流程，预演不会修改任何生效数据：

1. **维护资源与预订**：通过 `POST /api/reschedule/resources` 登记人员、车辆、供应商资源（日容量、改期手续费），通过 `POST /api/reschedule/bookings` 把资源预订挂到仪式订单上（数量、供应商承诺、已生成费用）。
2. **创建预演**：`POST /api/reschedule/plans` 输入原日期、候选日期范围和改期原因（幂等键防重复提交）。服务计算受影响订单、逐候选日的资源冲突、可迁移资源与预计费用差异，生成带版本和摘要的报告并选定首个可行日期；报告有过期时间。
3. **复核与双人确认**：`GET /api/reschedule/plans/{id}` 查看预演进度与事件流，`GET .../report` 查看报告摘要（`?full=true` 查看明细）。两名分别持有 `reschedule.confirm.operations` 与 `reschedule.confirm.finance` 权限的人员通过 `POST .../confirm` 确认同一份报告版本；重复确认、同人复确认、版本不一致都会被拒绝并留存原因。
4. **原子切换**：双确认齐备后 `POST .../apply` 在单个事务中完成全部订单改期、资源预订迁移、供应商承诺重置与费用调整，并写入运营干预审计；`GET .../final` 查询最终版本。切换前会复核资源可用性与数据摘要：报告过期、部分资源不可用或预演后数据已变化都会安全失败，不改动任何生效数据并保留失败原因。
5. **重启安全**：服务启动时自动对账，崩溃遗留的 `rehearsing`/`applying` 计划一律标记为失败并记录原因，未完成的预演绝不会被当作已执行。

```bash
python -m app.cli reschedule-demo   # 端到端演示以上流程
```

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
python -m app.cli reschedule-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/reschedule/    批量改期：预演报告、双人确认、原子切换与重启对账
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
