#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 themes/ 里缺失的日期并补跑，使仓库始终保留最近 N 个有数据的日子。

★ 不依赖交易日历（已取消）。判定规则改为「拉两次都拿不到 = 非交易日」：
   · 候选日期 = 工作日（周一~周五）—— 这是唯一还需要"猜"的地方，且它只是候选
   · 补跑拿不到的日期，本脚本会**自动记入 nontrading-days.json**（只试一次，不反复补）
   · 误判了直接编辑该文件删掉那条即可，下次运行会重新尝试

用法：
    python scripts/backfill_gaps.py                 # 默认保留 30，一次最多补 6 天
    python scripts/backfill_gaps.py --keep 30 --max 6
    python scripts/backfill_gaps.py --dry           # 只列出缺口，不真的跑

做法：
  1. 取最近 KEEP+15 个工作日做候选，剔掉已判定非交易日的，再取最后 KEEP 个
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
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

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


def mark_nontrading(date):
    """记一次「补不到」。**同一个日期失败到 2 次才真正跳过** —— 这样偶发的网络故障
    不会把原本有数据的日子永久跳过，而节假日连试两次之后就不再折腾。

    ★ 误判了直接编辑 nontrading-days.json 删掉那条（或把它从 tries 里去掉），
      下次运行会重新尝试。

    返回 (是否新判定为跳过, 累计失败次数)。"""
    data = {'dates': [], 'tries': {}}
    if os.path.exists(NONTRADING_FILE):
        try:
            data = json.load(open(NONTRADING_FILE, encoding='utf-8')) or data
        except Exception:
            pass
    dates = set(data.get('dates') or [])
    tries = dict(data.get('tries') or {})
    n = int(tries.get(date, 0)) + 1
    tries[date] = n
    newly = False
    if n >= 2 and date not in dates:
        dates.add(date)
        newly = True
    data['dates'] = sorted(dates)
    # tries 只保留最近 200 条，避免无限增长
    data['tries'] = dict(sorted(tries.items())[-200:])
    data['updated'] = datetime.now().strftime('%Y-%m-%d')
    data['note'] = ('拉两次都拿不到数据的日期。dates = 已判定跳过（失败满 2 次）；'
                    'tries = 失败次数。误判就把对应日期从这里删掉，下次会重新尝试。')
    try:
        json.dump(data, open(NONTRADING_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass
    return newly, n


def recent_weekdays(n, end):
    """从 end 往前取 n 个工作日（升序）。工作日只是「候选」，不代表一定是交易日。"""
    d = datetime.strptime(end, '%Y%m%d')
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return sorted(out)


def want_dates(keep, end, skip):
    """想要的日期 = 最近的 keep 个「非已知非交易日」的工作日。
       多取 15 个工作日做缓冲，免得期间有节假日导致凑不满 keep 个。"""
    cand = recent_weekdays(keep + 15, end)
    pool = [d for d in cand if d not in skip]
    return pool[-keep:]


def existing_dates():
    if not os.path.isdir(THEMES_DIR):
        return set()
    return {f[:-5] for f in os.listdir(THEMES_DIR)
            if f.endswith('.json') and len(f) == 13}


def _pipeline():
    """延迟导入：--dry 时不要求 requests 等依赖已安装。"""
    import pipeline
    return pipeline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep', type=int, default=30)
    ap.add_argument('--max', type=int, default=6)
    ap.add_argument('--end')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    end = a.end or datetime.now().strftime('%Y%m%d')
    skip = nontrading_dates()
    want = want_dates(a.keep, end, skip)
    have = existing_dates()
    missing = [d for d in want if d not in have]

    print('目标：保留最近 %d 个有数据的日子（截至 %s；无交易日历）' % (a.keep, end))
    print('  候选 %d 个 / 已有 %d 个 / 缺口 %d 个 / 累计已判定非交易日 %d 个'
          % (len(want), len(want) - len(missing), len(missing), len(skip)))
    if missing:
        print('  缺口：%s' % ' '.join(missing))
    if skip:
        print('  已判定非交易日（不再尝试）：%s' % ' '.join(sorted(skip)[-8:]))

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
            got = pipeline.run_one(d)
        except Exception as e:
            print('  ✗ %s 异常：%s' % (d, e))
            got = False
        if got:
            ok += 1
            print('  ✓ %s 已补齐' % d)
        else:
            newly, n = mark_nontrading(d)
            if newly:
                print('  ✗ %s 补不到 → 已连续失败 %d 次，后续不再尝试（误判请从 nontrading-days.json 删掉）' % (d, n))
            else:
                print('  ✗ %s 补不到（累计 %d 次；再失败一次才会跳过）' % (d, n))

    left = len(missing) - ok
    print('\n补跑完成：成功 %d / 仍缺 %d' % (ok, left))
    return 0 if left == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
