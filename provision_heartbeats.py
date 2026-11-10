# -*- coding: utf-8 -*-
"""一次性配置：项目直报心跳（Healthchecks 5 个专属 check + 5 个仓库变量）。

用法（本机）：
    HC_API_KEY=<Healthchecks 项目级读写 API Key> python provision_heartbeats.py

作用：
  1. 用 Management API v3 建/复用 5 个项目专属 check：
     Cron ``0 16 * * *``(UTC) = 北京次日 00:00 起应收 ping，
     Grace 14 小时 -> 北京 14:00 仍未收到就点名告警。
     覆盖 GitHub 定时调度 0~13 小时的漂移，不误报也不漏到晚上。
     每个 check 通过 channels="*" 自动挂上项目里全部通知集成
     （邮箱 + 微信 Webhook），否则新建的 check 超时也发不出告警。
  2. 把每个 Ping URL 写入对应仓库的 Actions Variable ``HC_PING_URL``
     （各工作流的 heartbeat job 已在签到全部成功后读取它）。
  3. 各 ping 一次完成首次激活，避免 check 停留在 New 状态不触发告警。
  4. 删除已废弃的旧「签到巡检」check（巡检不再上报心跳），
     并删掉注册时的空 check。

脚本不含任何明文凭据；API Key 只从环境变量 HC_API_KEY 读取。
unique=["name"] 保证重复执行不会建出重复 check。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OWNER = "caiping527"
API_ROOT = "https://healthchecks.io/api/v3/checks/"
CHANNELS_API = "https://healthchecks.io/api/v3/channels/"
SCHEDULE = "0 16 * * *"      # UTC 16:00 = 北京次日 00:00
GRACE = 14 * 3600            # 14 小时 -> 北京 14:00 告警
OLD_CHECK = "签到巡检"        # 已废弃的巡检级 check，重建时若存在则删除
JUNK_CHECK = "My First Check"
CHECKS = [
    ("签到-天翼云盘", "Cloud189Checkin"),
    ("签到-小浣熊", "raccoon-checkin"),
    ("签到-WorkBuddy", "workbuddy-checkin-public"),
    ("签到-Hyperdown", "hyperdown-checkin"),
    ("签到-迅维网", "chinafix-sign"),
]


def hc(method, url, key, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("Healthchecks HTTP %s: %s" % (exc.code, detail)) from exc


def desc_for(name, repo):
    return (
        "%s 每日签到成功后由 GitHub Actions 上报心跳。"
        "预期北京时间 00:00 起应收一次 ping，14:00 前没收到即告警。\n"
        "处理：1 打开 github.com/caiping527/%s/actions "
        "2 看当天有没有红色 run 并点开日志 "
        "3 有黄色横幅就点 Enable workflow "
        "4 点 Run workflow 手动补签。\n"
        "常见原因：GitHub 定时调度积压、登录态/密码过期、Secret 失效。" % (name, repo)
    )


def set_variable(repo, value):
    cmd = ["gh", "variable", "set", "HC_PING_URL",
           "-R", OWNER + "/" + repo, "--body", value]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode == 0:
        return "ok"
    # 后备：直接调 GitHub REST API（PATCH 更新 / POST 新建）
    base = "repos/%s/%s/actions/variables" % (OWNER, repo)
    try:
        subprocess.run(["gh", "api", "-X", "PATCH", base + "/HC_PING_URL",
                        "-f", "value=" + value], capture_output=True, check=True)
        return "ok(api-patch)"
    except subprocess.CalledProcessError:
        subprocess.run(["gh", "api", "-X", "POST", base,
                        "-f", "name=HC_PING_URL", "-f", "value=" + value],
                       capture_output=True, check=True)
        return "ok(api-post)"


def main():
    key = os.environ.get("HC_API_KEY", "").strip()
    if not key:
        print("缺少 HC_API_KEY 环境变量", file=sys.stderr)
        return 2

    channels = hc("GET", CHANNELS_API, key).get("channels", [])
    print("[Healthchecks] 现有通知集成 %d 个：%s"
          % (len(channels), "、".join(
              "%s(%s)" % (c.get("name") or "未命名", c.get("kind")) for c in channels)))
    if not any(c.get("kind") == "webhook" for c in channels):
        print("!! 未发现 Webhook 集成，新建 check 将不会推到微信", file=sys.stderr)

    listing = hc("GET", API_ROOT, key).get("checks", [])
    by_name = {c.get("name"): c for c in listing}
    print("[Healthchecks] 现有 check %d 个" % len(listing))

    result = []
    for name, repo in CHECKS:
        body = {
            "name": name,
            "tags": "签到直报",
            "desc": desc_for(name, repo),
            "schedule": SCHEDULE,
            "tz": "UTC",
            "grace": GRACE,
            "channels": "*",
            "unique": ["name"],
        }
        check = hc("POST", API_ROOT, key, body)
        action = "复用" if name in by_name else "新建"
        ping_url = check.get("ping_url") or ""
        if not ping_url:
            raise RuntimeError("%s 未返回 ping_url：%s" % (name, check))
        var_state = set_variable(repo, ping_url)
        try:
            with urllib.request.urlopen(ping_url, timeout=20) as resp:
                activated = "HTTP %s" % resp.status
        except Exception as exc:                                  # noqa: BLE001
            activated = "激活失败 %s" % str(exc)[:60]
        fresh = hc("GET", API_ROOT + check["uuid"], key)
        result.append((name, repo, action, var_state, activated, fresh))
        print("  [%s] %-16s %-26s 变量=%-14s 激活=%-10s 状态=%s 下次=%s"
              % (action, name, repo, var_state, activated,
                 fresh.get("status"), fresh.get("next_ping")))

    # 旧巡检级 check 已废弃：2026-11-10 起巡检不再 ping，重建时直接删除
    old = by_name.get(OLD_CHECK)
    if old:
        hc("DELETE", API_ROOT + old["uuid"], key)
        print("[清理] 已删除废弃 check「%s」" % OLD_CHECK)
    junk = by_name.get(JUNK_CHECK)
    if junk:
        hc("DELETE", API_ROOT + junk["uuid"], key)
        print("[清理] 已删除注册时的空 check「%s」" % JUNK_CHECK)

    print("\n=== 复核 ===")
    final = {c["name"]: c for c in hc("GET", API_ROOT, key).get("checks", [])}
    for name, repo in CHECKS:
        c = final.get(name, {})
        ok = (c.get("schedule") == SCHEDULE and c.get("grace") == GRACE
              and bool(c.get("channels")) and c.get("status") in ("up", "new", "grace"))
        print("  %-16s %-26s %s status=%-6s channels=%s"
              % (name, repo, "OK" if ok else "!!", c.get("status"),
                 "已挂载" if c.get("channels") else "未挂载"))

    print("\n[完成] %d 个项目已接线。Ping URL 只写进 GitHub 变量，未打印明文。" % len(result))
    print("[验证] Healthchecks 集成页点 Test -> 微信应收到「项目签到未上报」并点名到仓库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
