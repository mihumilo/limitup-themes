#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产出质量体检：对 themes/YYYYMMDD.json 做自动检查，不用人肉看图。

用法：
  python scripts/check_output.py                     # 检查 themes/ 下所有日期
  python scripts/check_output.py 20260918            # 检查指定日期
  python scripts/check_output.py D:/下载/20260918.json   # 检查任意路径的产出

检查项（都是历史上真实踩过的坑）：
  ① 关键词里混入连板列 / 成交额 / 时间 / 纯数字（说明列定位串了）
  ② 关键词被截断成单字或过长
  ③ 连板数清一色都是 1（说明连板列没识别到）
  ④ 时间缺失比例过高
  ⑤ 主题家数与图上标注（declare）偏差过大（说明标题漏识别）
"""
import glob
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
THEMES_DIR = os.path.join(HERE, '..', 'themes')

MAX_STREAK = 9                    # 连板数上限，超过基本是 OCR 竖排粘连
NOISE_KW = re.compile(r'^(\d{1,2}天\d{0,2}板?|\d{1,2}连板?|首板|连板|天数)$')
AMOUNT_KW = re.compile(r'^\d+(\.\d+)?[亿万]$')
TIME_KW = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?$')


def check_one(path):
    try:
        j = json.load(open(path, encoding='utf-8'))
    except Exception as e:
        print('  读取失败 %s: %s' % (path, e))
        return 0
    themes = j.get('themes') or []
    stocks = [s for t in themes for s in t.get('stocks', [])]
    name = os.path.basename(path)
    print('\n=== %s ===' % name)
    if not themes:
        # 兼容早期格式（只有 codes/names，没有 stocks 明细）
        print('  （无 stocks 明细，跳过字段检查）')
        return 0

    problems = 0
    print('  日期 %s｜主题 %d 个｜个股 %d 只｜verified=%s'
          % (j.get('date'), len(themes), len(stocks), j.get('verified')))

    def rep(label, items, sample=3):
        nonlocal problems
        if items:
            problems += len(items)
            print('  [问题] %s：%d 处' % (label, len(items)))
            for x in items[:sample]:
                print('          %s' % (x,))

    # ① 关键词里的杂质
    bad_kw = []
    for s in stocks:
        kw = (s.get('keyword') or '').strip()
        if not kw:
            continue
        for part in re.split(r'[+＋]', kw):
            if NOISE_KW.match(part) or AMOUNT_KW.match(part) or TIME_KW.match(part):
                bad_kw.append('%s %s → keyword=%r' % (s['code'], s.get('name'), kw))
                break
    rep('关键词混入连板/成交额/时间等非关键词', bad_kw)

    # ② 关键词长度异常
    len_bad = ['%s %s → %r' % (s['code'], s.get('name'), s.get('keyword'))
               for s in stocks
               if s.get('keyword') and not (2 <= len(s['keyword']) <= 40)]
    rep('关键词长度异常', len_bad)

    # ③ 连板数清一色 1
    streaks = [s.get('streak', 1) for s in stocks]
    if streaks and sum(1 for x in streaks if x > 1) == 0:
        problems += 1
        print('  [问题] 全部 %d 只连板数都是 1 —— 连板列很可能没解析到' % len(streaks))
    else:
        dist = {}
        for x in streaks:
            dist[x] = dist.get(x, 0) + 1
        print('  连板分布 %s' % json.dumps(dist, ensure_ascii=False))

    # ③b 连板数异常大：连板列是竖排文字，OCR 会把「3」+「3板」粘成 33
    absurd = ['%s %s → %s板' % (s['code'], s.get('name'), s.get('streak'))
              for s in stocks if (s.get('streak') or 0) > MAX_STREAK]
    if absurd:
        problems += 1
        print('  [问题] 连板数异常（> %d 板，多为竖排数字被粘连）：%d 处' % (MAX_STREAK, len(absurd)))
        for a in absurd[:5]:
            print('          %s' % a)
        print('          （看板已由 Worker 用同花顺池值覆盖，但建议重跑该日数据）')

    # ④ 时间缺失
    miss_t = ['%s %s' % (s['code'], s.get('name'))
              for s in stocks if not s.get('time')]
    if miss_t:
        ratio = len(miss_t) / len(stocks)
        if ratio > 0.05:
            rep('时间缺失（占比 %.0f%%）' % (ratio * 100), miss_t)
        else:
            print('  时间缺失 %d 只（占比 %.0f%%，可接受，看板会用涨停池兜底）'
                  % (len(miss_t), ratio * 100))

    # ⑤ 主题家数与图注偏差
    off = []
    for t in themes:
        d, c = t.get('declare') or 0, t.get('count') or len(t.get('stocks', []))
        if d and abs(c - d) > 2:
            off.append('%s 标注 %d 实得 %d' % (t['name'], d, c))
    rep('主题家数与图上标注偏差过大（标题可能漏识别）', off)

    print('  → %s' % ('未发现问题 ✓' if problems == 0 else '共 %d 处待修' % problems))
    return problems


def main():
    args = sys.argv[1:]
    paths = []
    if not args:
        paths = sorted(glob.glob(os.path.join(THEMES_DIR, '*.json')))
    for a in args:
        if os.path.exists(a):
            paths.append(a)
        else:
            p = os.path.join(THEMES_DIR, '%s.json' % a.replace('-', ''))
            paths.append(p if os.path.exists(p) else a)
    if not paths:
        print('没有找到可检查的产出文件')
        return 1
    total = 0
    for p in paths:
        total += check_one(p)
    print('\n合计问题：%d' % total)
    # ★ 体检只是诊断：发现问题只打印，**退出码恒为 0**。
    #   以前是「有问题就 exit 2」，会把 Actions 作业判红 —— 但数据本身可能是好的
    #   （比如它报的"关键词长度异常"只是关键词偏长，不影响使用）。
    #   数据到底行不行，由 pipeline.py 的 verified 决定，不由体检决定。
    return 0


if __name__ == '__main__':
    sys.exit(main())
