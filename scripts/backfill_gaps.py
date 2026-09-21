#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 themes/ 里缺失的交易日并补跑，使仓库始终保留最近 N 个交易日。

用法：
    python scripts/backfill_gaps.py                 # 默认保留 30，一次最多补 6 天
    python scripts/backfill_gaps.py --keep 30 --max 6
    python scripts/backfill_gaps.py --dry           # 只列出缺口，不真的跑

做法：
  1. 用交易日历算出「截至今天」最近的 KEEP 个交易日
  2. 与 themes/*.json 求差集 → 缺口
  3. **从最早的缺口开始补**，一次最多 MAX 个（OCR 一天约 2~4 分钟，
     分批补可以让后续定时运行接着补，不会把单次任务拖太长）
  4. 逐个调用 pipeline.run_one()（幂等：已 verified 的日期会被自动跳过）

退出码：0 = 无缺口或全部补齐；2 = 仍有缺口（留给下一次运行继续）
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import trading_calendar as tc           # noqa: E402

THEMES_DIR = os.path.join(ROOT, 'themes')
NONTRADING_FILE = os.path.join(ROOT, 'nontrading-days.json')


def nontrading_dates():
    """两次都没拉到、已判定为非交易日的日期（不需要数据，补跑时跳过）。"""
    if not os.path.exists(NONTRADING_FILE):
        return set()
    try:
        return set(json.load(open(NONTRADING_FILE, encoding='utf-8')).get('dates') or [])
    except Exception:
        return set()


def _pipeline():
    """延迟导入：--dry 时不要求 requests 等依赖已安装。"""
    import pipeline
    return pipeline


def existing_dates():
    if not os.path.isdir(THEMES_DIR):
        return set()
    return {f[:-5] for f in os.listdir(THEMES_DIR)
            if f.endswith('.json') and len(f) == 13}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep', type=int, default=30)
    ap.add_argument('--max', type=int, default=6)
    ap.add_argument('--end')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    end = a.end or datetime.now().strftime('%Y%m%d')
    want = tc.last_n(a.keep, end)
    skip = nontrading_dates()          # 已判定非交易日的，不再反复尝试
    have = existing_dates()
    missing = [d for d in want if d not in have and d not in skip]

    print('目标：保留最近 %d 个交易日（截至 %s）' % (a.keep, end))
    print('  应有 %d 个 / 已有 %d 个 / 已判定非交易日 %d 个 / 缺口 %d 个'
          % (len(want), len(want) - len(missing), len(skip & set(want)), len(missing)))
    if missing:
        print('  缺口：%s' % ' '.join(missing))

    # 超出 KEEP 的旧文件由 cleanup.yml 负责删除，这里不管
    if not missing:
        print('  无缺口 ✓')
        return 0

    todo = missing[:a.max]
    if len(todo) < len(missing):
        print('  本次只补最早的 %d 个，剩余 %d 个留给下次运行' % (len(todo), len(missing) - len(todo)))

    if a.dry:
        print('  --dry：不实际执行')
        return 2

    pipeline = _pipeline()
    ok = 0
    for d in todo:
        try:
            if pipeline.run_one(d):
                ok += 1
                print('  ✓ %s 已补齐' % d)
            else:
                print('  ✗ %s 未通过校验（下次会重试）' % d)
        except Exception as e:
            print('  ✗ %s 异常：%s' % (d, e))

    left = len(missing) - ok
    print('\n补跑完成：成功 %d / 仍缺 %d' % (ok, left))
    return 0 if left == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
