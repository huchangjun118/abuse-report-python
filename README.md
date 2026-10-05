# 举报受理与账号处置审核工作流

覆盖举报提交、案件聚合、账号处置、申诉复核、权限审计、阈值自动升级与
重启恢复的完整链路。所有写操作以 SQLite 单事务提交，进程中断不会产生
中间态；重启后自动对齐在途案件并解除已到期的限期封禁。

## 运行

```bash
uv sync
uv run pytest            # 全部自动化测试
uv run python main.py    # 启动 HTTP 服务（127.0.0.1:8000）
```

HTTP 接口通过请求头标识操作者：`X-Actor`（账号）、`X-Role`
（`reporter` / `reviewer` / `admin`）。数据库路径由环境变量
`ABUSE_DB_PATH` 指定，默认 `abuse.db`。

## 核心一致性设计

1. **举报幂等**：幂等键默认为 `{reporter_id}:{event_id}`，也可显式传入。
   相同来源对同一事件的重复提交与网络重试归并为同一条举报，返回原记录，
   不重复计数、不重复触发聚合与审计。幂等重试不受限速影响。
2. **案件聚合**：同一被举报对象的所有举报聚合到唯一案件
   （`cases.target_account` 唯一约束）。案件更新采用乐观锁整行替换，
   并发下不会出现状态回退或字段级混合。
3. **处置收敛**：同一账号同时生效的多个处置按规则收敛为唯一结果——
   严重级别高者生效（warn < mute < temp_ban < perm_ban），同级取到期
   最晚者；败者以 `superseded_by:<id>` 解除并留痕。
4. **限期封禁自动解除**：到期封禁在读取路径与启动恢复时被惰性解除，
   审计记录 `auto_lift`（含生效/到期时间），可解释、幂等、只执行一次。
5. **申诉确定性**：仅被处置方本人可发起申诉；申诉待决期间禁止新的
   处置决定，申诉与处置并发时由写锁串行化，结果确定。
6. **防误封**：单个举报人对单案件的计数贡献有上限
   （`per_reporter_case_cap`），刷量会被限速且无法把目标顶到自动升级
   阈值。
7. **恢复**：每个公开操作是一个事务，中断只留完整前/后态；`recover()`
   在服务构造时执行，解除到期封禁（幂等）。

## 案件状态机

```
OPEN ──submit/阈值──▶ TRIAGED ──decide──▶ ACTION_TAKEN ──file_appeal──▶ APPEALED
  │                    │                      │   ▲                       │
  │                    │                      │   └──appeal_upheld────────┤
  ├──reject────────────┴──▶ REJECTED          └──resolve──▶ RESOLVED ◀────┘
  (RESOLVED / REJECTED 为终态)                    (appeal_overturned)
```

允许的转换（`abuse/models.py: CASE_TRANSITIONS`）：

| 当前状态 | 允许转换到 | 触发 |
|---|---|---|
| OPEN | TRIAGED | 人工复核 / 达到自动升级阈值 |
| OPEN, TRIAGED | REJECTED | 复核不成立 |
| TRIAGED | ACTION_TAKEN | 处置决定 |
| ACTION_TAKEN | APPEALED | 本人申诉 |
| ACTION_TAKEN | RESOLVED | 人工结案 |
| APPEALED | ACTION_TAKEN | 申诉被维持 |
| APPEALED | RESOLVED | 申诉成立（同时解除活跃处置） |

自动升级只把 OPEN 推进到 TRIAGED，绝不覆盖人工已推进的状态；
人工复核优先于自动升级。

## 角色权限

| 操作 | reporter | reviewer | admin |
|---|---|---|---|
| 提交举报 / 本人申诉 | ✓ | ✓ | ✓ |
| 复核（triage/reject/resolve） | ✗ | ✓ | ✓ |
| 处置 warn/mute/temp_ban | ✗ | ✓ | ✓ |
| 处置 perm_ban | ✗ | ✗ | ✓ |
| 复核申诉 | ✗ | ✓ | ✓ |

越权操作被拒绝并写入审计（如 `appeal_denied`、`rate_limited`）。
每个决定都记录操作人、角色、原因与证据快照（`audit_log` 表，
`GET /audit?target=` 查询）。

## 配置项（`abuse/config.py: WorkflowConfig`）

| 配置 | 默认值 | 说明 |
|---|---|---|
| `escalate_report_threshold` | 5 | 案件累计举报数达到该值自动升级 TRIAGED |
| `escalate_reporter_threshold` | 3 | 独立举报人数达到该值自动升级 TRIAGED |
| `reporter_rate_limit` | 10 | 单举报人窗口内最大提交数，超出拒绝并留痕 |
| `reporter_rate_window` | 60s | 限速窗口长度（秒） |
| `per_reporter_case_cap` | 3 | 单举报人对单案件的最大计数贡献（防刷量误封） |
| `temp_ban_duration` | 7 天 | 限期封禁默认时长（秒），可在 decide 时覆盖 |
| `db_path` | `:memory:` | SQLite 路径，`:memory:` 用于测试 |

## HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/reports` | 提交举报（幂等） |
| POST | `/cases/{id}/triage` `/reject` `/resolve` | 复核流转 |
| POST | `/cases/{id}/decide` | 处置决定（warn/mute/temp_ban/perm_ban） |
| POST | `/cases/{id}/appeals` | 本人申诉 |
| POST | `/appeals/{id}/decide` | 申诉复核（uphold / overturn） |
| GET | `/cases/{id}` | 案件详情 |
| GET | `/accounts/{acc}/sanction` | 账号当前生效处置 |
| GET | `/audit?target=` | 审计链查询 |

## 测试覆盖（`tests/`）

- 重复举报与网络重试归并、不重复计数
- 多举报人并发提交：计数一致、状态不回退
- 并发处置收敛为唯一结果、限期封禁到期自动解除
- 申诉与处置并发的确定性、仅本人可申诉
- 越权复核/处置/申诉被拒且留痕
- 单举报人刷量限速且不会顶爆阈值造成误封
- 进程重启后在途案件恢复、到期解除不重复执行、幂等键跨重启有效
