#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日自动更新：扫 ccgp 分类列表页 → 拉详情页 → 拆分字段 → 写进 reports.json。

这是「每天早上八点自动更新」的主更新环节（此前只有哨兵巡检，没有任何任务
真正写台账）。它只做机械动作，判断留在两个显式开关里：

  * 强相关标题（推进 / 推力器 / 阴极 / 贮箱 / 气瓶 / 姿轨控 / 模拟舱）
    才自动入账 —— 这些几乎不可能是噪音。
  * 只命中「真空」这类宽泛词的公告，留在 data/watch_hits.json 里等人工确认，
    绝不自动写进台账。台账是给人看的结论，不能被泛词灌水。

用法：
  python scripts/update_daily.py             # 正常更新（无新增也会写当日条目）
  python scripts/update_daily.py --dry-run   # 只打印将要写入的内容
  python scripts/update_daily.py --page 8    # 每个分类翻几页（默认 6）
退出码：0 正常；2 今日条目已存在（未改动）；1 出错。
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import watchman as w          # 复用 fetch / ccgp_lists / 常量
import write_day as wd        # 复用「全站唯一合法序列化格式」

ROOT = os.path.dirname(HERE)
REPORTS = os.path.join(ROOT, "data", "reports.json")

# 强相关：命中即自动入账。必须比 watchman.MUST_HIT 更严 ——
# 「推进」单独一个词在政务公告里极常见（项目推进中心、整县推进），不能自动入账。
STRONG_HIT = ["电推进", "推力器", "离子推进", "等离子推进", "霍尔", "阴极",
              "贮箱", "气瓶", "姿轨控", "模拟舱"]

# 公告类型 → 跟踪项目的 status
STATUS_BY_CAT = {
    "公开招标": "招标中",
    "中标公告": "已中标",
    "废标终止": "废标/终止",
    "更正公告": "已更正",
    "其他公告": "公告更新",
}

TAG_RE = re.compile(r"<[^>]+>")
PAIR_RE = re.compile(r"<td class='title'[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", re.S)


def text(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s or "")).strip()


def find_winner(body):
    """从公告正文抽中标人。概要表里没有「供应商名称」时才走到这里。"""
    i = body.find("供应商名称")
    if i < 0:
        return ""
    tail = body[i + len("供应商名称"):i + 160]
    tail = re.sub(r"^[:：]\s*", "", tail)
    # 名称后面紧跟的一定是别的字段，遇到就截断（否则会把地址、金额全吞进去）
    for stop in ("供应商地址", "中标（成交）金额", "中标金额", "成交金额",
                 "地　　址", "地址", "四、", "评审", "主要标的信息", "序号"):
        j = tail.find(stop)
        if j > 0:
            tail = tail[:j]
    tail = tail.strip(" 　:：,，。;；、")
    return tail if 2 <= len(tail) <= 60 else ""


def parse_detail(html):
    """从公告详情页抽字段。取不到的一律留空字符串，绝不用猜测填空。"""
    out = {
        "title": "", "buyer": "", "publish_date": "", "amount": "",
        "winner": "", "project_code": "", "deadline": "", "category": "", "region": "",
    }
    m = re.search(r"<h2 class=\"tc\">(.*?)</h2>", html, re.S)
    if m:
        out["title"] = text(m.group(1))
    m = re.search(r'id="pubTime">(.*?)<', html, re.S)
    if m:
        out["publish_date"] = text(m.group(1))

    fields = {}
    for k, v in PAIR_RE.findall(html):
        k, v = text(k), text(v)
        if k and k not in fields:      # 同名键取第一次出现（概要表在前）
            fields[k] = v
    out["buyer"] = fields.get("采购单位", "")
    out["region"] = fields.get("行政区域", "")
    out["amount"] = fields.get("总中标金额") or fields.get("预算金额") or fields.get("最高限价") or ""
    out["winner"] = fields.get("供应商名称", "")

    # 概要表里没有的，从正文里捞。注意项目编号后面常紧跟「（招标文件编号：…）」
    # 或全角括号，正则必须在第一个括号/空白处停下，否则会把括号吞进编号。
    body = text(html)
    m = re.search(r"项目编号[:：]\s*([0-9A-Za-z\-]+)", body)
    if m:
        out["project_code"] = m.group(1).strip("-")
    if not out["winner"]:
        out["winner"] = find_winner(body)
    for key in ("提交投标文件截止时间", "开标时间", "响应文件提交截止时间"):
        m = re.search(key + r"[:：]\s*([0-9年月日时分{:}\- ]{6,30})", body)
        if m:
            out["deadline"] = m.group(1).strip()
            break
    return out


def category_of(cat_label):
    return cat_label.split("-", 1)[-1] if "-" in cat_label else cat_label


def next_free_id(data, taken=None):
    """台账里最大的 P 编号 +1，保持 P## 两位数风格。taken 用于同批多条连续分配。"""
    used = set(taken or ())
    for r in data.get("reports", []):
        for it in r.get("new_items") or []:
            m = re.match(r"P(\d+)$", str(it.get("id", "")))
            if m:
                used.add(int(m.group(1)))
    for p in data.get("site", {}).get("tracked_projects") or []:
        m = re.match(r"P(\d+)$", str(p.get("id", "")))
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return "P%02d" % n


def is_strong(title):
    return any(k in title for k in STRONG_HIT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--page", type=int, default=w.LIST_PAGES)
    ap.add_argument("--replace", action="store_true", help="当日条目已存在时覆盖")
    args = ap.parse_args()

    now = w.today_cn().replace(tzinfo=None)
    day = now.strftime("%Y-%m-%d")

    data = wd.load()
    reports = data.get("reports") or []
    if reports and reports[0].get("date") == day and not args.replace and not args.dry_run:
        print("台账已有 %s 的条目，未改动（要覆盖加 --replace）" % day)
        return 2

    known = w.load_known()
    print("已知公告 %d 条，开始扫 ccgp 分类列表页…" % len(known))

    candidates, lists_ok = w.ccgp_lists(pages=args.page)
    if not lists_ok:
        print("列表页全部不可用，本次不写台账（等下一轮或人工处理）")
        return 1
    print("列表页候选 %d 条" % len(candidates))

    fresh = [c for c in candidates if c.get("url") not in known]
    print("其中台账未见过的 %d 条" % len(fresh))

    strong, weak = [], []
    for c in fresh:
        (strong if is_strong(c["title"]) else weak).append(c)
    print("强相关 %d 条，弱相关（只命中宽泛词）%d 条暂不入账" % (len(strong), len(weak)))

    new_items, new_projects, used_ids = [], [], []
    for c in strong:
        html = w.fetch(c["url"], referer="http://www.ccgp.gov.cn/")
        info = parse_detail(html) if html else {}
        title = info.get("title") or c["title"]
        cat = category_of(c.get("category", ""))
        pid = next_free_id(data, used_ids)
        used_ids.append(int(pid[1:]))
        item = {
            "id": pid,
            "title": title,
            "buyer": info.get("buyer", ""),
            "winner": info.get("winner", ""),
            "amount": info.get("amount", ""),
            "publish_date": c.get("date", ""),
            "url": c["url"],
            "note": "（云端自动收录）%s，来源 ccgp 分类列表页"%cat,
        }
        new_items.append(item)
        if info.get("deadline"):
            milestone = "公告载明的截止/开标时间：%s" % info["deadline"]
        elif cat == "中标公告":
            milestone = "已定标（%s），后续看合同签订与交付" % (info.get("winner") or "中标人见公告")
        elif cat == "废标终止":
            milestone = "已废标/终止，关注是否重新招标"
        else:
            milestone = "详见公告正文"
        new_projects.append({
            "id": pid,
            "name": title,
            "buyer": info.get("buyer", ""),
            "amount": info.get("amount", ""),
            "next_milestone": milestone,
            "status": STATUS_BY_CAT.get(cat, "公告更新"),
            "note": "（云端自动收录）公告编号 %s，公告日 %s。"
                    % (info.get("project_code") or "—", c.get("date", "")),
            "source_url": c["url"],
        })
        time.sleep(2)

    has_new = bool(new_items)
    # 漏跑会留下空白日期：中间断过几天就写清覆盖范围，读者才知道这几天不是
    # "查过且无新增"，而是"根本没跑"——两者含义完全不同。
    prev_day = reports[0].get("date") if reports else None
    gap = ""
    if prev_day:
        try:
            d0 = dt.datetime.strptime(prev_day, "%Y-%m-%d")
            n = (dt.datetime.strptime(day, "%Y-%m-%d") - d0).days
            if n > 1:
                gap = "本次覆盖 %s 至 %s（上一条为 %s，中间 %d 天未运行，属补跑）。" % (
                    prev_day, day, prev_day, n - 1)
        except ValueError:
            pass

    if has_new:
        summary = (gap + "云端自动巡检：新增 %d 条强相关公告（%s）。另有 %d 条只命中宽泛词的公告留在候选池，"
                   "需人工判断是否收录。"
                   % (len(new_items), "、".join(it["title"][:34] for it in new_items),
                      len(weak)))
    else:
        summary = (gap + "云端自动巡检：ccgp 分类列表页共 %d 条公告，其中未见于台账 %d 条，"
                   "无强相关新增；%d 条弱相关候选留在候选池待人工判断。"
                   % (len(candidates), len(fresh), len(weak)))

    payload = {
        "date": day,
        "has_new": has_new,
        "summary": summary,
        "new_items": new_items,
        "projects": [{"id": p["id"], "next_milestone": p["next_milestone"]} for p in new_projects],
    }

    print("---- 将写入 %s ----" % day)
    print(summary)
    for it in new_items:
        print("  %s %s | %s | %s | %s" % (it["id"], it["title"][:44], it["buyer"][:18],
                                          it["amount"], it["publish_date"]))
    if args.dry_run:
        print("(dry-run，未写入)")
        return 0

    # 新增跟踪项目必须直接落盘：write_day.main() 会重新从磁盘读台账，
    # 内存里改 data["site"] 是无效的（2026-10-29 踩过）。
    if new_projects:
        disk = wd.load()
        tp = disk.setdefault("site", {}).setdefault("tracked_projects", [])
        existing = {p.get("id") for p in tp}
        added = []
        for p in new_projects:
            if p["id"] not in existing:
                tp.append(p)
                existing.add(p["id"])
                added.append(p["id"])
        if added:
            wd.dump(disk)
            print("跟踪项目新增：%s（共 %d 个）" % ("、".join(added), len(tp)))

    tmp = os.path.join(ROOT, "data", "_update_day.json")
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False))
    sys.argv = ["write_day.py", "--file", tmp, "--replace"]
    rc = wd.main()
    os.remove(tmp)
    if rc != 0:
        print("write_day 返回 %d，台账未更新" % rc)
        return 1

    print("台账已更新：%s" % day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
