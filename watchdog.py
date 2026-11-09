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

只在有异常时推送；全绿时完全静默。异常时同时让任务标红，GitHub 会另发一封
失败邮件，等于多一条免费通道。
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


def check_one(repo, label, token, since):
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
        base["reason"] = "今天没有运行（最近一次 %s）" % newest_at
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


def build_alert(bad, now):
    lines = ["⚠️ 签到巡检异常（%s 北京时间）" % now.astimezone(BEIJING).strftime("%m-%d %H:%M"), ""]
    for item in bad:
        lines.append("• %s：%s" % (item["label"], item["reason"]))
        lines.append("  github.com/%s/%s/actions" % (OWNER, item["repo"]))
    lines.append("")
    lines.append("巡检只查有没有运行；签到跑起来后的失败由各项目自己推送。")
    return "\n".join(lines)


def main():
    token = (os.environ.get("WATCH_PAT") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("缺少 WATCH_PAT，无法读取各仓库运行记录")
        return 2

    now = utcnow()
    bj_day = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    since = bj_day.astimezone(datetime.timezone.utc)

    print("=== 签到巡检 %s（北京时间，判定起点 %s）===" % (
        now.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M"), bj_day.strftime("%m-%d 00:00")))
    results = [check_one(repo, label, token, since) for repo, label in TARGETS]
    for item in results:
        print("  [%s] %-11s %-24s %s" % (
            "OK" if item["ok"] else "!!", item["label"], item["repo"], item["reason"]))

    bad = [x for x in results if not x["ok"]]
    force = (os.environ.get("WATCH_FORCE_ALERT") or "").strip().lower() in ("1", "true", "yes")
    if not bad and not force:
        print("全部正常，本次不推送")
        return 0

    testing = force and not bad
    if testing:
        bad = [{"label": "通道自检", "repo": "checkin-watchdog",
                "reason": "这是一条测试告警，五个项目当前都正常，忽略即可"}]
    text = build_alert(bad, now)
    print("--- 推送内容 ---")
    print(text)
    ok = push_wecom(text)
    if testing:
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
