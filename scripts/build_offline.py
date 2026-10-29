#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 index.html + data/*.json 生成「离线单文件版」。

为什么需要它：离线单文件版（三个 JSON 全部内联、双击可用）必须和当日数据
同步，否则转发出去的那份是旧的。本机原先由 deploy_report.py 生成，但那个
脚本在工作区上层、不在仓库里，云端 Actions 拿不到，所以这里放一份仓库内的
实现，让 08:00 的自动更新跑完就能顺手重建离线版。

用法：python scripts/build_offline.py [--check]
  --check  只比较是否需要重建，不写文件（退出码 1 表示已过期）
"""
import argparse
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "index.html")
OFFLINE = os.path.join(ROOT, "霍尔电推进日报_离线单文件版.html")

# 顺序即内联顺序，与页面里的读取顺序一致
INLINE_TARGETS = (
    ("offline-data", os.path.join(ROOT, "data", "reports.json")),
    ("offline-cards", os.path.join(ROOT, "data", "knowledge_cards.json")),
    ("offline-cardlog", os.path.join(ROOT, "data", "daily_cards.json")),
)


def build():
    """返回 (html, blocks)；无法生成时返回 (None, 原因)"""
    html = io.open(INDEX, encoding="utf-8").read()
    if 'id="offline-data"' in html:
        return None, "index.html 里已含内联数据（这是离线版本身，不是模板）"
    if html.count("<script>") != 1:
        return None, "<script> 标签数量异常（%d），拒绝内联" % html.count("<script>")

    blocks = []
    for dom_id, path in INLINE_TARGETS:
        if not os.path.exists(path):
            continue
        raw = io.open(path, encoding="utf-8", newline="").read()
        json.loads(raw)  # 必须是合法 JSON，否则页面白屏
        blocks.append('<script id="%s" type="application/json">%s</script>' % (dom_id, raw))
    if not blocks:
        return None, "没有任何可内联的数据"

    return html.replace("<script>", "\n".join(blocks) + "\n<script>", 1), len(blocks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否需要重建")
    args = ap.parse_args()

    html, extra = build()
    if html is None:
        print("跳过离线版：%s" % extra)
        return 0

    old = ""
    if os.path.exists(OFFLINE):
        old = io.open(OFFLINE, encoding="utf-8", newline="").read()
    changed = old != html

    if args.check:
        print("离线版%s（内联 %d 个数据块）" % ("已过期，需要重建" if changed else "是最新的", extra))
        return 1 if changed else 0

    if not changed:
        print("离线单文件版无变化（内联 %d 个数据块）" % extra)
        return 0

    io.open(OFFLINE, "w", encoding="utf-8", newline="").write(html)
    print("离线单文件版已更新：%.1f KB（内联 %d 个数据块）"
          % (len(html.encode("utf-8")) / 1024.0, extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
