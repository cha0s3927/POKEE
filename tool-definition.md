# Scheduler API — Tool 定义 (共 8 个)

Dify Agent 通过导入 `http://localhost:8000/openapi.json` 获得以下工具。
LLM 根据 `operationId` 和 `summary` 的语义判断何时调用哪个接口。

---

## 1. create_reminder — 创建定时提醒

```
POST /reminders
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task | string | 是 | 提醒内容，如 "提醒用户开会" |
| run_at | string | 是 | 触发时间，ISO 8601 格式，如 "2026-07-30T14:30:00" |
| user_id | string | 否 | 用户标识，默认 "default" |

返回 `201`：`{ id, status, run_at }`

---

## 2. list_reminders — 查询提醒列表

```
GET /reminders
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 否 | 默认 "default" |
| status | string | 否 | 筛选：pending / sent / cancelled |

返回 `200`：提醒对象数组

---

## 3. get_reminder — 查看单个提醒

```
GET /reminders/{reminder_id}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| reminder_id | string | 是 | 提醒 ID，URL 路径参数 |

返回 `200`：单个提醒对象

---

## 4. cancel_reminder — 取消提醒

```
DELETE /reminders/{reminder_id}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| reminder_id | string | 是 | 提醒 ID，URL 路径参数 |

返回 `200`：`{ id, status: "cancelled" }`

---

## 5. get_notifications — 获取未读通知

```
GET /notifications
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 否 | 默认 "default" |

返回 `200`：状态为 sent 且 seen_at 为空的提醒数组
（Agent 每次回复前调用，检查是否有刚到时间的提醒）

---

## 6. ack_notifications — 标记通知已读

```
POST /notifications/ack
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 否 | 默认 "default" |

返回 `200`：`{ acked: N }`（已标记条数）

---

## 7. get_current_time — 获取当前时间

```
GET /now
```

无参数。返回 `200`：`{ current_time, timestamp, timezone }`
（LLM 计算 "2分钟后" 时调用，确保时间基准正确）

---

## 8. health_check — 健康检查

```
GET /health
```

无参数。返回 `200`：`{ status, scheduler_running }`

---

## 调用示例

用户说："2分钟后提醒我开会"

LLM 先调 `get_current_time` 获取当前时间，算出 2 分钟后的 ISO 8601 字符串，
然后调 `create_reminder`：

```json
{
  "task": "开会",
  "run_at": "2026-07-30T20:25:00"
}
```

2 分钟后 APScheduler 触发 → `send_notification()` → WxPusher 推送到手机。
