# 束线真空阀组切换服务 (Vacuum Valve Bank Switch)

一次提交多阀（2–8 台）整数开度切换的 Saga 服务，保证：

- **意图与逐阀动作持久化**：切换意图（含完整载荷）和每台阀的前向/补偿动作都落 SQLite。
- **稳定操作标识幂等**：`operation_id` 重复提交返回同一结果（首次 `201`，重放 `200`）；同一标识换载荷（含换修订号快照）返回 `409` 且**不触碰任何设备**。
- **修订号条件变更（修订栅栏）**：每台阀门持有单调递增的 `revision`。页面先显示服务端为每台阀门确认的开度修订号，提交切换时携带该快照；设备模拟器把每次前向、补偿或重启补记动作都作为“期望修订号 → 下一修订号”的条件变更持久化——只有对应修订仍一致才改变开度，动作回执与切换记录都保存期望及实际修订。
- **陈旧快照绝不覆盖现场值**：两名工程师在不同控制台查看同一阀组后各自提交切换时，若前向或逆序补偿发现修订已被另一份已确认切换推进，服务**不覆盖现场值**，而是把该切换稳定收敛为可查询的 `REVISION_CONFLICT`（修订栅栏冲突），列出阀门、期望修订、实际修订及未执行动作；同标识重传只返回原结论。较早的补偿绝不会把另一份已经确认的开度悄然改回。
- **旧客户端兼容**：未携带修订号的请求按受理瞬间快照执行，原有接口与重放语义保持可用。
- **设备侧“操作标识 + 阶段”去重**：模拟设备对 `(operation_id, phase)` 去重，开度变更、修订推进与去重记录在同一事务提交；已执行动作（含修订区间）可经 `/api/devices/executed-actions` 查询。
- **崩溃回执对账**：进程恰在“设备已变更、应用回执未落库”时中断（`os._exit(77)`），重启后恢复流程按操作标识和修订区间核对设备日志，辨认已发生的条件变更，**不会重复改变开度、不会再次推进修订**。
- **逆序补偿**：任一台阀前向失败（拒绝/网络失败）后，已成功变更的阀门按成功顺序的**相反顺序**恢复原开度；全部恢复成功才报告 `COMPENSATED`。
- **补偿失败可续**：补偿也失败时停留在 `COMPENSATION_FAILED`，逐阀状态明确可继续恢复（`resume` 或同标识重提），已恢复的阀不会再次动作。
- **阶段明确**：`PENDING / EXECUTING / COMPLETED / COMPENSATING / COMPENSATED / COMPENSATION_FAILED / REVISION_CONFLICT`，任何中断点都不会留下“未说明的半切换状态”。

## 运行（Docker Compose）

```bash
docker compose up web --build        # http://localhost:8080
WEB_PORT=9090 PORT=80 docker compose up web --build   # 端口可配
```

健康检查：`GET /health`（Dockerfile HEALTHCHECK 与 Compose healthcheck 均已配置）。

一键验证（代码测试 + 构建检查 + HTTP 冒烟，完成后退出并报告退出码）：

```bash
docker compose build
docker compose run --rm verify       # 退出码 0 表示全部通过
```

`verify` 实际覆盖：陈旧快照拒绝、补偿遇栅栏、崩溃补记后的网页与 HTTP 结果（冒烟阶段直接打活 `web` 服务，pytest 阶段含真实 uvicorn 子进程崩溃/重启用例）。

## 本地（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r app/requirements.txt -r requirements-dev.txt
VALVE_DB_DIR=./data VALVE_ALLOW_RESET=1 \
  .venv/bin/uvicorn app.main:app --port 8080
WEB_URL=http://127.0.0.1:8080 .venv/bin/python scripts/verify.py
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/switches` | 提交多阀切换（2–8 阀，开度 0–100 整数）；可携带 `revisions` 修订号快照 |
| GET | `/api/switches/{operation_id}` | 查询服务端确认的阶段、每阀最终开度与修订、冲突及未执行动作 |
| POST | `/api/switches/{operation_id}/resume` | 继续未完成的切换/补偿 |
| GET | `/api/devices/executed-actions?operation_id=` | 查询设备已执行动作（去重日志，含期望/实际修订） |
| GET | `/api/devices/valves` | 模拟设备当前开度与**服务端确认修订号** |
| GET | `/health` | 健康检查 |

`POST /api/test/reset` 与 `POST /api/test/failures` 为测试钩子，仅在 `VALVE_ALLOW_RESET=1` 时可用，可注入某阀的 FORWARD 拒绝或 COMPENSATE 网络失败。

### 修订号快照

- 控制台页面先经 `GET /api/devices/valves` 显示每台阀门的确认修订号，提交时在 `revisions` 字段携带该快照；快照必须恰好覆盖本次请求的全部阀门。
- 未携带 `revisions` 的请求（旧页面/旧客户端）由服务端在受理瞬间读取当前修订作为快照，语义与旧版一致。
- 设备把每次动作持久化为条件变更：仅当阀门当前修订等于期望修订时才改变开度并推进修订（`N → N+1`）；否则拒绝并报告实际修订。
- 同一 `operation_id` 换快照视为换载荷：`409`，不触碰任何设备。

## 阶段语义

```
PENDING ──> EXECUTING ──> COMPLETED
                │  (任一前向动作失败/遇栅栏)
                v
           COMPENSATING ──> COMPENSATED            (逆序全部恢复成功)
                │    │
                │    └──> REVISION_CONFLICT        (前向/补偿遇修订栅栏：不覆盖现场值，
                │                                   稳定终态，可查询阀门/期望/实际/未执行动作)
                v
        COMPENSATION_FAILED ──resume──> COMPENSATED / REVISION_CONFLICT
```

`REVISION_CONFLICT` 是稳定终态：`terminal=true`、`resumable=false`，同标识重传只返回原结论；排除快照差异后请用新的 `operation_id` 基于最新修订号重新提交。

## 测试

- `tests/test_saga.py`：逆序补偿顺序、幂等重放、载荷/快照冲突、补偿失败续恢复、设备先提交/回执后丢失的对账、陈旧快照前向栅栏、补偿遇栅栏不回退他人开度、旧客户端受理瞬间快照。
- `tests/test_http.py`：HTTP 生命周期、409 不触设备、校验（422）、陈旧快照 `REVISION_CONFLICT`、补偿栅栏、同标识重传返回原结论。
- `tests/test_restart_process.py`：**真实 uvicorn 子进程**在设备提交后、回执前硬退出，新进程重启后按操作标识与修订区间辨认动作，不重复改变开度、不再次推进修订；补偿中断后续补仍为逆序。
