#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通道探测：GitHub Actions 的出口 IP 能不能读到政采网的「列表页」？

背景：ccgp 的搜索接口（search.ccgp.gov.cn/bxsearch）对本机和 Actions 都返回
2920 字节的限流页，不可用。但站点的分类列表页是静态 HTML，可能可用。

本机实测：http:// 协议 200 可用；https:// 协议 403 Forbidden。
所以在 Actions 上必须逐个验证协议与分类页。

用法：python scripts/probe_sources.py
"""
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DETAIL_RE = re.compile(r'href="(\./20\d{4}/t\d{8}_\d+\.htm)"[^>]*title="([^"]{4,90})"')
BLOCK_MARKERS = ("频繁", "访问过于", "验证码", "拒绝访问")


def get(url, referer=None, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
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


def report(name, status, raw, expect_links=True):
    body = decode(raw)
    blocked = any(m in body for m in BLOCK_MARKERS)
    links = len(DETAIL_RE.findall(body))
    ok = (status == 200) and not blocked and (links > 0 or not expect_links)
    level = "notice" if ok else "warning"
    print("::%s::%s status=%s len=%d blocked=%s links=%d"
          % (level, name, status, len(raw), blocked, links))
    return ok


CATEGORIES = [
    ("中央-公开招标", "http://www.ccgp.gov.cn/cggg/zygg/gkzb/"),
    ("中央-中标公告", "http://www.ccgp.gov.cn/cggg/zygg/zbgg/"),
    ("中央-废标终止", "http://www.ccgp.gov.cn/cggg/zygg/fblbgg/"),
    ("地方-公开招标", "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/"),
    ("地方-中标公告", "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/"),
]


def main():
    print("### A) http 列表页（本机可用，此处验证 Actions 出口）")
    reachable = 0
    for name, url in CATEGORIES:
        st, raw = get(url, referer="http://www.ccgp.gov.cn/")
        if report(name, st, raw):
            reachable += 1

    print("### B) https 列表页（本机 403，确认是否协议相关）")
    st, raw = get("https://www.ccgp.gov.cn/cggg/zygg/gkzb/",
                  referer="https://www.ccgp.gov.cn/")
    report("https-列表页", st, raw)

    print("### C) 翻页 index_1.htm")
    st, raw = get("http://www.ccgp.gov.cn/cggg/zygg/gkzb/index_1.htm",
                  referer="http://www.ccgp.gov.cn/")
    report("http-第2页", st, raw)

    print("### D) 详情页")
    # 用列表页里真实出现的详情链接测一条，避免写到固定 id
    st, raw = get("http://www.ccgp.gov.cn/cggg/zygg/gkzb/",
                  referer="http://www.ccgp.gov.cn/")
    body = decode(raw)
    m = DETAIL_RE.search(body)
    if not m:
        print("::warning::详情页探测跳过：列表页里没解析出详情链接")
    else:
        rel = m.group(1).replace("./", "/cggg/zygg/gkzb/")
        detail = "http://www.ccgp.gov.cn" + rel
        st, draw = get(detail, referer="http://www.ccgp.gov.cn/")
        dtext = decode(draw)
        print("::%s::详情页 status=%s len=%d title_length=%d"
              % ("notice" if st == 200 and len(draw) > 3000 else "warning",
                 st, len(draw), len(m.group(2))))

    print("")
    print("::notice::列表页可达分类数 = %d / %d" % (reachable, len(CATEGORIES)))
    return 0 if reachable else 1


if __name__ == "__main__":
    sys.exit(main())
