# 心跳告警中转（checkin-watchdog）

> 巡检各签到项目当天有没有跑，异常才推企业微信。这是**告警中转站**，本身不签到。

**仓库名说明**：GitHub 不支持中文仓库名（会被静默丢弃），所以仓库名是英文
`checkin-watchdog`。中文别名记在仓库描述里：**【心跳告警中转】**。

---

## 这个仓库是干什么的

Healthchecks 判定某个签到项目超时（Down）后，会把事件打到这里，
由本仓库的 workflow 转发到企业微信，并**点名到具体是哪个项目**。

没有它，你只会收到一条「某个 check 挂了」，不知道是哪个。

| 项目 | 说明 |
|---|---|
| 作用 | 心跳告警中转 + 企业微信推送 |
| 触发方式 | Healthchecks Webhook → `repository_dispatch` |
| 本仓库是否签到 | **否**。它只转发告警 |
| 谁在用它 | 全部 6 个签到项目共用这一个中转仓库 |

---

## 工作流程

```
签到项目今天没跑
   ↓
Healthchecks 判定 Down（过了宽限期）
   ↓
Webhook 打到本仓库（repository_dispatch）
   ↓
heartbeat_alert job 读 checks_map.json，查出是哪个仓库
   ↓
推企业微信：「签到-XXX 超时，来源 caiping527/xxx」
```

---

## 目录

```
.github/workflows/watch.yml      Webhook 入口，收到 Down 事件就推企业微信
.github/workflows/keepalive.yml  保活，防止仓库长期无活动被停用
watchdog.py                      告警处理逻辑
provision_heartbeats.py          给各签到仓库注入心跳 job 的工具
checks_map.json                  点名映射表
使用说明.txt                      操作与排障
```

## 两个 job

| job | 作用 |
|---|---|
| `heartbeat_alert` | 收到 Webhook 后，点名到具体仓库并推企业微信 |
| 自检分支 | 手动触发时发一条测试消息，验证链路通不通 |

---

## 需要的 Secrets

| 名称 | 用途 |
|---|---|
| `WATCH_PAT` | GitHub 个人令牌，用于读各仓库状态 |
| `WECOM_APP` | 企业微信应用凭据（corpid,corpsecret,touser,agentid） |

两个缺一个，告警就推不出去。

---

## checks_map.json

点名映射表：Healthchecks 的 check 名 → 对应的 GitHub 仓库。

```json
{
  "签到-WorkBuddy": "caiping527/workbuddy-checkin-public",
  "签到-小浣熊": "caiping527/raccoon-checkin",
  "签到-Qoder": "caiping527/qoder-checkin"
}
```

由「自动心跳监测」每次运行时自动同步，**不用手改**。
新增签到项目后，重跑一次心跳就会自动加进来。

---

## 重要说明

- **仓库必须私有**。里面有各仓库的映射关系和企业微信凭据引用。
- **本仓库不签到**，删掉它不会影响签到本身，只会让告警推不出去。
- 告警只推**失败**，成功不打扰。
- 企业微信必须是 **text 类型**：自建应用不认 markdown，
  用 markdown 会返回 ok 但内容显示异常。

---

## 心跳监测

本仓库在心跳的**排除列表**里，因为它自己就是告警通道，
不需要再被监测一遍（否则它挂了没人告警，形成死循环）。

---

## 排错

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 收不到告警 | WECOM_APP 缺或错 | 查 Secrets，跑一次自检分支 |
| 告警不点名 | checks_map.json 缺条目 | 重跑「自动心跳监测」 |
| 消息内容乱码 | 用了 markdown | 改回 text 类型 |
| workflow 不触发 | 仓库被 archive | 取消归档 |

---

## 相关仓库

| 仓库 | 中文名 |
|---|---|
| `workbuddy-checkin-public` | WorkBuddy 签到 |
| `raccoon-checkin` | 小浣熊签到 |
| `qoder-checkin` | Qoder 签到 |
| `Cloud189Checkin` | 天翼云盘签到 |
| `chinafix-sign` | 迅维网签到 |
| `hyperdown-checkin` | Hyperdown 签到 |

---

## 许可

个人自用，不对外分发。
