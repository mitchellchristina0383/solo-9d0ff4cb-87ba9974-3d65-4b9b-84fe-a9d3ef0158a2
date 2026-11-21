# PostgreSQL 复制槽清理演练 API

值班 DBA 的复制槽清理"沙盘"：录入当前 WAL 边界、复制槽与订阅确认位点，服务计算每个槽的
WAL 占用、最早保留点、预计可释放范围，并生成清理预览。清理只允许针对**明确停用
（disabled）且不再被在线/停顿订阅引用**的槽；确认时任何位点、槽状态或 WAL 边界变化都会
让整次请求被拒绝，数据库保持原样。执行失败或进程重启不会留下半完成的删除状态。

此外支持 **lost 逻辑槽的重建单**：为停顿订阅开单新建逻辑槽（以当前 WAL 边界为全量快照
位点），复制期间边界可继续前推但回退不得越过快照位点；提交回执且位点吻合时原子换绑、
按快照位点恢复在线并结单，原槽留在库存；位点不符可重报，中止则释放新槽。

- Swagger 操作入口：`http://localhost:8000/docs`
- 可视化演练控制台：`http://localhost:8000/demo`
- 原始库存 JSON：`GET /api/inventory`；事件流：`GET /api/state`

## 启动

```bash
docker compose up --build        # 等价于 docker compose up --build -d
# 打开 http://localhost:8000/docs
```

数据存放在命名卷 `drill-data`（`/data/drill.db`）。默认演示数据已就绪；
`PREVIEW_TTL_SECONDS`（默认 900）控制预览有效期，`ENABLE_DEMO=false` 可关闭注入接口，
`SEED_DEMO=false` 可关闭演示数据自动播种。

本地不使用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## /docs 演示路径

1. `GET /api/inventory`：查看 6 个演示槽、各自占用（`restart_lsn` 决定保留量）、
   订阅、`eligible` 与 `blocking_reasons`（含 `source` 字段指出占用/阻断来源）。
2. `POST /api/previews`，body `{"slot_names": ["slot_old_metrics"]}`：得到预览、
   `fingerprint`、`reclaimable_range`、`detach_subscriptions`。
3. **过期确认**：
   - 漂移：`PUT /api/cluster` 改 WAL 边界（或 `PATCH /api/slots/...`、注入订阅停顿），
     再 `POST /api/previews/{id}/confirm` 带上指纹 → `409 preview_stale`，
     `detail.changes` 列出逐字段 before/after，**一个槽都不删**；
   - 过期：`POST /api/demo/previews/{id}/expire` 后再确认 → `409 preview_expired`。
4. **失败保留**：`POST /api/demo/cleanup-failure?slot_names=slot_old_metrics`，
   新建预览并确认 → `200` 但 `ok=false, status=failed`；槽、订阅绑定、WAL 边界均不变，
   `GET /api/inventory` 中该槽 `pending_cleanup.status=failed`。清除注入
   （`DELETE /api/demo/cleanup-failure`）后重新预览即可成功。
5. **崩溃/重启**：`POST /api/demo/crash-before-commit` 后确认 →
   删除事务未提交、库存不变；`POST /api/demo/recover`（或重启容器）把
   `applying` 收敛为 `failed`，仍然没有任何槽被删除。
6. **订阅停顿/恢复**：`POST /api/demo/subscriptions/sub_audit/stall` 让订阅进入
   stalled（仍阻断清理）；`.../resume` 恢复为 online（**仍然阻断**）。只有显式
   `disabled` 且解绑的订阅不阻断。
7. **槽重建单**（演示数据 `slot_reports` 为 lost、`sub_reports` 停顿）：
   - `POST /api/rebuild-orders`，body
     `{"subscription_name": "sub_reports", "new_slot_name": "slot_reports_v2"}`：
     开单，记下当前 WAL 边界为快照位点，新槽从该位点起保留 WAL；
   - `PUT /api/cluster` 把边界前推到 `0/2800000`：新槽占用随之增长；
     回退到快照位点之前（如 `0/1F00000`）→ `409 boundary_below_snapshot`；
   - 在办期间 `PATCH`/`DELETE` 新槽、把新槽绑给别的订阅、改动原订阅 →
     `409 slot_reserved_by_rebuild` / `subscription_rebuild_in_progress`；
   - `POST /api/rebuild-orders/{id}/receipt` 带错误位点 → `409 snapshot_mismatch`，
     单仍在办可重报；带正确位点（开单响应里的 `snapshot_lsn`）→ 原子换绑：
     订阅恢复 online 并指向新槽，确认位点=快照位点，原槽留在库存；
   - 或 `POST /api/rebuild-orders/{id}/abort`：释放新槽，原订阅继续停顿；
     中止后再提交回执 → `409 rebuild_order_not_open`，库存不再改动。
8. `POST /api/demo/reset` 重置演示数据。

## 数据模型与规则

- `cluster`：单行，`wal_boundary_lsn` 为当前 WAL 边界。
- `slots`：`restart_lsn` 决定 WAL 保留起点（占用来源），`confirmed_flush_lsn`
  为订阅消费位点（滞后参考）；`active`、`disabled`、`wal_status` 参与安全判定。
- `subscriptions`：`status ∈ {online, stalled, disabled}`；`online` / `stalled` 都是
  活跃引用，阻断清理；`disabled` 必须不绑定槽（`slot_name` 为空），清理它引用的槽时
  会在同一事务里解绑。
- `rebuild_orders`：重建单。`snapshot_lsn` 为开单时的 WAL 边界（全量快照位点），
  `status ∈ {open, completed, aborted}`；部分唯一索引保证同一订阅最多一单在办。
- 可清理判定（任一不满足即阻断）：`active=false`、`disabled=true`、
  不被非 disabled 订阅引用、位点不超过 WAL 边界（`invalid_position`）、
  不是在办重建单的新槽（`reserved_by_rebuild_order`）。
- 占用与释放：每槽 `boundary - restart_lsn`；最早保留点取所有槽 `restart_lsn` 最小值；
  清理后最早保留点取剩余槽最小值，差额即预计可释放区间（按 LSN 估算，不含 16MiB
  WAL 段对齐误差，响应中有说明）。

### 重建单规则

- 开单条件：订阅 `stalled` 且其逻辑槽 `wal_status='lost'`；新槽名未被占用。
  开单在同一事务里创建新逻辑槽（`restart_lsn = confirmed_flush_lsn = 快照位点`，
  `active=false, disabled=false, wal_status='reserved'`）并落单。
- 在办期间（`open`）：
  - WAL 边界可继续前推，新槽占用计入库存；边界回退不得越过任一在办单的快照位点
    （`409 boundary_below_snapshot`）；
  - 新槽禁止改写/删除/绑定给其他订阅（`409 slot_reserved_by_rebuild`），
    清理预览与直接删除同样被阻断；
  - 原订阅维持停顿、保留原绑定与确认位点，禁止通过订阅接口或停顿/恢复注入改动
    （`409 subscription_rebuild_in_progress`）。
- 回执：上报位点与快照位点吻合时，单事务内原子换绑（订阅指向新槽、恢复 `online`、
  确认位点=快照位点，新槽转 `active`）并结单，原槽留在库存；位点不符返回
  `409 snapshot_mismatch` 并记 `mismatch_count`，单仍在办可重报。
- 中止：释放新槽（同事务删除），原订阅继续停顿；重复中止幂等。
- 结单后（`completed`/`aborted`）的回执不再改动库存：`completed` 幂等回放结果，
  `aborted` 返回 `409 rebuild_order_not_open`。
- 重启后在办单可查询（`GET /api/rebuild-orders`）并续办（回执/中止照常）。

## 一致性 / 崩溃安全设计

- 预览保存**安全性关键字段**的 SHA-256 指纹（位点、槽类型、active/disabled、
  wal_status、订阅绑定与状态、WAL 边界）。备注、时间戳、revision 不参与指纹。
- 确认采用两段写入：
  1. 校验过期/回传指纹/现网指纹/可清理性，并提交 `preview → applying` 声明；
  2. 单事务内重新校验指纹、解绑 disabled 订阅、删除槽、置 `completed`、递增 revision。
- 第 2 段是原子的：注入失败会整体回滚并把预览标为 `failed`（需要重新生成预览）；
  进程在提交前死亡只会留下 `applying`，启动时 `recover_interrupted` 将其收敛为
  `failed`，**不会留下删除了一半的库存**。删除语句本身还带
  `AND active=0 AND disabled=1` 前置条件，并有 `ON DELETE RESTRICT` 外键兜底。
- 预览一次性使用：`completed` 重复确认幂等返回 `already_applied=true`；
  `failed/rejected/expired/cancelled/applying` 不能再次确认。
- 重建单的换绑/释放都是单事务：回执换绑（订阅、新槽、单状态、revision、事件一次
  提交）不存在半完成状态；中止释放新槽同理。开单/回执/中止都会使库存 revision
  递增，从而让未确认的清理预览按既有指纹机制整次拒绝。
- 所有写操作在单进程内串行（`RLock`）+ `BEGIN IMMEDIATE`，单容器部署下无并发写竞态。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

覆盖：LSN 解析、占用/最早保留点/可释放区间、阻断规则、指纹稳定性；API 的录入与校验、
预览确认成功、指纹错误、WAL 边界/槽/订阅漂移整批拒绝、预览过期、混合批次含阻断槽整批拒绝、
订阅停顿阻断与恢复、注入清理失败回滚、提交前崩溃与恢复、演示接口开关、SQLite 层
原子性与外键约束；重建单的开单校验、边界前推/回退守卫、在办期间新槽与原订阅守卫、
回执不符重报、原子换绑、中止释放、结单后回执不动库存、重启后续办；
以及按文档走一遍的 storyboard 场景。

## 已完成 / 已验证 / 未完成

已完成：上述 API、计算、两段式事务确认、恢复扫描、故障注入、重建单生命周期
（开单/守卫/回执换绑/中止/重启续办）、`/docs` 与 `/demo` 页面、
Dockerfile 与 Compose、测试套件。

已验证：`python -m pytest tests -q` 全部通过（66 项）；在本地以真实 uvicorn 进程启动后，
通过 HTTP 走通库存 → 预览 → 漂移拒绝 → 注入失败保留 → 清理成功 → 重启后历史保留；
以及重建单全链路：开单（快照位点=当前边界）→ 边界前推（新槽占用增长）→
回退越过快照位点被 409 拒绝 → 在办期间改写/删除/绑定新槽与改动原订阅均被 409 拒绝 →
回执位点不符 409 可重报 → 位点吻合原子换绑、订阅按快照位点恢复在线、原槽留库存 →
结单后回执幂等不动库存；另走通中止释放新槽 → 中止后收到回执 409 且库存不变 →
杀掉进程重启后在办单可查询、可续办。

未完成 / 已知限制：

- 容器镜像**未在本环境实际构建**（沙箱内没有 docker CLI），`Dockerfile`/Compose
  仅经静态检查；首次 `docker compose up --build` 需联网安装依赖。
- 数据是演练/模拟库存，不会连接真实 PostgreSQL 执行 `pg_drop_replication_slot` /
  `pg_create_logical_replication_slot`；回执位点由操作者自报，服务只做吻合性校验，
  不验证真实复制进度。接入真实库时，需要把建新槽、换绑与清理映射到真实事务
  （PG 的 DDL 在事务内可回滚）。
- 单实例假设（进程内 RLock）；多副本部署需要改为数据库行锁/租约。
- 无鉴权与审计账号体系（演练环境），生产接入需前置 SSO/网关与操作人记录。
- 预估可释放量为 LSN 区间估算，未模拟检查点、WAL 段回收与 `max_wal_size` 行为。
