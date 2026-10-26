#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日知识卡生成器（云端 GitHub Actions 与本机共用）。

做三件事：
  1. 读取 data/knowledge_cards.json 词库，按**日期确定性**算出今天该出哪张卡；
  2. 把结果追加/更新到 data/daily_cards.json（含连续更新天数），供页面显示；
  3. 可选校验词库合法性（CI 里跑 --check 作为守门）。

为什么用确定性轮换而不是随机：
  GitHub cron 会延迟甚至漏跑；只要算法只依赖日期，浏览器端能用同一套规则
  自行算出今天的卡，页面永远不会因为云端失联而停在旧内容。

用法：
  python scripts/daily_card.py            # 打印今日卡（不写文件）
  python scripts/daily_card.py --write    # 生成/更新 data/daily_cards.json
  python scripts/daily_card.py --check    # 只校验词库
  python scripts/daily_card.py --date 2026-11-01 --write   # 指定日期（补跑/测试）
"""
import argparse
import datetime as dt
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

TZ = dt.timezone(dt.timedelta(hours=8))  # 北京时间
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CARDS_FILE = os.path.join(ROOT, "data", "knowledge_cards.json")
LOG_FILE = os.path.join(ROOT, "data", "daily_cards.json")
KEEP_DAYS = 120


# ---------------- 轮换算法（与 index.html 中的 JS 实现必须一致） ----------------
def build_order(cards, categories):
    """按类别轮转发牌：相邻两天永远来自不同类别，走完一轮自动接下一轮。

    这样每天换的领域（原理 / 指标 / 电源 / 试验 / 采购 ……）不会扎堆，
    且顺序完全由词库本身决定，Python 与浏览器能算出同一张卡。
    """
    groups = {}
    for c in cards:
        groups.setdefault(c.get("category") or "其他", []).append(c)
    keys = [k for k in (categories or []) if k in groups]
    keys += [k for k in groups if k not in keys]
    order = []
    depth = 0
    while True:
        added = False
        for k in keys:
            if depth < len(groups[k]):
                order.append(groups[k][depth])
                added = True
        if not added:
            break
        depth += 1
    return order


def today_of(tz=TZ):
    return dt.datetime.now(tz).date()


def load_cards():
    with io.open(CARDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def validate(lib):
    errs = []
    cards = lib.get("cards") or []
    if not cards:
        errs.append("cards 为空")
    cats = lib.get("categories") or []
    seen = set()
    for i, c in enumerate(cards):
        for k in ("id", "category", "title", "en", "one_liner", "detail", "watch"):
            if not str(c.get(k) or "").strip():
                errs.append("%s 缺少字段 %s" % (c.get("id") or ("#%d" % i), k))
        if c.get("id") in seen:
            errs.append("重复 id：%s" % c.get("id"))
        seen.add(c.get("id"))
        if cats and c.get("category") not in cats:
            errs.append("%s 的分类 %s 不在 categories 列表里" % (c.get("id"), c.get("category")))
    rot = lib.get("rotation") or {}
    try:
        dt.date.fromisoformat(rot.get("epoch", ""))
    except Exception:
        errs.append("rotation.epoch 必须是 YYYY-MM-DD")
    return errs


def resolve(lib, date_obj):
    cards = lib["cards"]
    rot = lib.get("rotation") or {}
    epoch = dt.date.fromisoformat(rot.get("epoch") or "2026-10-26")
    order = build_order(cards, lib.get("categories") or [])
    n = len(order)
    idx = (date_obj - epoch).days % n
    issue = (date_obj - epoch).days + 1
    return {
        "date": date_obj.isoformat(),
        "issue": issue,
        "index": idx,
        "total": n,
        "card": order[idx],
    }


# ---------------- 历史与连续天数 ----------------
def load_log():
    if not os.path.exists(LOG_FILE):
        return {"updated": "", "streak": 0, "days": []}
    try:
        with io.open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return {"updated": "", "streak": 0, "days": []}
    log.setdefault("days", [])
    return log


def streak_of(days, today):
    """从 today 往前数连续有记录的天数（today 未记录时从昨天起算）。"""
    have = {d["date"] for d in days}
    cur = today if today.isoformat() in have else today - dt.timedelta(days=1)
    n = 0
    while cur.isoformat() in have:
        n += 1
        cur -= dt.timedelta(days=1)
    return n


def write_log(lib, date_obj):
    res = resolve(lib, date_obj)
    log = load_log()
    days = [d for d in log.get("days", []) if isinstance(d, dict) and d.get("date")]
    entry = {
        "date": res["date"],
        "card_id": res["card"]["id"],
        "index": res["index"],
        "issue": res["issue"],
    }
    days = [d for d in days if d["date"] != res["date"]] + [entry]
    days.sort(key=lambda d: d["date"], reverse=True)
    days = days[:KEEP_DAYS]
    out = {
        "updated": res["date"],
        "streak": streak_of(days, date_obj),
        "issue": res["issue"],
        "total": res["total"],
        "days": days,
    }
    with io.open(LOG_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return res, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 data/daily_cards.json")
    ap.add_argument("--check", action="store_true", help="只校验词库")
    ap.add_argument("--date", default="", help="指定日期 YYYY-MM-DD（默认北京时间今天）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出今日卡，便于脚本消费")
    args = ap.parse_args()

    lib = load_cards()
    errs = validate(lib)
    if errs:
        print("词库校验失败：")
        for e in errs:
            print("  - " + e)
        sys.exit(1)
    if args.check:
        print("词库校验通过：%d 张卡 / %d 个分类" % (len(lib["cards"]), len(lib.get("categories") or [])))
        return

    date_obj = dt.date.fromisoformat(args.date) if args.date else today_of()
    res = resolve(lib, date_obj)

    if args.write:
        res, log = write_log(lib, date_obj)
        print("已写入 %s：%s 第 %d 期 → %s《%s》（连续 %d 天）"
              % (os.path.relpath(LOG_FILE, ROOT), res["date"], res["issue"],
                 res["card"]["id"], res["card"]["title"], log["streak"]))

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        c = res["card"]
        print("=" * 60)
        print("%s 第 %d 期 · %s · %s" % (res["date"], res["issue"], c["id"], c["category"]))
        print("%s（%s）" % (c["title"], c["en"]))
        print("· " + c["one_liner"])
        print("")
        print(c["detail"])
        print("")
        print("📍 对采购/投标：" + c["watch"])


if __name__ == "__main__":
    main()
