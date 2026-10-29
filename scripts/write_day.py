#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一天的日报写进 data/reports.json（云端 GitHub Actions 与本机 DSH 共用）。

为什么要有这个脚本：
  每日更新真正容易出错的不是"找到公告"，而是**写台账的格式**——
  reports.json 必须保证 CRLF 换行、indent=2、末尾无换行，否则每次提交
  都会产生整文件 diff；upcoming 要过滤掉已过去的日期，constraints 要继承。
  这些机械动作交给脚本，调用方只管提供"今天发现了什么"。

用法（喂 JSON）：
  python scripts/write_day.py --stdin < day.json
  python scripts/write_day.py --file day.json

输入 JSON：
{
  "date": "2026-10-30",              // 可省略，默认北京时间今天
  "has_new": true,
  "summary": "……",                    // 当日概述，写清窗口与结论
  "new_items": [                      // 没有新公告就给空数组
    {"id":"P22","title":"…","buyer":"…","winner":"…","amount":"…",
     "publish_date":"2026-10-30","url":"https://…","note":"…"}
  ],
  "upcoming": [                       // 可省略：省略时继承昨日并自动剔除过期项
    {"date":"2026-11-04","event":"P20 开标","detail":"…"}
  ],
  "constraints": "…",                 // 可省略：继承昨日
  "projects": [                       // 可选：更新跟踪项目的 next_milestone
    {"id":"P22","next_milestone":"…"}
  ]
}

退出码：0 成功；2 已存在同日条目（未改动，除非 --replace）；1 出错。
"""
import argparse
import datetime as dt
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
# Windows 下 stdin 默认是 GBK，直接 read() 会把 UTF-8 中文解成乱码（表现为
# "can't encode character '\udcXX'"）。这里强制按 UTF-8 解码。
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
except Exception:  # noqa
    pass

TZ = dt.timezone(dt.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS = os.path.join(ROOT, "data", "reports.json")
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def load():
    return json.load(io.open(REPORTS, encoding="utf-8"))


def dump(data):
    """全站唯一合法序列化格式：indent=2 + CRLF + 末尾无换行"""
    raw = json.dumps(data, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    io.open(REPORTS, "w", encoding="utf-8", newline="").write(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="从文件读输入 JSON")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读输入 JSON")
    ap.add_argument("--date", help="指定日期（补跑用）")
    ap.add_argument("--replace", action="store_true", help="同日条目已存在时覆盖")
    args = ap.parse_args()

    if args.stdin or not args.file:
        payload = json.loads(sys.stdin.read())
    else:
        payload = json.load(io.open(args.file, encoding="utf-8"))

    now = dt.datetime.now(TZ)
    day = args.date or payload.get("date") or now.strftime("%Y-%m-%d")
    try:
        day_dt = dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print("日期格式非法：%s（需要 YYYY-MM-DD）" % day)
        return 1

    data = load()
    reports = data.get("reports") or []
    if not reports:
        print("台账为空，拒绝写入（先确认 reports.json 是否被破坏）")
        return 1

    prev = reports[0]
    idx = next((i for i, r in enumerate(reports) if r.get("date") == day), None)
    if idx is not None and not args.replace:
        print("已存在 %s 的条目（第 %d 条），未改动。要覆盖请加 --replace" % (day, idx + 1))
        return 2

    new_items = payload.get("new_items") or []
    has_new = bool(payload.get("has_new", bool(new_items)))
    if has_new and not new_items:
        print("has_new=true 但 new_items 为空，拒绝写入（避免台账出现有新增却无内容的条目）")
        return 1

    # upcoming：显式给了就用给的，否则继承昨日并剔除已过期项
    if "upcoming" in payload:
        upcoming = payload["upcoming"]
    else:
        upcoming = [u for u in (prev.get("upcoming") or []) if u.get("date", "") >= day]

    entry = {
        "date": day,
        "weekday": WEEKDAY_CN[day_dt.weekday()],
        "has_new": has_new,
        "summary": payload.get("summary") or "",
        "new_items": new_items,
        "upcoming": upcoming,
        "constraints": payload.get("constraints", prev.get("constraints", "")),
    }

    if idx is not None:
        reports[idx] = entry
        print("已覆盖 %s 的条目" % day)
    else:
        # 严格按日期倒序插入，补跑旧日期也不会打乱顺序
        pos = 0
        while pos < len(reports) and reports[pos]["date"] > day:
            pos += 1
        reports.insert(pos, entry)
        print("已插入 %s 的条目（第 %d 条）" % (day, pos + 1))

    # 可选：同步更新跟踪项目的下一个关键节点
    projects = payload.get("projects") or []
    if projects:
        by_id = {p.get("id"): p for p in data.get("site", {}).get("tracked_projects") or []}
        for upd in projects:
            pid = upd.get("id")
            if pid in by_id and "next_milestone" in upd:
                by_id[pid]["next_milestone"] = upd["next_milestone"]
                print("   更新 %s 的 next_milestone" % pid)

    data["reports"] = reports
    dump(data)

    # 自检：写出去的东西必须还能读回来且 CRLF 一致
    raw = io.open(REPORTS, encoding="utf-8", newline="").read()
    assert json.loads(raw), "写出后无法解析"
    assert raw.count("\n") == raw.count("\r\n"), "CRLF 不一致"
    print("reports.json 现有 %d 期，最新 %s" % (len(reports), reports[0]["date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
