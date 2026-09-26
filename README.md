# 束线真空阀组切换服务 (Vacuum Valve Bank Switch)

一次提交多阀（2–8 台）整数开度切换的 Saga 服务，保证：

- **意图与逐阀动作持久化**：切换意图（含完整载荷）和每台阀的前向/补偿动作都落 SQLite。
- **稳定操作标识幂等**：`operation_id` 重复提交返回同一结果（首次 `201`，重放 `200`）；同一标识换载荷返回 `409` 且**不触碰任何设备**。
- **修订号条件变更（乐观并发）**：服务端为每台阀门确认一个单调递增的**开度修订号**；每次前向、补偿或重启补记动作都作为“期望修订号 → 下一修订号”的条件变更持久化，动作回执与切换记录同时保存期望及实际修订。只有对应修订仍一致才改变开度。
- **修订栅栏冲突**：两名工程师在不同控制台查看同一阀组后各自提交时，若前向或逆序补偿发现修订号已被其他已确认切换推进，服务**绝不覆盖现场值**（较早的补偿不会把另一份已确认的开度悄然改回），而是把该切换稳定收敛为可查询的 `REVISION_CONFLICT` 结论，列出阀门、期望修订、实际修订及未执行动作；同标识重传只返回原结论。
- **旧页面兼容**：未携带修订号的请求按**受理瞬间快照**执行，原有接口与重放语义保持可用。
- **设备侧“操作标识 + 阶段”去重**：模拟设备对 `(operation_id, phase)` 去重，开度变更、修订号推进与去重记录在同一事务提交；已执行动作（含修订区间）可经 `/api/devices/executed-actions` 查询。
- **崩溃回执对账**：进程恰在“设备已变更、应用回执未落库”时中断（`os._exit(77)`），重启后凭设备日志中的操作标识与修订区间辨认已发生的条件变更，**不会重复改变开度、不会再次推进修订号**。
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

Compose 验收实际覆盖：陈旧快照拒绝、补偿遇修订栅栏、崩溃补记后的网页与 HTTP 结果
（`tests/` 由 verify 服务运行，`scripts/smoke.py` 对运行中的 web 服务做 HTTP 与页面检查）。

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
| POST | `/api/switches` | 提交多阀切换（2–8 阀，开度 0–100 整数，可逐阀携带 `expected_revision` 快照） |
| GET | `/api/switches/{operation_id}` | 查询服务端确认的阶段、每阀最终开度与修订栅栏冲突结论 |
| POST | `/api/switches/{operation_id}/resume` | 继续未完成的切换/补偿 |
| GET | `/api/devices/executed-actions?operation_id=` | 查询设备已执行动作（去重日志，含期望/实际修订区间） |
| GET | `/api/devices/valves` | 模拟设备当前开度与服务端确认修订号 |
| GET | `/health` | 健康检查 |

`POST /api/test/reset` 与 `POST /api/test/failures` 为测试钩子，仅在 `VALVE_ALLOW_RESET=1` 时可用，可注入某阀的 FORWARD 拒绝或 COMPENSATE 网络失败。

### 修订号快照

- 控制台页面先通过 `GET /api/devices/valves` 显示服务端为每台阀门确认的开度与修订号，提交切换时逐阀携带 `expected_revision`。
- 未携带 `expected_revision` 的旧请求：服务端在**受理瞬间**为每台阀快照当前修订号并按其执行。
- 设备只在“存储修订号 == 期望修订号”时改变开度并推进到下一修订号；否则拒绝且现场值不变。
- 补偿动作以**本切换自己前向回执记录的修订号**为期望，因此永远不会回退别人后来确认的开度。

## 阶段语义

```
PENDING ──> EXECUTING ──> COMPLETED
                │  (任一前向动作失败)
                v
           COMPENSATING ──> COMPENSATED            (逆序全部恢复成功)
                │
                v
        COMPENSATION_FAILED ──resume──> COMPENSATED (补偿失败，可继续)

EXECUTING / COMPENSATING ──> REVISION_CONFLICT     (修订号已被其他已确认
                             切换推进：稳定收敛，可查询阀门/期望/实际修订
                             与未执行动作；同标识重传只返回该结论)
```

## 测试

- `tests/test_saga.py`：逆序补偿顺序、幂等重放、载荷冲突、补偿失败续恢复、设备先提交/回执后丢失的对账（含修订区间核对）。
- `tests/test_revisions.py`：陈旧快照前向遇栅栏且不改写现场值、补偿遇栅栏绝不回退他人已确认开度、旧页面受理瞬间快照、崩溃后按操作标识与修订区间对账。
- `tests/test_http.py`：HTTP 生命周期、409 不触设备、校验（422）、陈旧快照拒绝与补偿栅栏的 HTTP 结果。
- `tests/test_restart_process.py`：**真实 uvicorn 子进程**在设备提交后、回执前硬退出，新进程重启后辨认动作且不重复改变开度/推进修订；崩溃后另一切换推进修订时补偿稳定收敛为修订栅栏冲突。
