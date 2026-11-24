# 心跳告警中转（checkin-watchdog）

> 每天下午巡检各签到项目当天有没有跑，异常才推企业微信；跑漏了就补一次。
> 这是**告警中转站**，本身不签到。

**仓库名说明**：GitHub 不支持中文仓库名（会被静默丢弃），所以仓库名是英文
`checkin-watchdog`。中文别名记在仓库描述里：**【心跳告警中转】**。

---

## 这个仓库是干什么的

三件事：

1. **每天 14:07 巡检**（`watch.yml`）：点名各签到项目有没有跑成功，异常推企业微信。
2. **每天 14:00 补跑检查**（`catchup.yml`）：今天还没成功、也没在跑的，补触发一次原 workflow。
3. **告警转发**：Healthchecks 判定某个项目 Down 后，Webhook 打进来，转成一条点名到具体项目的企业微信消息。

没有它，你只会收到一条「某个 check 挂了」，不知道是哪个。

| 项目 | 说明 |
|---|---|
| 作用 | 每天的巡检 + 补跑 + 心跳告警转发 |
| 触发方式 | 定时（14:00 补跑 / 14:07 巡检）+ Healthchecks Webhook |
| 本仓库是否签到 | **否**。补跑只是去触发别的仓库，自己不登录任何账号 |
| 谁在用它 | 全部 6 个签到项目共用这一个中转仓库 |

---

## 两个定时（都是北京时间）

| 时间 | 文件 | 干什么 |
|---|---|---|
| 14:00 | `catchup.yml` → `catch_up.py` | 今天三个签到仓库有没有成功？没有就补一次 |
| 14:07 | `watch.yml` → `watchdog.py` | 五个项目点名，异常推企业微信 |

### 补跑（14:00）为什么不会重复签到

三个条件同时成立才补：

- 今天 0 次成功
- 没有在跑 / 排队
- 今天没有补过（没有 `workflow_dispatch` 记录）

补的是原来那个主 workflow，脚本本身幂等：

| 项目 | 今天已经领过时的输出 |
|---|---|
| Qoder | 直接返回，不会领第二次 |
| WorkBuddy | 今日已签到，无需重复领取 |
| 小浣熊 | 今日每日积分 +300 已到账 |

另外只在 **09:00~23:00** 之间动作——这班自己被 GitHub 拖到凌晨时不补，免得半夜登录触发风控。

### 巡检（14:07）为什么不会凌晨误报

GitHub 的定时会延迟，这班自己被拖到凌晨时：

- **00:00~08:59 运行**：判定窗口退回「昨天」，不会把刚开始的新一天全判成没跑。
- **09:00~12:59 运行**：项目「没跑」先不判（还在调度延迟里），只报「有运行但失败」。
- **13:00 之后**：正常判定。

（2026-11-24 01:57 那次五个项目全标红，就是被拖到凌晨还按「今天 00:00」判导致的，已修。）

---

## 工作流程

```
每天 14:00  catch_up.py：今天成功了吗？
              ├─ 是 / 在跑 / 已补过 → 什么都不做
              └─ 否 → 触发原 workflow（脚本幂等，不会重复领）

每天 14:07  watchdog.py：五个项目点名
              ├─ 都正常 → 不推送
              └─ 有异常 → 企业微信点名

Healthchecks Down
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
.github/workflows/watch.yml      14:07 巡检 + Webhook 告警入口
.github/workflows/catchup.yml    14:00 补跑检查入口
.github/workflows/keepalive.yml  保活，防止仓库长期无活动被停用
watchdog.py                      巡检逻辑（判定 + 点名 + 推企业微信）
catch_up.py                      补跑逻辑（判断今天成功没，缺了就补一次）
provision_heartbeats.py          给各签到仓库注入心跳 job 的工具
checks_map.json                  点名映射表
使用说明.txt                      操作与排障
```

## 三个 job

| job / workflow | 作用 |
|---|---|
| `catchup.yml` | 14:00 补跑检查，只补今天一次都没成功的那几个 |
| `watch.yml` 的 `watch` | 14:07 巡检五个项目，异常才推 |
| `watch.yml` 的 `heartbeat_alert` | 收到 Webhook 后，点名到具体仓库并推企业微信 |

手动触发 `watch.yml` 时发一条测试消息，验证链路通不通。

---

## 需要的 Secrets

| 名称 | 用途 |
|---|---|
| `WATCH_PAT` | GitHub 个人令牌，用于读各仓库状态、以及补跑时触发别的 workflow |
| `WECOM_APP` | 企业微信应用凭据（corpid,corpsecret,touser,agentid） |

两个缺一个，告警就推不出去（补跑也动不了）。

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
- **本仓库不签到**，删掉它不会影响签到本身，只会让告警推不出去、丢班没人补。
- 告警只推**失败**，成功不打扰。
- 企业微信必须是 **text 类型**：自建应用不认 markdown，
  用 markdown 会返回 ok 但内容显示异常。
- 补跑只覆盖 **小浣熊 / WorkBuddy / Qoder** 三个自有仓库；
  Cloud189 / 迅维网 / Hyperdown 是第三方脚本，只报警不补跑（乱补容易触发对方风控）。

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
| 补跑没动作 | 今天已成功 / 在跑 / 已补过 | 看 `catchup.yml` 的运行日志，三行判断都写在里面 |
| 补跑触发了但没领到 | 原 workflow 自己失败 | 看那个仓库的运行日志，问题不在补跑 |

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
