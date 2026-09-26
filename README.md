# 束线真空阀组切换服务 (Vacuum Valve Bank Switch)

一次提交多阀（2–8 台）整数开度切换的 Saga 服务，保证：

- **意图与逐阀动作持久化**：切换意图（含完整载荷）和每台阀的前向/补偿动作都落 SQLite。
- **稳定操作标识幂等**：`operation_id` 重复提交返回同一结果（首次 `201`，重放 `200`）；同一标识换载荷返回 `409` 且**不触碰任何设备**。
- **设备侧“操作标识 + 阶段”去重**：模拟设备对 `(operation_id, phase)` 去重，开度变更与去重记录在设备库同一事务提交；已执行动作可经 `/api/devices/executed-actions` 查询。
- **崩溃回执对账**：进程恰在“设备已变更、应用回执未落库”时中断（`os._exit(77)`），重启后凭设备去重记录辨认该动作，**不会重复改变开度**。
- **逆序补偿**：任一台阀前向失败（拒绝/网络失败）后，已成功变更的阀门按成功顺序的**相反顺序**恢复原开度；全部恢复成功才报告 `COMPENSATED`。
- **补偿失败可续**：补偿也失败时停留在 `COMPENSATION_FAILED`，逐阀状态明确可继续恢复（`resume` 或同标识重提），已恢复的阀不会再次动作。
- **阶段明确**：`PENDING / EXECUTING / COMPLETED / COMPENSATING / COMPENSATED / COMPENSATION_FAILED`，任何中断点都不会留下“未说明的半切换状态”。

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
| POST | `/api/switches` | 提交多阀切换（2–8 阀，开度 0–100 整数） |
| GET | `/api/switches/{operation_id}` | 查询服务端确认的阶段与每阀最终开度 |
| POST | `/api/switches/{operation_id}/resume` | 继续未完成的切换/补偿 |
| GET | `/api/devices/executed-actions?operation_id=` | 查询设备已执行动作（去重日志） |
| GET | `/api/devices/valves` | 模拟设备当前开度 |
| GET | `/health` | 健康检查 |

`POST /api/test/reset` 与 `POST /api/test/failures` 为测试钩子，仅在 `VALVE_ALLOW_RESET=1` 时可用，可注入某阀的 FORWARD 拒绝或 COMPENSATE 网络失败。

## 阶段语义

```
PENDING ──> EXECUTING ──> COMPLETED
                │  (任一前向动作失败)
                v
           COMPENSATING ──> COMPENSATED            (逆序全部恢复成功)
                │
                v
        COMPENSATION_FAILED ──resume──> COMPENSATED (补偿失败，可继续)
```

## 测试

- `tests/test_saga.py`：逆序补偿顺序、幂等重放、载荷冲突、补偿失败续恢复、设备先提交/回执后丢失的对账。
- `tests/test_http.py`：HTTP 生命周期、409 不触设备、校验（422）。
- `tests/test_restart_process.py`：**真实 uvicorn 子进程**在设备提交后、回执前硬退出，新进程重启后辨认动作且不重复改变开度；补偿中断后续补仍为逆序。
