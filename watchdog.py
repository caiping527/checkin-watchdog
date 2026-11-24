#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立签到巡检：只检查各项目「今天有没有真正运行起来」，异常才推企业微信。

为什么需要独立巡检：告警代码如果长在签到任务内部，任务没启动时告警代码
也一起没启动，永远不会有人报信。本脚本跑在单独的私有仓库里，用 GitHub API
读取各项目的运行记录，因此「完全没运行」也能被发现。

判定口径（按用户要求，只查有没有运行，不查业务是否签到成功）：
  · 北京时间今天 00:00 之后有过成功完成的运行        → 正常
  · 今天有运行且还在进行中 / 刚排队                  → 正常（不误报）
  · 今天没有任何运行记录                             → 异常，推送
  · 今天有运行但全部被拦 / 失败 / 长时间排队没启动    → 异常，推送
     （例如 action_required、failure、queued 超时：0 个 job，等于没跑）
  · 签到跑起来了但业务失败：不归本巡检管，各项目自己会推失败提醒

工作流自愈：每天额外检查各项目 workflow 是否被 GitHub 停用（例如 60 天
无活动自动禁用）。发现 disabled_* 状态时用 API 自动重新启用，并在告警里
写明「停在哪、已经做了什么、还要你做什么」，不再只报一句「没运行」。

只在有异常时推送；全绿时完全静默。异常时同时让任务标红，GitHub 会另发一封
失败邮件，等于多一条免费通道。

第三方告警由五个项目在签到成功后各自向自己的 Healthchecks check 上报，
不依赖本巡检是否按时运行；本脚本只负责检查运行记录、自动重开被停用的
workflow，以及在异常时点名推送。
"""

import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

OWNER = "caiping527"
TARGETS = [
    ("chinafix-sign", "迅维网"),
    ("hyperdown-checkin", "Hyperdown"),
    ("workbuddy-checkin-public", "WorkBuddy"),
    ("raccoon-checkin", "小浣熊"),
    ("Cloud189Checkin", "天翼云盘"),
]
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
STALE_QUEUED_MIN = float(os.environ.get("WATCH_STALE_QUEUED_MIN") or "90")
ACTIVE_STATUS = {"in_progress", "queued", "waiting", "pending", "requested"}
API_ROOT = "https://api.github.com"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def bj_text(dt):
    return dt.astimezone(BEIJING).strftime("%m-%d %H:%M")


def api_get(path, token, attempts=3):
    """GitHub 偶尔会把大响应截断（IncompleteRead），重试并缩小单页条数。"""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                API_ROOT + path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": "Bearer " + token,
                    "User-Agent": "checkin-watchdog",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def enable_workflow(repo, workflow_id, token):
    """重新启用被 GitHub 停用的 workflow。需要令牌带 repo / actions:write 权限。"""
    path = "/repos/%s/%s/actions/workflows/%s/enable" % (OWNER, repo, workflow_id)
    try:
        req = urllib.request.Request(
            API_ROOT + path,
            data=b"",
            method="PUT",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "User-Agent": "checkin-watchdog",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        print("  [自愈] 重新启用 %s 失败：%s" % (repo, str(exc)[:110]))
        return False


def ensure_enabled(repo, token):
    """检查 workflow 是否被停用；能自动开就自动开，返回给告警用的说明。

    主动检查而不等它「今天没运行」才查：有些工作流是当天跑完后才被停用的，
    提前发现并开回来，第二天才不会断。
    """
    try:
        data = api_get("/repos/%s/%s/actions/workflows?per_page=30" % (OWNER, repo), token)
    except Exception as exc:
        print("  [自愈] 读取 %s 工作流状态失败：%s" % (repo, str(exc)[:110]))
        return []
    disabled = [w for w in (data.get("workflows") or [])
                if w.get("state") not in ("active", "deleted")]
    if not disabled:
        return []

    notes = []
    for wf in disabled:
        state = wf.get("state") or "未知状态"
        name = wf.get("name") or wf.get("path") or "workflow"
        if state.startswith("disabled_") and wf.get("id"):
            if enable_workflow(repo, wf["id"], token):
                print("  [自愈] %s：%s 被停用(%s)，已自动重新启用" % (repo, name, state))
                notes.append("工作流「%s」曾被 GitHub 停用(%s)，已自动重新启用；明天恢复，想立刻补签点 Run workflow"
                             % (name, state))
            else:
                notes.append("工作流「%s」被停用(%s)，自动重开失败：请到 Actions 页点 Enable workflow"
                             % (name, state))
        else:
            notes.append("工作流「%s」状态异常 %s，请到 Actions 页检查" % (name, state))
    return notes


def check_one(repo, label, token, since, day_word="今天"):
    base = {"repo": repo, "label": label, "ok": False, "reason": ""}
    try:
        data = api_get(
            "/repos/%s/%s/actions/runs?per_page=10" % (OWNER, repo), token)
    except Exception as exc:
        base["reason"] = "巡检读取失败（重试3次）：%s" % str(exc)[:90]
        return base

    runs = data.get("workflow_runs") or []
    if not runs:
        base["reason"] = "该仓库从未有过运行记录"
        return base

    today = [r for r in runs if parse_time(r["created_at"]) >= since]
    newest = runs[0]
    newest_at = bj_text(parse_time(newest["created_at"]))
    if not today:
        base["reason"] = "%s没有运行（最近一次 %s）" % (day_word, newest_at)
        return base

    good = [r for r in today if r.get("conclusion") == "success"]
    if good:
        base["ok"] = True
        base["reason"] = "已运行，成功于 %s" % bj_text(parse_time(good[0]["created_at"]))
        return base

    now = utcnow()
    fresh = [
        r for r in today
        if r.get("status") in ACTIVE_STATUS
        and (now - parse_time(r["created_at"])).total_seconds() / 60.0 < STALE_QUEUED_MIN
    ]
    if fresh:
        base["ok"] = True
        base["reason"] = "正在运行中，暂不判定"
        return base

    state = today[0].get("conclusion") or today[0].get("status") or "未知"
    if state == "queued":
        state = "排队超过 %g 分钟仍未启动" % STALE_QUEUED_MIN
    base["reason"] = "有运行但没跑起来：%s（%s）" % (state, newest_at)
    return base


def push_wecom(text):
    """推送到企业微信自建应用。凭据格式 corpid,corpsecret,touser,agentid。"""
    raw = (os.environ.get("WECOM_APP") or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 4:
        print("[推送] 未配置 WECOM_APP，跳过")
        return False
    corpid, secret, touser, agentid = parts[:4]
    try:
        token_url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?" + urllib.parse.urlencode(
            {"corpid": corpid, "corpsecret": secret})
        with urllib.request.urlopen(token_url, timeout=15) as resp:
            tk = json.loads(resp.read().decode("utf-8", "replace"))
        access = tk.get("access_token")
        if not access:
            print("[推送] 取 access_token 失败 errcode=%s" % tk.get("errcode"))
            return False
        body = json.dumps({
            "touser": touser or "@all",
            "msgtype": "text",
            "agentid": int(agentid),
            "text": {"content": text},
            "safe": 0,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token="
            + urllib.parse.quote(access),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
        print("[推送] errcode=%s errmsg=%s msgid=%s invaliduser=%s" % (
            out.get("errcode"), out.get("errmsg"), out.get("msgid"),
            out.get("invaliduser")))
        return out.get("errcode") == 0
    except Exception as exc:
        print("[推送] 异常：%s" % str(exc)[:150])
        return False


FIX_GUIDE = [
    "1 被停用：Actions 页黄色横幅点 Enable workflow；本巡检已会自动重开",
    "2 待审核 action_required：只发生在公开仓库，现已全转私仓；万一出现点 Approve",
    "3 排队卡住 queued：GitHub 调度拥堵，等下次自动恢复，或手动 Run workflow",
    "4 运行失败 failure：点红色 run 看日志，多半是登录态/密码过期，重跑密码登录",
    "5 没运行也没横幅：直接点 Run workflow 手动补签，越早越好",
]


# 项目直报：每个签到项目有专属 Healthchecks check，Down 时按名字点名到仓库
CHECK_REPOS = {
    "签到-天翼云盘": "Cloud189Checkin",
    "签到-小浣熊": "raccoon-checkin",
    "签到-WorkBuddy": "workbuddy-checkin-public",
    "签到-Hyperdown": "hyperdown-checkin",
    "签到-迅维网": "chinafix-sign",
}

# 自动心跳监测程序（D:\自动心跳监测）维护的映射，支持多账号和新增项目。
CHECKS_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "checks_map.json")


def load_check_repos():
    """优先读自动维护的映射；文件不存在/损坏时回退内置清单。

    value 写成 "repo" 表示本 OWNER 名下，写成 "owner/repo" 可指向其它账号。
    """
    try:
        with open(CHECKS_MAP_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        pass
    return dict(CHECK_REPOS)


def build_heartbeat_alert(name, status, now):
    """第三方心跳判超时后的告警。这条消息由 Healthchecks 经 GitHub 触发，
    不经过本巡检程序，所以能兜住「巡检自己完全没跑」。

    name 是 Healthchecks 的 check 名：项目级 check 点名到具体仓库；
    名字不在清单里时给一条通用兜底文案（多为链路自检或残留配置）。
    """
    stamp = now.astimezone(BEIJING).strftime("%m-%d %H:%M")
    label = name or "未知项目"
    state = status or "Down"
    repo = load_check_repos().get(label)
    if repo:
        if "/" in repo:
            owner, name = repo.split("/", 1)
        else:
            owner, name = OWNER, repo
        return "\n".join([
            "⚠️ 项目签到未上报（%s 北京时间）" % stamp,
            "",
            "第三方心跳没收到今天的成功上报：%s（状态 %s）" % (label, state),
            "说明：该项目今天没跑完签到，或跑完但失败。各项目独立监控，缺谁报谁。",
            "",
            "【怎么处理】",
            "1 打开 github.com/%s/%s/actions" % (owner, name),
            "2 看今天有没有红色 run，点进去看日志",
            "3 有黄色横幅就点 Enable workflow",
            "4 点 Run workflow 手动补签",
            "常见原因：GitHub 定时调度积压、登录态/密码过期、Secret 失效。",
        ])
    return "\n".join([
        "⚠️ 心跳来源未知（%s 北京时间）" % stamp,
        "",
        "收到一个不在项目清单里的心跳事件：%s（状态 %s）" % (label, state),
        "多半是 Healthchecks 的 Test 自检或残留 check，非真实故障。",
        "",
        "【怎么处理】",
        "1 打开 Healthchecks 检查列表，确认五个「签到-xxx」check 是否 Down",
        "2 缺谁点谁：github.com/caiping527/<对应仓库>/actions → Run workflow 补签",
        "3 本仓库 Actions 页可看这条推送的运行日志",
        "常见原因：Test 按钮、check 改名、旧配置残留。",
    ])


def send_heartbeat_event():
    """由 repository_dispatch 触发：把心跳的 Down/Up 事件推到微信。

    只做推送，不跑巡检、不发任何报平安 ping，避免把故障误记成正常。
    """
    event = (os.environ.get("HC_EVENT") or "heartbeat_down").strip()
    test = (os.environ.get("HC_TEST") or "").strip().lower() in ("1", "true", "yes")
    if event == "heartbeat_up":
        label = os.environ.get("HC_NAME") or "未知项目"
        text = "✅ %s 已恢复上报（%s 北京时间），忽略即可" % (
            label, utcnow().astimezone(BEIJING).strftime("%m-%d %H:%M"))
    elif test:
        label = os.environ.get("HC_NAME") or "自动心跳监测"
        text = "\n".join([
            "✅ 自动心跳监测 · 配置自检通过（%s 北京时间）"
            % utcnow().astimezone(BEIJING).strftime("%m-%d %H:%M"),
            "",
            "心跳配置已完成，告警链路验证成功：%s" % label,
            "这条消息只验证通道，不代表任何项目签到失败，忽略即可。",
        ])
    else:
        text = build_heartbeat_alert(
            os.environ.get("HC_NAME"), os.environ.get("HC_STATUS"), utcnow())
    print("--- 心跳事件 %s 推送内容 ---" % event)
    print(text)
    return 0 if push_wecom(text) else 1


def build_alert(bad, now):
    lines = ["⚠️ 签到巡检异常（%s 北京时间）" % now.astimezone(BEIJING).strftime("%m-%d %H:%M"), ""]
    for item in bad:
        lines.append("• %s：%s" % (item["label"], item["reason"]))
        for note in (item.get("notes") or []):
            lines.append("  → %s" % note)
        lines.append("  github.com/%s/%s/actions" % (OWNER, item["repo"]))
    lines.append("")
    lines.append("【停了怎么解决】")
    lines.extend(FIX_GUIDE)
    lines.append("")
    lines.append("若连这条巡检消息都没收到 = 巡检仓库自己也停了：")
    lines.append("github.com/caiping527/checkin-watchdog/actions → Enable / Run workflow")
    lines.append("巡检只查有没有运行；签到业务失败由各项目自己推送。")
    return "\n".join(lines)


def main():
    # 外部心跳判定超时/恢复时，由 repository_dispatch 单独触发这一分支。
    # 必须放在 WATCH_PAT 检查之前：该 job 只需要 WECOM_APP。
    if "--heartbeat-alert" in sys.argv:
        return send_heartbeat_event()

    token = (os.environ.get("WATCH_PAT") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("缺少 WATCH_PAT，无法读取各仓库运行记录")
        return 2

    now = utcnow()
    bj_now = now.astimezone(BEIJING)
    # 这班自己被 GitHub 拖到凌晨（00:00~08:59）时，判定窗口退回"昨天"：
    # 那时"今天"才刚开始，各项目根本还没到点，按今天判只会全员误报
    # （2026-11-24 01:57 那条五种项目全"没运行"的假告警就是这么来的）。
    if bj_now.hour < 9:
        bj_day = (bj_now - datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        day_word = "昨天"
    else:
        bj_day = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_word = "今天"
    since = bj_day.astimezone(datetime.timezone.utc)

    print("=== 签到巡检 %s（北京时间，判定%s起点 %s）===" % (
        bj_now.strftime("%Y-%m-%d %H:%M"), day_word, bj_day.strftime("%m-%d 00:00")))

    print("--- 工作流状态自愈 ---")
    state_notes = {}
    for repo, label in TARGETS:
        state_notes[repo] = ensure_enabled(repo, token)
    if not any(state_notes.values()):
        print("  五个项目的 workflow 均为 active，无需处理")

    results = [check_one(repo, label, token, since, day_word) for repo, label in TARGETS]

    # 13:00 前不判"今天没跑"：这个点之前各项目还在 GitHub 的调度延迟里，
    # 判了只会误报；有运行但失败 / 卡住不动的照常报。14:00 的补跑班会接手。
    if day_word == "今天" and bj_now.hour < 13:
        for item in results:
            if not item["ok"] and "没有运行" in item["reason"]:
                item["ok"] = True
                item["reason"] += "（13:00 前不判定，继续等调度）"

    for item in results:
        item["notes"] = state_notes.get(item["repo"]) or []
        print("  [%s] %-11s %-24s %s" % (
            "OK" if item["ok"] else "!!", item["label"], item["repo"], item["reason"]))
        for note in item["notes"]:
            print("      -> %s" % note)

    bad = [x for x in results if not x["ok"]]
    force = (os.environ.get("WATCH_FORCE_ALERT") or "").strip().lower() in ("1", "true", "yes")
    if not bad and not force:
        print("全部正常，本次不推送")
        rc = 0
    else:
        testing = force and not bad
        if testing:
            bad = [{"label": "通道自检", "repo": "checkin-watchdog",
                    "reason": "这是一条测试告警，五个项目当前都正常，忽略即可"}]
        text = build_alert(bad, now)
        print("--- 推送内容 ---")
        print(text)
        ok = push_wecom(text)
        rc = (0 if ok else 1) if testing else 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
