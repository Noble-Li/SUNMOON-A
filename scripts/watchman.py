#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
云端哨兵（GitHub Actions 版）

职责：
  1. 抓取公开渠道的推进类采购公告线索，与台账已知条目比对，输出新增线索到 data/watch_hits.json
  2. 检测日报是否断更（最新条目日期 vs 今天），断更则告警
  3. 需要提醒时，生成 issue 标题/正文文件，交给 workflow 开 Issue

设计原则：
  - 任何网络异常都不允许让任务失败，降级后仍要完成断更检测
  - 抓不到不等于没问题，断更告警是保底能力（100% 可靠，因为只读仓库自身数据）
"""

import io
import json
import os
import re
import sys
import time
import datetime
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(DATA, "reports.json")
HITS_FILE = os.path.join(DATA, "watch_hits.json")
OUT_DIR = os.path.join(ROOT, ".watch_out")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 检索关键词（ccgp 搜索接口）
KEYWORDS = [
    "霍尔电推进",
    "霍尔推力器",
    "电推进",
    "空心阴极",
    "推进子系统",
    "空间环境模拟",
]

# 标题必须命中其一，否则丢弃（去噪）
MUST_HIT = ["推进", "推力器", "阴极", "真空", "贮箱", "气瓶", "模拟舱", "姿轨控", "姿轨控分系统"]

# 明显无关的直接丢
BLACKLIST = ["霍尔效应", "霍尔传感器", "霍尔元件", "SimpleFOC", "汽车", "电梯", "空调"]

STALE_DAYS = 2          # 落后多少天判定为断更
LOOKBACK_DAYS = 10      # ccgp 搜索的时间窗口


def log(*a):
    print("[watchman]", *a, flush=True)


def today_cn():
    # GitHub Actions runner 为 UTC，这里显式换算到北京时间
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def fetch(url, timeout=30, referer=None, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
            if referer:
                req.add_header("Referer", referer)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(3)
    log("fetch failed:", url[:70], "->", repr(last)[:120])
    return None


# ---------------- 源 1：中国政府采购网搜索 ----------------
def ccgp_search(kw, start, end):
    params = {
        "searchtype": "1",
        "page_index": "1",
        "bidSort": "0",
        "buyerName": "",
        "projectId": "",
        "pinMu": "0",
        "bidType": "0",
        "dbselect": "bidx",
        "kw": kw,
        "start_time": start,
        "end_time": end,
        "timeType": "6",
        "displayZone": "",
        "zoneId": "",
        "pppStatus": "0",
        "agentName": "",
    }
    url = "http://search.ccgp.gov.cn/bxsearch?" + urllib.parse.urlencode(params)
    return fetch(url, referer="http://search.ccgp.gov.cn/")


def parse_ccgp(html):
    """返回 (items, blocked)；blocked=True 表示被限流"""
    if html is None:
        return [], True
    if "频繁访问" in html or "访问过于频繁" in html:
        return [], True
    items = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 8 or "ccgp.gov.cn" not in href:
            continue
        if "/cggg/" not in href and "/cggg" not in href:
            continue
        tail = html[m.end(): m.end() + 600]
        dm = re.search(r"(20\d{2})[-./年](\d{1,2})[-./月](\d{1,2})", tail)
        date = ""
        if dm:
            date = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))
        items.append({"title": title, "url": href, "date": date})
    return items, False


# ---------------- 源 2：Bing RSS（降级备用） ----------------
def bing_rss(kw):
    url = "https://www.bing.com/search?format=rss&count=20&q=" + urllib.parse.quote(kw + " 招标")
    xml = fetch(url)
    if not xml:
        return []
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        blk = m.group(1)
        t = re.search(r"<title>(.*?)</title>", blk, re.S)
        l = re.search(r"<link>(.*?)</link>", blk, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", blk, re.S)
        if not (t and l):
            continue
        title = re.sub(r"\s+", " ", t.group(1)).strip()
        items.append({
            "title": title,
            "url": l.group(1).strip(),
            "date": (d.group(1)[:16] if d else ""),
        })
    return items


# ---------------- 过滤 ----------------
def relevant(title):
    if any(b in title for b in BLACKLIST):
        return False
    return any(k in title for k in MUST_HIT)


def load_known():
    known = set()
    try:
        d = json.load(io.open(REPORTS, encoding="utf-8"))
        for r in d.get("reports", []):
            for it in r.get("new_items", []) or []:
                if it.get("url"):
                    known.add(it["url"].strip())
        for p in d.get("site", {}).get("tracked_projects", []) or []:
            if p.get("source_url"):
                known.add(p["source_url"].strip())
    except Exception as e:  # noqa
        log("load reports.json failed:", repr(e)[:120])
    return known


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = today_cn()
    today = now.strftime("%Y-%m-%d")

    # --- 1. 断更检测（保底，绝不失败） ---
    latest = None
    stale_days = -1
    try:
        d = json.load(io.open(REPORTS, encoding="utf-8"))
        latest = d["reports"][0]["date"]
        ld = datetime.datetime.strptime(latest, "%Y-%m-%d")
        stale_days = (datetime.datetime.strptime(today, "%Y-%m-%d") - ld).days
    except Exception as e:  # noqa
        log("stale check failed:", repr(e)[:120])
    log("最新条目:", latest, "| 落后天数:", stale_days)

    # --- 2. 抓取 ---
    start = (now - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y:%m:%d")
    end = now.strftime("%Y:%m:%d")
    raw_items = []
    sources = []
    blocked = True

    for kw in KEYWORDS:
        html = ccgp_search(kw, start, end)
        got, is_blocked = parse_ccgp(html)
        if not is_blocked:
            blocked = False
            if kw not in sources:
                sources.append("ccgp")
        for it in got:
            it["keyword"] = kw
            it["source"] = "ccgp"
        raw_items.extend(got)
        log("ccgp[%s] -> %d 条%s" % (kw, len(got), "（被限流）" if is_blocked else ""))
        time.sleep(6)

    if blocked:
        log("ccgp 全部被限流，启用 Bing RSS 备用")
        for kw in KEYWORDS[:4]:
            got = bing_rss(kw)
            for it in got:
                it["keyword"] = kw
                it["source"] = "bing"
            raw_items.extend(got)
            log("bing[%s] -> %d 条" % (kw, len(got)))
            time.sleep(2)

    # --- 3. 去重 + 过滤 ---
    known = load_known()
    hits_db = {"updated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "hits": []}
    if os.path.exists(HITS_FILE):
        try:
            hits_db = json.load(io.open(HITS_FILE, encoding="utf-8"))
            hits_db.setdefault("hits", [])
        except Exception:  # noqa
            pass
    seen = set(h["url"] for h in hits_db["hits"]) | known

    added = []
    for it in raw_items:
        u = it["url"].strip()
        if not u or u in seen:
            continue
        if not relevant(it["title"]):
            continue
        seen.add(u)
        rec = {
            "found_at": today,
            "title": it["title"],
            "url": u,
            "date": it.get("date", ""),
            "keyword": it.get("keyword", ""),
            "source": it.get("source", ""),
            "status": "pending",
        }
        hits_db["hits"].append(rec)
        added.append(rec)

    hits_db["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    hits_db["last_run"] = {
        "date": today,
        "sources": sources or (["bing"] if raw_items else []),
        "ccgp_blocked": blocked,
        "raw": len(raw_items),
        "added": len(added),
        "stale_days": stale_days,
    }
    json.dump(hits_db, io.open(HITS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    log("抓取原始 %d 条，新增线索 %d 条" % (len(raw_items), len(added)))

    # --- 4. 是否需要提醒 ---
    need = False
    lines = []
    if stale_days >= STALE_DAYS:
        need = True
        lines.append("## ⚠️ 日报已断更\n")
        lines.append("- 台账最新条目：**%s**" % (latest or "未知"))
        lines.append("- 距今 **%d 天**未更新（阈值 %d 天）" % (stale_days, STALE_DAYS))
        lines.append("- 常见原因：WorkBuddy 客户端未运行，本地定时任务未触发。")
        lines.append("\n请打开客户端，或在有客户端时直接说「补更日报」，会自动补上缺失窗口。\n")
    if added:
        need = True
        lines.append("## 🔔 哨兵发现 %d 条新线索（待核实）\n" % len(added))
        lines.append("| 公告日期 | 标题 | 命中词 | 来源 |")
        lines.append("|---|---|---|---|")
        for r in added:
            lines.append("| %s | [%s](%s) | %s | %s |" % (
                r["date"] or "—",
                r["title"].replace("|", "／")[:70],
                r["url"], r["keyword"], r["source"]))
        lines.append("\n核实后请更新 `data/reports.json` 并发布；已核实的线索把 `status` 改为 `done`。\n")
    if blocked and not added:
        lines.append("> 本次运行 ccgp 搜索接口被限流，未获取到可靠结果。")

    title = None
    if need:
        if stale_days >= STALE_DAYS and added:
            title = "[哨兵] %s 日报断更 %d 天 + %d 条新线索" % (today, stale_days, len(added))
        elif stale_days >= STALE_DAYS:
            title = "[哨兵] %s 日报已 %d 天未更新" % (today, stale_days)
        else:
            title = "[哨兵] %s 发现 %d 条新线索待核实" % (today, len(added))

    io.open(os.path.join(OUT_DIR, "need_alert.txt"), "w", encoding="utf-8").write(
        "true" if need else "false")
    io.open(os.path.join(OUT_DIR, "issue_title.txt"), "w", encoding="utf-8").write(title or "")
    io.open(os.path.join(OUT_DIR, "issue_body.md"), "w", encoding="utf-8").write(
        "\n".join(lines))

    # GitHub Actions 输出
    gout = os.environ.get("GITHUB_OUTPUT")
    if gout:
        with io.open(gout, "a", encoding="utf-8") as f:
            f.write("need_alert=%s\n" % ("true" if need else "false"))
            f.write("added=%d\n" % len(added))
            f.write("stale_days=%d\n" % stale_days)
            f.write("ccgp_blocked=%s\n" % ("true" if blocked else "false"))

    log("need_alert =", need)
    return 0


if __name__ == "__main__":
    sys.exit(main())
