# 飞控共享总线锁 · 优先级继承裁决服务

模拟飞控共享总线在**锁竞争**下的优先级继承（Priority Inheritance）裁决，供值班员复核
"有效优先级与锁移交是否始终一致"。Python 标准库实现，零第三方依赖。

## 裁决规则

- 数值**越小越紧急**；锁为**不可重入**互斥锁。
- 等待者经**任意嵌套等待链**（A 持锁、B 等 A 的锁、C 等 B 的锁……）把最高紧急度
  （最小有效优先级）传递给阻塞拥有者；每步快照给出完整传递链。
- **释放**或**取消**后，从当前等待图**重算**有效优先级，表现为继承解除后的回落。
- 释放时锁移交给**有效优先级最高（值最小）、并列时任务标识最小**的等待者。
- 以下事件非法，系统**定位该事件（errorIndex 从 1 起）并清除本次提交此前所有旧成功**
  （冻结裁决，无终态快照）：
  - 非拥有者释放；
  - 取消运行任务（含持锁者）；
  - 重复等待同一锁 / 已阻塞再等别的锁；
  - acquire 制造等待环；
  - 引用不存在的任务/锁、非法事件类型或参数。
- 同一稳定审计标识：**相同输入重传回放冻结结论**（`replayed: true`）；
  **内容不同则 409 冲突**，原裁决不受影响。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | ---
| `POST` | `/api/verdicts` | 提交：首次冻结 / 相同回放 / 不同 409 |
| `GET`  | `/api/verdicts` | 冻结裁决索引 |
| `GET`  | `/api/verdicts/<auditId>` | 按标识重新读取冻结裁决与每步快照 |
| `GET`  | `/api/verdicts/<auditId>/inheritance-windows?taskId=N` | 任务 N 有效优先级偏离基准的连续继承时段 |
| `GET`  | `/health` | 健康响应 |
| `GET`  | `/` | 裁决台网页（真实调用上述 API） |

继承时段（inheritance-windows）把某任务"有效优先级 ≠ 基准优先级"的连续步归并成段：
相邻快照即使继承来源改变也不拆段；每段给出起止事件、段内最紧急的继承优先级、
起点/终点可对照逐步快照复核的等待链证据，以及段内按首次出现顺序去重的来源任务。
任务不存在（404，附可选任务列表）、裁决因非法事件冻结无快照（409）、审计标识
不存在（404）、缺 `taskId`（400）均返回可操作的失败说明。

提交体：

```json
{
  "auditId": "FC-AUDIT-001",
  "tasks": [{ "id": 1, "priority": 8 }],
  "locks": [{ "id": 1, "priority": 9 }],
  "events": [
    { "type": "acquire", "taskId": 1, "lockId": 1 },
    { "type": "set-priority", "taskId": 1, "priority": 2 },
    { "type": "cancel", "taskId": 2 }
  ]
}
```

限制：任务 ≤ 16、锁 ≤ 32、事件 ≤ 128。每步快照含任务基准/有效优先级/继承来源、
运行·等待·持锁状态，以及锁持有者与等待队列。

## 本地运行（无需 Docker）

```bash
python3 app/server.py                 # 默认 0.0.0.0:8080
PORT=9090 DATA_FILE=/tmp/v.json python3 app/server.py
python3 -m unittest tests.test_rules  # 规则测试
WEB_BASE_URL=http://127.0.0.1:9090 python3 verify/run_verify.py
```

## Docker / Compose

宿主端口可配（默认 8080）：

```bash
HOST_PORT=9090 docker compose up -d --build web
curl http://127.0.0.1:9090/health
```

校验容器针对示例轨迹运行**规则测试 + 构建检查 + API/HTTP 冒烟**，结束即退出，
退出码即结论：

```bash
docker compose up --build verify        # 自动等待 web 健康后执行
docker compose inspect verify ...       # 或查看退出码：0 通过 / 1 失败
docker compose run --rm verify
docker compose logs verify
```

## 目录

```
app/engine.py          裁决状态机（继承图、移交、非法事件定位）
app/analysis.py        继承时段归并（偏离基准的连续区间、边界证据）
app/store.py           输入规范化、指纹、冻结/回放/冲突、持久化
app/server.py          HTTP API + 静态页
app/static/index.html  裁决台（编辑/预设/提交/回放/逐步快照/继承区间复核）
tests/test_rules.py    规则单测（28 项）
verify/run_verify.py   verify 容器入口（测试+构建+冒烟，退出码报告）
Dockerfile, docker-compose.yml
```
