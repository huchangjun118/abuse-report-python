# 举报受理与账号处置审核工作流

一套面向并发、重试与崩溃恢复设计的举报审核系统。举报经多入口进入后会被
**归并去重**、按被举报对象**聚合成案件**、由人工或阈值规则作出**账号处置**、
允许被处置方**申诉**，全过程**鉴权并留痕**，且可在进程重启后**恢复对齐**。

## 1. 模块结构

```
abuse/
  enums.py        状态、处置、申诉、角色等枚举
  models.py       领域数据模型（Report / Case / Action / Appeal / AuditEntry ...）
  errors.py       领域错误（越权、非法状态、限速、冲突重试等）
  config.py       阈值与处置时长等可配置项（Config）
  clock.py       可注入时钟，测试可手动推进时间
  idempotency.py  举报去重键：sha256(source|reporter|event)
  repository.py   SQLite 持久化：schema、CAS 更新、唯一索引、事务
  permissions.py  基于角色的权限矩阵
  audit.py        审计：每个决定与每次被拒尝试都写不可变日志
  cases.py        举报受理、案件聚合、证据累计、案件状态机
  actions.py      处置决定、并发收敛、到期自动解除、可解释
  appeals.py      仅本人可发起的申诉与确定性裁决
  threshold.py    举报人限速 + 不同举报人数量阈值的自动升级
  recovery.py     启动恢复：补激活、放到期、收敛、对齐案件
  workflow.py     门面：装配服务、启动恢复、后台清扫线程
  api.py          FastAPI HTTP 接口
tests/            55 个自动化测试
main.py           uvicorn 入口
```

## 2. 举报归并（要求 1）

举报身份键为 `source | reporter_id | event_key` 的哈希：

```
dedup_key = sha256("web|u123|evt-9")
```

- 同一来源、同一举报人、同一事件的重复提交/网络重试只会更新
  `dedup_counters.count`，**不会新增 report 行、不会重复累计证据、
  不会再次触发自动升级**。
- 重复重试本身不消耗限速配额（它是幂等重放）；仅"新事件"消耗配额。
- 不同举报人、不同入口、不同事件会生成独立举报。

## 3. 案件聚合与状态机（要求 2）

指向同一 `target_account_id` 的所有举报聚合为唯一案件（数据库
`UNIQUE(target_account_id)` 兜底），每条新举报使 `evidence_count + 1`。

允许的状态转换：

```
            ┌──────────────────────────────────────────┐
            ▼                                            │
        ┌────────┐  review   ┌────────────┐  decide ┌───────────┐
   ───▶ │  open  │ ────────▶ │ in_review  │ ──────▶ │ actioned  │
        └────────┘           └────────────┘         └───────────┘
            ▲                    │  │       ▲   │       │  │
            │                    │  │       │   │ appeal│  │ expire/revoke
            │             reopen │  └───────┘   │       ▼  ▼
            │                    │            ┌───────────┐
            │                    └──────────▶ │ appealed  │
            │                                 └───────────┘
            └────────────── reopen / grant / expire ───────┘
                              任一非终态均可 ──▶ closed
                       closed 可重开 ──▶ open
```

具体邻接表（`cases.ALLOWED_TRANSITIONS`）：

| from        | allowed to                                      |
|-------------|-------------------------------------------------|
| open        | in_review, actioned, closed                     |
| in_review   | open, actioned, appealed, closed                |
| actioned    | open（封禁到期/撤销）, appealed, closed          |
| appealed    | open（申诉成立）, actioned（驳回/维持）, closed   |
| closed      | open                                            |

并发保护：

- 每次更新都是 **compare-and-set**（`WHERE id=? AND version=?`），版本不匹配
  返回冲突而不是覆盖，杜绝状态回退与字段级混合；
- 进程内用可重入锁串行化，跨进程由 SQLite `BEGIN IMMEDIATE` 串行化；
- 调用方可传 `expected_version` 基于快照提交，过期快照立即收到
  `ConflictRetryError`。

## 4. 账号处置（要求 3）

四种处置，严重度从低到高：

| 处置       | 严重度 | 默认时长      | 到期处理      |
|------------|--------|---------------|---------------|
| warn       | 1      | 无期限（记录） | 不自动解除     |
| mute       | 2      | 3600 秒       | 自动解除       |
| ban_timed  | 3      | 86400 秒      | 自动解除       |
| ban_perm   | 4      | 永久          | 不自动解除     |

每个处置都带 `effective_at`、`expires_at`、`decided_by`、`reason`、`basis`。

### 并发收敛规则（确定性）

同一账号**至多一个 ACTIVE 处置**（部分唯一索引
`ux_active_action ON actions(account_id) WHERE status='active'` 兜底）。
多个处置按以下排序，元组最大者生效：

```
( 0 if 自动 else 1,   # 1) 人工决定永远压过自动决定
  severity,           # 2) warn < mute < ban_timed < ban_perm
  created_at_us,      # 3) 同级时后作出的决定生效
  id )                # 4) 时间戳相同的确定性兜底
```

落败处置置为 `superseded` 并写审计；收敛 `converge()` 幂等，重复执行结果一致。

### 生效、到期与恢复

- `effective_at` 在未来的处置先以 `pending` 落库，由后台 sweeper（或启动
  恢复）在到期时激活——崩溃不会导致处置丢失，也不会重复执行"生效"动作
  （CAS：仅 `pending -> active` 可成功一次）。
- 限期处置到达 `expires_at` 后 `active -> expired`，记录 `released_at`，
  案件对齐为 `open`；重复调用不会二次解除。
- `GET /accounts/{id}/enforcement` 返回当前生效处置与完整历史，并给出
  "谁、基于什么、何时自动解除"的自然语言解释。

## 5. 申诉（要求 4）

- **仅被处置账号本人**（`actor.id == case.target_account_id` 且角色为
  `user`）可发起；他人或审核角色冒充都被拒绝并留痕。
- 每个案件每个账号仅允许一条申诉（`UNIQUE(case_id, account_id)`）；
  案件进入 `appealed`。
- 申诉裁决为 CAS：`WHERE status='pending'`，与新举报、第二个审核员并发时
  **恰好一个裁决成功**，失败者收到 `ConflictRetryError`。
- 成立（grant）：当前有效处置置 `revoked`、案件回到 `open`；
- 驳回（deny）：处置维持、案件回到 `actioned`。

## 6. 权限与审计（要求 5）

| 操作                | user | reviewer | admin |
|---------------------|:----:|:--------:|:-----:|
| 提交举报 submit_report     | ✅ | ✅ | ✅ |
| 本人申诉 file_appeal       | ✅ | ✅ | ✅ |
| 复核案件 review_case       | ❌ | ✅ | ✅ |
| 作出处置 decide_action     | ❌ | ✅ | ✅ |
| 变更处置 change_action     | ❌ | ✅ | ✅ |
| 裁决申诉 decide_appeal     | ❌ | ✅ | ✅ |

`audit_log` 记录：时间戳、操作人、动作、实体类型/ID、是否允许、原因明细。
**越权、限速、非法状态转换等被拒操作同样写审计**（`allowed=0`），因此
"谁在何时基于什么证据作出了什么决定/试图做什么"可完整还原：

```
GET /audit?entity_type=case&entity_id=case_xxx
```

## 7. 限速与阈值自动升级（要求 6）

两层相互独立的保护：

1. **单举报人滑动窗口限速**：窗口内由同一 reporter 创建的"新举报"超过
   `rate_limit_max_per_reporter` 即返回 429 并留痕。限速发生在证据聚合
   **之前**，刷量不会抬高证据数，也不可能把目标刷过封禁阈值。
2. **按不同举报人数量升级**：窗口内对目标的 `DISTINCT reporter_id`
   - 达到 `auto_escalate_distinct_reporters`（默认 3）：
     案件自动 `open -> in_review`，优先级 `auto`；
   - 达到 `auto_ban_distinct_reporters`（默认 10）：
     系统自动作出**限期封禁**（`auto=True`，审计操作人为 `system:*`）。

人工复核优先级：一旦人工接管（`in_review/actioned/...` 且 `priority=manual`
或已有人工生效处置），系统**不会**再自动升级或覆盖；任何自动处置在收敛
规则中都输给人工处置（即使自动决定是永久封禁、人工只是警告）。

## 8. 重启与失败恢复（要求 7）

所有状态变化均先事务落库，再考虑副作用。启动时 `RecoveryService.recover()`
执行且**幂等**：

1. 激活所有已到 `effective_at` 的 pending 处置；
2. 解除所有已过 `expires_at` 的 active 限期处置；
3. 对所有曾有处置的账号重新 `converge()` 到唯一胜出处置；
4. 依据持久化的处置与申诉对齐案件状态（pending 申诉 -> appealed；
   有生效处置 -> actioned；申诉成立或处置已解除 -> open）。

CAS 与唯一索引保证：恢复重跑、进程重启都不会重复生效或重复解除处置；
文件型 SQLite（WAL + synchronous=FULL）保证在途案件不丢失。

## 9. 配置项（`abuse/config.py`）

| 配置项                                | 默认值 | 含义                                   |
|---------------------------------------|--------|----------------------------------------|
| `action_durations.warn`               | None   | 警告无到期                              |
| `action_durations.mute`               | 3600   | 禁言默认秒数                            |
| `action_durations.ban_timed`          | 86400  | 限期封禁默认秒数                        |
| `action_durations.ban_perm`           | None   | 永久封禁                                |
| `rate_limit_window_seconds`           | 60     | 单举报人限速窗口                        |
| `rate_limit_max_per_reporter`         | 5      | 窗口内允许的新举报数                    |
| `auto_escalate_window_seconds`        | 3600   | 不同举报人统计窗口                      |
| `auto_escalate_distinct_reporters`    | 3      | 进入人工/自动复核队列的不同举报人数     |
| `auto_ban_distinct_reporters`         | 10     | 触发自动限期封禁的不同举报人数          |
| `auto_action_enabled`                 | True   | 是否允许自动升级（测试可关）            |
| `sweep_interval_seconds`              | 1.0    | 后台到期清扫间隔                        |

## 10. HTTP 接口与鉴权

通过请求头传入操作主体（无状态演示用）：

- `X-Actor-Id: <id>`
- `X-Actor-Role: user | reviewer | admin`（默认 user）

| 方法   | 路径                                  | 说明                     |
|--------|---------------------------------------|--------------------------|
| POST   | `/reports`                            | 提交/重试举报（幂等归并） |
| GET    | `/cases`                              | 案件列表                  |
| GET    | `/cases/{id}`                         | 案件详情                  |
| POST   | `/cases/{id}/review`                  | 人工接管（manual 优先）   |
| POST   | `/cases/{id}/transition`              | 合法状态推进              |
| POST   | `/cases/{id}/actions`                 | 作出处置                  |
| POST   | `/actions/{id}/revoke`                | 变更/撤销处置             |
| GET    | `/accounts/{id}/enforcement`          | 当前处置与可解释历史      |
| POST   | `/cases/{id}/appeals`                 | 本人申诉                  |
| POST   | `/appeals/{id}/decision`              | 申诉成立/驳回             |
| GET    | `/audit`                              | 审计查询（可按实体过滤）   |
| POST   | `/internal/sweep`                     | 手动触发一次清扫          |

错误码：401 未鉴权 / 403 越权 / 404 不存在 / 409 状态冲突或重复 /
422 校验失败 / 429 限速。

## 11. 运行与测试

```bash
# 启动 HTTP 服务（文件库持久化、后台自动解除到期处置）
uvicorn main:app --reload

# 运行全部自动化测试（55 个）
pytest -q
```

测试覆盖：重复举报归并、案件状态机、并发处置收敛、限期到期自动解除、
申诉并发裁决与越权申诉、越权留痕、刷量限速、刷量误封防护、自动升级与
人工优先级，以及文件库下的多场景重启恢复。
