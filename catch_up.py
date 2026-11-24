#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到补跑检查（跑在 checkin-watchdog 仓库，下午 14:00 一次）

为什么要有它
------------
GitHub 的定时（schedule）只是 best-effort：实测会延迟 4~9 小时，负载高时
还会把整班直接丢掉、而且不补跑。三个签到主 workflow 一天只有一班，一旦被
丢掉，当天就彻底没有签到（2026-11-24 Qoder 就是这样漏的）。

这里在下午 14:00 再查一次：今天已经成功跑过就什么都不做；确实一班都没跑成，
才补触发一次原来的主 workflow。

怎么保证不重复签到（防风控）
----------------------------
1) 三个条件同时成立才补：今天 0 次成功 + 没有在跑/排队 + 今天没补过；
2) 补的是原来那个主 workflow，脚本自己幂等：
     Qoder      → 今天已领取会直接返回，不会领第二次
     WorkBuddy  → 返回「今日已签到，无需重复领取」
     小浣熊      → 返回「今日每日积分 +300 已到账」
3) 只在 09:00~23:00 之间动作。这班自己被 GitHub 拖到凌晨时不补，
   免得半夜去登录（风控）。

用法
----
    python3 catch_up.py            正常执行
    python3 catch_up.py --dry-run  只打印判断结果，不触发任何 workflow
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

OWNER = "caiping527"
API = "https://api.github.com"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

# 只补本项目管的三个签到仓库；Cloud189 / Hyperdown / 迅维网 不归这里补
# （它们是第三方脚本，乱补容易触发对方风控，仍然只由巡检报警）。
TARGETS = [
    ("raccoon-checkin", "checkin.yml", "小浣熊"),
    ("workbuddy-checkin-public", "checkin.yml", "WorkBuddy"),
    ("qoder-checkin", "qoder-checkin.yml", "Qoder"),
]

ACTIVE_STATUS = {"queued", "in_progress", "waiting", "pending", "requested"}
WINDOW_START = int(os.environ.get("CATCHUP_WINDOW_START") or 9)    # 北京时间几点后允许补
WINDOW_END = int(os.environ.get("CATCHUP_WINDOW_END") or 23)       # 北京时间几点后不再补

TOKEN = (os.environ.get("WATCH_PAT") or os.environ.get("GITHUB_TOKEN") or "").strip()


def api(path, method="GET", body=None, attempts=3):
    """调 GitHub API，返回 (状态码, 解析后的 JSON 或原始文本)。"""
    url = API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + TOKEN,
        "User-Agent": "checkin-catchup",
    }
    if data:
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            return exc.code, detail
        except Exception as exc:  # 网络抖动 / IncompleteRead
            last = exc
            time.sleep(2 + i * 2)
    raise RuntimeError("请求失败（重试 %d 次）：%s" % (attempts, last))


def parse_time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def bj_text(dt):
    return dt.astimezone(BEIJING).strftime("%m-%d %H:%M")


def run_path_matches(run, workflow_file):
    return str(run.get("path") or "").endswith("/" + workflow_file)


def check_one(repo, workflow_file, label, since, dry_run):
    """判断一个仓库要不要补跑；返回一句话结果。"""
    print("\n--- %s（%s）---" % (label, repo))
    status, data = api("/repos/%s/%s/actions/runs?per_page=30" % (OWNER, repo))
    if status != 200 or not isinstance(data, dict):
        print("  读取运行记录失败（HTTP %s）：%s" % (status, str(data)[:150]))
        return False, "读取失败"

    runs = [r for r in (data.get("workflow_runs") or []) if run_path_matches(r, workflow_file)]
    today = [r for r in runs if parse_time(r["created_at"]) >= since]
    print("  今天这班的运行记录：%d 条" % len(today))

    good = [r for r in today if r.get("conclusion") == "success"]
    if good:
        print("  今天已成功（%s），不补" % bj_text(parse_time(good[0]["created_at"])))
        return True, "已成功"

    active = [r for r in today if r.get("status") in ACTIVE_STATUS]
    if active:
        print("  有运行在排队/进行中（%s），不补" % active[0].get("status"))
        return True, "在跑"

    dispatched = [r for r in today if r.get("event") == "workflow_dispatch"]
    if dispatched:
        print("  今天已经补过（%s，%s），不重复补" % (
            bj_text(parse_time(dispatched[0]["created_at"])),
            dispatched[0].get("conclusion") or dispatched[0].get("status")))
        return True, "已补过"

    if today:
        latest = today[0]
        print("  今天有运行但没成功：%s / %s" % (
            latest.get("event"), latest.get("conclusion") or latest.get("status")))
    else:
        print("  今天一条运行记录都没有（主班被 GitHub 丢了或还压在队列里）")

    if dry_run:
        print("  [预览] 将触发 %s 的 %s" % (repo, workflow_file))
        return True, "预览补跑"

    status, data = api(
        "/repos/%s/%s/actions/workflows/%s/dispatches" % (OWNER, repo, workflow_file),
        method="POST", body={"ref": "main"})
    if status in (204, 201):
        print("  已触发补跑：https://github.com/%s/%s/actions" % (OWNER, repo))
        return True, "已补跑"
    print("  触发失败（HTTP %s）：%s" % (status, str(data)[:180]))
    return False, "触发失败"


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.datetime.now(datetime.timezone.utc)
    bj_now = now.astimezone(BEIJING)

    print("=== 签到补跑检查 %s（北京时间）%s ===" % (
        bj_now.strftime("%Y-%m-%d %H:%M"), "【预览，不触发】" if dry_run else ""))

    if not TOKEN:
        print("缺少 WATCH_PAT，无法读取/触发")
        return 2

    if not (WINDOW_START <= bj_now.hour < WINDOW_END):
        print("现在不在 %02d:00~%02d:00 之间，本次不补（避免半夜签到）"
              % (WINDOW_START, WINDOW_END))
        return 0

    since = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = since.astimezone(datetime.timezone.utc)

    failed = []
    for repo, wf, label in TARGETS:
        ok, why = check_one(repo, wf, label, since, dry_run)
        if not ok:
            failed.append(label)

    print("\n=== 汇总：%s ===" % ("全部不需要补" if not failed else "有 %d 个触发失败" % len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
