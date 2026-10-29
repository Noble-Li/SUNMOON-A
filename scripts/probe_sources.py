#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通道探测：GitHub Actions 的出口 IP 能不能直连政采网？

这决定了「早八点自动更新」该由云端跑还是必须由本机跑。
逐条打印 ::notice:: / ::warning:: 注解，结论一眼可读。

用法：python scripts/probe_sources.py
"""
import re
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DETAIL_RE = re.compile(r"/t\d{8}_\d+\.htm")
BLOCK_MARKERS = ("频繁", "访问过于", "验证码", "拒绝访问")


def get(url, ua=UA_DESKTOP, referer=None, timeout=40):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa
        return 0, repr(e).encode("utf-8", "replace")


def decode(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def note(name, status, raw):
    body = decode(raw)
    blocked = any(m in body for m in BLOCK_MARKERS)
    links = len(DETAIL_RE.findall(body))
    level = "warning" if (blocked or status == 0) else "notice"
    print("::%s::%s status=%s len=%d blocked=%s detail_links=%d"
          % (level, name, status, len(raw), blocked, links))
    return blocked, links


def ccgp_url(kw, start, end):
    params = {
        "searchtype": "1", "page_index": "1", "bidSort": "0", "buyerName": "",
        "projectId": "", "pinMu": "0", "bidType": "0", "dbselect": "bidx",
        "kw": kw, "start_time": start, "end_time": end, "timeType": "6",
        "displayZone": "", "zoneId": "", "pppStatus": "0", "agentName": "",
    }
    return "http://search.ccgp.gov.cn/bxsearch?" + urllib.parse.urlencode(params)


def main():
    url = ccgp_url("电推进", "2026-10-18", "2026-10-29")

    print("### 1) ccgp 搜索 · 桌面 UA")
    note("ccgp-desktop-UA", *get(url))

    print("### 2) ccgp 搜索 · Python UA")
    note("ccgp-urllib-UA", *get(url, ua="Python-urllib/3.11"))

    print("### 3) ccgp 搜索 · 带 Referer")
    note("ccgp-with-referer", *get(url, referer="http://search.ccgp.gov.cn/"))

    print("### 4) ccgp 站根")
    st, raw = get("http://search.ccgp.gov.cn/", referer="http://www.ccgp.gov.cn/")
    print("::notice::ccgp-root status=%s len=%d" % (st, len(raw)))

    print("### 5) ccgp 公告列表页（不依赖搜索接口）")
    note("ccgp-list", *get("https://www.ccgp.gov.cn/cggg/zygg/fblbgg/",
                           referer="https://www.ccgp.gov.cn/"))

    print("### 6) Bing")
    st, raw = get("https://cn.bing.com/search?q=" + urllib.parse.quote("电推进 招标 公告"))
    body = decode(raw)
    print("::notice::bing status=%s len=%d ccgp_links=%d"
          % (st, len(raw), body.count("ccgp")))

    print("### 7) DuckDuckGo html")
    st, raw = get("https://html.duckduckgo.com/html/?q="
                  + urllib.parse.quote("电推进 招标"))
    print("::notice::ddg status=%s len=%d" % (st, len(raw)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
