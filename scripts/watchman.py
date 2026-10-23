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

import base64
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

# 采购信号词：标题必须命中其一，否则视为科普/词典噪音
PROCURE_HINT = [
    "招标", "中标", "采购", "成交", "公告", "公示", "询价", "磋商", "谈判",
    "废标", "终止", "候选", "开标", "邀标", "竞标", "单一来源", "竞争性",
    "结果公告", "采购意向", "需求公示", "更正公告", "澄清",
]

# 词典 / 科普 / 内容农场噪音
NOISE_HINT = [
    "词典", "意思", "拼音", "造句", "翻译", "近义词", "反义词", "例句",
    "怎么读", "怎么说", "是什么", "词语", "汉语", "国学", "百科", "知道",
    "文库", "下载", "论文", "专利", "招聘", "论坛", "博客", "视频",
    "工作原理", "简介", "浅谈", "综述", "发展现状", "百度",
]

STALE_DAYS = 2          # 落后多少天判定为断更
LOOKBACK_DAYS = 10      # ccgp 搜索的时间窗口
RECENT_DAYS = 45        # 公告日期距今超过这个天数就丢弃（避免翻出陈年旧帖）

# 这些域名下的链接本身就是采购公告，可豁免「必须带采购信号词」的限制
TRUSTED_DOMAINS = [
    "ccgp.gov.cn", "cebpubservice.com", "chinabidding", "weain",
    "caigou", "bidding", "ztb",
]


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


# ---------------- 源 3：搜索引擎网页版（海外 IP 可用） ----------------
# Actions runner 在境外，DuckDuckGo / Bing 网页版可直连（本机国内网络反而不通）。
# 用它们做「广撒网 + 领域词过滤」，弥补 ccgp 接口限流。
SE_QUERIES = [
    "site:ccgp.gov.cn 电推进 招标",
    "site:ccgp.gov.cn 霍尔 推进",
    "site:ccgp.gov.cn 推进子系统 中标",
    "电推进 中标公告 采购 卫星",
    "霍尔推力器 采购 招标",
    "空心阴极 中和器 采购 招标",
    "空间环境模拟 真空舱 招标 采购",
    "姿轨控 推进分系统 招标 中标",
]

# 明显不是采购公告的站点，直接丢
SE_BLACK_DOMAINS = [
    "zhihu.com", "baike.baidu", "baidu.com", "wikipedia", "bilibili",
    "csdn.net", "cnblogs", "jianshu", "weibo", "douban", "toutiao",
    "taobao", "jd.com", "zhidao", "sogou", "so.com",
]


def _clean(t):
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"&[a-zA-Z#0-9]+;", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def ddg_html(q):
    """DuckDuckGo HTML 版（境外可达）。POST 表单比 GET 稳。"""
    data = urllib.parse.urlencode({"q": q, "kl": "cn-zh"}).encode()
    try:
        req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data)
        req.add_header("User-Agent", UA)
        req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        html = raw.decode("utf-8", "replace")
    except Exception as e:  # noqa
        log("ddg failed:", repr(e)[:100])
        return []
    items = []
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), _clean(m.group(2))
        # DDG 的链接是跳转地址，真实 URL 藏在 uddg 参数里
        if "uddg=" in href:
            href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
        if not href.startswith("http") or len(title) < 8:
            continue
        items.append({"title": title, "url": href, "date": "", "source": "ddg", "query": q})
    return items


def _decode_bing(href):
    """Bing 的 /ck/a? 跳转链接，真实地址 base64 藏在 u= 参数里（前缀 a1）。"""
    if "/ck/a" not in href:
        return href
    m = re.search(r"[?&]u=([^&]+)", href)
    if not m:
        return href
    raw = urllib.parse.unquote(m.group(1))
    try:
        dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa
        return href
    if dec.startswith("a1"):
        dec = dec[2:]
    return dec if dec.startswith("http") else href


def bing_html(q):
    """Bing 网页版（ensearch=1 走国际索引，对境外 runner 更稳）。"""
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
           + "&setlang=zh-CN&ensearch=1&count=20")
    html = fetch(url, referer="https://www.bing.com/")
    if not html:
        return []
    items = []
    for m in re.finditer(r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>', html, re.S):
        href, title = _decode_bing(m.group(1).replace("&amp;", "&")), _clean(m.group(2))
        if not href.startswith("http") or len(title) < 8:
            continue
        items.append({"title": title, "url": href, "date": "", "source": "bing", "query": q})
    return items


def se_black(url):
    low = url.lower()
    return any(d in low for d in SE_BLACK_DOMAINS)


def search_engine_fallback():
    """ccgp 被限流时的兜底检索：先 DDG，再 Bing。"""
    items = []
    tried = []
    for fn, name in ((ddg_html, "ddg"), (bing_html, "bing")):
        got = fn(SE_QUERIES[0])
        if got:
            tried.append(name)
            break
    if not tried:
        log("搜索引擎全部不可用")
        return [], []
    use = ddg_html if tried[0] == "ddg" else bing_html
    for q in SE_QUERIES:
        got = use(q)
        got = [g for g in got if not se_black(g["url"])]
        items.extend(got)
        log("%s[%s] -> %d 条" % (tried[0], q[:22], len(got)))
        time.sleep(2)
    return items, tried


# ---------------- 过滤 ----------------
def url_date(u):
    """政采网公告 URL 形如 /t20260919_27360832.htm，可直接取日期。"""
    m = re.search(r"/t(20\d{6})_", u or "")
    if not m:
        return None
    s = m.group(1)
    try:
        return datetime.datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def relevant(title, url="", now=None):
    if any(b in title for b in BLACKLIST):
        return False
    if any(n in title for n in NOISE_HINT):
        return False
    if not any(k in title for k in MUST_HIT):
        return False

    if url:
        low = url.lower()
        if "bing.com" in low or "microsoft" in low or "duckduckgo" in low:
            return False
        if se_black(url):
            return False
        # 只保留看起来像公告详情页的链接
        if not re.search(r"(cggg|/t\d{8}_|\.shtml|\?.*id=|notice|detail|zbgg|gkzb|fblbgg)", low):
            return False

        trusted = any(d in low for d in TRUSTED_DOMAINS)

        # 太旧的公告直接丢（标题常被搜索引擎截断，日期比标题可靠）
        if now is not None:
            d = url_date(url)
            if d is not None and (now - d).days > RECENT_DAYS:
                return False

        # 非可信域名必须带采购信号词，否则多半是科普/词典页
        if not trusted and not any(p in title for p in PROCURE_HINT):
            return False
    else:
        if not any(p in title for p in PROCURE_HINT):
            return False
    return True


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

    # 先探一次，被限流就立刻止损（不空跑 6 次各 6 秒）
    probe, probe_blocked = parse_ccgp(ccgp_search(KEYWORDS[0], start, end))
    if probe_blocked or not probe:
        log("ccgp 不可用（限流/无结果），跳过，直接走搜索引擎通道")
    else:
        blocked = False
        sources.append("ccgp")
        raw_items.extend(probe)
        log("ccgp[探] -> %d 条" % len(probe))
        for kw in KEYWORDS[1:]:
            got, is_blocked = parse_ccgp(ccgp_search(kw, start, end))
            if is_blocked:
                break
            for it in got:
                it["keyword"] = kw
                it["source"] = "ccgp"
            raw_items.extend(got)
            log("ccgp[%s] -> %d 条" % (kw, len(got)))
            time.sleep(6)

    if blocked:
        se_items, se_src = search_engine_fallback()
        sources.extend(se_src)
        for it in se_items:
            it.setdefault("keyword", it.get("query", ""))
        raw_items.extend(se_items)

    # 原始结果全量落盘 + 摘要打印，方便在 Actions 日志里核对抓取源到底返回了什么
    try:
        json.dump(raw_items, io.open(os.path.join(OUT_DIR, "raw_dump.json"), "w",
                                     encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:  # noqa
        pass
    if os.environ.get("WATCHMAN_DEBUG") == "1":
        log("--- 原始结果 %d 条 ---" % len(raw_items))
        for it in raw_items:
            log("   [%s] %s | %s" % ("保留" if relevant(it["title"], it["url"], now)
                                     else "丢弃", it["title"][:52], it["url"][:72]))

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
        if not relevant(it["title"], u, now):
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
    if not added:
        if blocked and sources:
            lines.append("> 说明：ccgp 接口被限流，本次走的是 **%s** 搜索引擎通道，结果未经人工核实。"
                         % " / ".join(sources))
        elif blocked:
            lines.append("> 说明：ccgp 接口被限流且搜索引擎通道不可用，本次未获取到任何线索。")

    # 手动验证通知链路用（workflow_dispatch 的 force_alert）
    if os.environ.get("FORCE_ALERT") == "true" and not need:
        need = True
        lines.append("## ✅ 通知链路测试\n")
        lines.append("这是一条**手动触发的测试提醒**，用来确认提醒能正常送达。")
        lines.append("当前日报状态正常（最新条目 %s，落后 %s 天），无需处理。\n"
                     % (latest or "未知", stale_days))

    title = None
    if need:
        if stale_days >= STALE_DAYS and added:
            title = "[哨兵] %s 日报断更 %d 天 + %d 条新线索" % (today, stale_days, len(added))
        elif stale_days >= STALE_DAYS:
            title = "[哨兵] %s 日报已 %d 天未更新" % (today, stale_days)
        elif added:
            title = "[哨兵] %s 发现 %d 条新线索待核实" % (today, len(added))
        else:
            title = "[哨兵] %s 通知链路测试（状态正常）" % today

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
