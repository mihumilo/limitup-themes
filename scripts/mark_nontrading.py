#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「两次都没拉取到数据」的日期记为**非交易日** —— 按约定，非交易日不需要数据。

规则（见 README「运行机制」）：
    每个交易日跑两次（北京 16:30 / 20:00）。
    两次都没拿到官方复盘帖 → 判定当天为非交易日 → 不需要数据，不发告警。

本脚本做三件事：
  1. 把日期追加进仓库根目录 `nontrading-days.json`（去重、排序）
  2. 清掉当天残留的**未通过校验**产物（themes/<date>.json 的 verified != true、
     以及对应的 review/<date>.json），避免仓库里留下半成品
  3. 打印一行摘要，写进 Actions 日志

注意：如果**交易日历已确认当天是交易日**却仍然两次失败，那是真故障，
      由 daily.yml 的告警步骤负责开 Issue，本脚本不参与。

用法：
    python scripts/mark_nontrading.py --date 20261001
    python scripts/mark_nontrading.py --date 20261001 --reason "官方未发布"
"""
import argparse
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NONTRADING_FILE = os.path.join(ROOT, 'nontrading-days.json')
THEMES_DIR = os.path.join(ROOT, 'themes')
REVIEW_DIR = os.path.join(ROOT, 'review')


def load():
    if not os.path.exists(NONTRADING_FILE):
        return {'updated': '', 'dates': []}
    try:
        d = json.load(open(NONTRADING_FILE, encoding='utf-8'))
        return {'updated': d.get('updated') or '', 'dates': list(d.get('dates') or [])}
    except Exception:
        return {'updated': '', 'dates': []}


def save(obj):
    obj['dates'] = sorted(set(obj['dates']))
    json.dump(obj, open(NONTRADING_FILE, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    return obj


def drop_stale(datestr):
    """清掉未通过校验的残留产物；已 verified 的不动。"""
    removed = []
    tf = os.path.join(THEMES_DIR, datestr + '.json')
    if os.path.exists(tf):
        keep = False
        try:
            keep = bool(json.load(open(tf, encoding='utf-8')).get('verified'))
        except Exception:
            keep = False
        if not keep:
            os.remove(tf)
            removed.append('themes/%s.json' % datestr)
            rf = os.path.join(REVIEW_DIR, datestr + '.json')
            if os.path.exists(rf):
                os.remove(rf)
                removed.append('review/%s.json' % datestr)
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--reason', default='两次尝试均未取到官方复盘帖')
    a = ap.parse_args()

    d = str(a.date).replace('-', '')
    obj = load()
    fresh = d not in obj['dates']
    obj['dates'].append(d)
    obj['updated'] = date.today().strftime('%Y-%m-%d')
    save(obj)

    removed = drop_stale(d)
    print('判定 %s 为非交易日（%s），不需要数据。' % (d, a.reason))
    if fresh:
        print('  已记入 nontrading-days.json（累计 %d 天）' % len(obj['dates']))
    else:
        print('  该日期此前已记录过')
    if removed:
        print('  清理未通过校验的残留：%s' % ', '.join(removed))
    else:
        print('  无残留产物')
    return 0


if __name__ == '__main__':
    sys.exit(main())
