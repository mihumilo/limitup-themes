#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 themes/ 里缺失的日期并补跑，使仓库始终保留最近 N 个有数据的日子。

★ 不依赖交易日历（已取消）。判定规则改为「拉两次都拿不到 = 非交易日」：
   · 候选日期 = 工作日（周一~周五）—— 这是唯一还需要"猜"的地方，且它只是候选
   · 补跑拿不到的日期，本脚本会**自动记入 nontrading-days.json**（只试一次，不反复补）
   · 误判了直接编辑该文件删掉那条即可，下次运行会重新尝试

★★ 宽限期（--grace-days，默认 3 天）
   「今天」在 16:30 那次运行时，官方复盘帖**可能还没发布**（通常 15:30~16:30 才发）。
   如果这时就按「拿不到 = 非交易日」处理，两次失败后今天会被永久跳过，
   哪怕半小时后帖子发布了也不会再补 —— 这是本脚本历史上最危险的一个坑。
   所以：**距今不足 grace-days 天的日期，失败只累计次数，绝不判定为跳过。**

用法：
    python scripts/backfill_gaps.py                 # 默认保留 30，一次最多补 6 天
    python scripts/backfill_gaps.py --keep 30 --max 6
    python scripts/backfill_gaps.py --dry           # 只列出缺口，不真的跑
    python scripts/backfill_gaps.py --reset-tries   # 清空失败计数与已判定名单

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

NOTE = ('拉两次都拿不到数据的日期。dates = 已判定跳过（失败满 2 次且已过宽限期）；'
        'tries = 失败次数。误判就把对应日期从这里删掉，或跑 --reset-tries 全部清空。')


def load_state():
    if not os.path.exists(NONTRADING_FILE):
        return {'dates': [], 'tries': {}}
    try:
        d = json.load(open(NONTRADING_FILE, encoding='utf-8'))
        return {'dates': list(d.get('dates') or []), 'tries': dict(d.get('tries') or {})}
    except Exception:
        return {'dates': [], 'tries': {}}


def save_state(dates, tries):
    data = {
        'dates': sorted(set(dates)),
        # tries 只保留最近 200 条，避免无限增长
        'tries': dict(sorted(tries.items())[-200:]),
        'updated': datetime.now().strftime('%Y-%m-%d'),
        'note': NOTE,
    }
    try:
        json.dump(data, open(NONTRADING_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


def nontrading_dates():
    """两次都没拉到、已判定为非交易日的日期（不需要数据，补跑时跳过）。"""
    return set(load_state()['dates'])


def reset_tries():
    """清空「已判定跳过名单」与「失败计数」。

    什么时候需要：批量重建数据时（比如刚把 themes/ 清空重跑），
    之前累积的失败计数会让某些日期被永久跳过 —— 那些日期其实是有数据的，
    只是当时因为 bug 或网络故障没拿到。"""
    save_state([], {})
    print('已清空 nontrading-days.json（dates 与 tries 都置空）')


def mark_nontrading(date, grace_days=3):
    """记一次「补不到」。

    两条安全阀：
      · **失败满 2 次**才真正跳过 —— 偶发网络故障不会把有数据的日子永久跳过
      · **宽限期**内（距今 < grace_days 天）只累计次数，**绝不判定跳过**
        —— 否则「官方帖还没发布」会被误判成「非交易日」（见文件头说明）

    返回 (是否新判定为跳过, 累计失败次数, 是否在宽限期内)。"""
    st = load_state()
    dates, tries = set(st['dates']), dict(st['tries'])
    n = int(tries.get(date, 0)) + 1
    tries[date] = n

    # YYYYMMDD 可直接按字符串比较
    cutoff = (datetime.now() - timedelta(days=grace_days)).strftime('%Y%m%d')
    in_grace = date > cutoff

    newly = False
    if n >= 2 and date not in dates and not in_grace:
        dates.add(date)
        newly = True
    save_state(dates, tries)
    return newly, n, in_grace


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
    ap.add_argument('--grace-days', type=int, default=3,
                    help='距今不足 N 天的日期失败时只计数、不判定为非交易日（默认 3）')
    ap.add_argument('--reset-tries', action='store_true',
                    help='先清空已判定名单与失败计数，再做本次检查')
    a = ap.parse_args()

    if a.reset_tries:
        reset_tries()

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
            continue
        newly, n, in_grace = mark_nontrading(d, a.grace_days)
        if in_grace:
            print('  ✗ %s 本次没拿到（累计 %d 次）→ 还在 %d 天宽限期内，'
                  '不判定为非交易日，下次继续尝试' % (d, n, a.grace_days))
        elif newly:
            print('  ✗ %s 补不到 → 已连续失败 %d 次，后续不再尝试'
                  '（误判请从 nontrading-days.json 删掉，或跑 --reset-tries）' % (d, n))
        else:
            print('  ✗ %s 补不到（累计 %d 次；再失败一次才会跳过）' % (d, n))

    left = len(missing) - ok
    print('\n补跑完成：成功 %d / 本次尝试 %d / 仍缺 %d' % (ok, len(todo), left))
    # ★ 退出码语义：**只要本次有进展就算成功**。
    #   以前是「仍有缺口 → 2」，于是 daily.yml 里每跑一次都留下一条
    #   "Process completed with exit code 2" 的红色错误注解 —— 但那不是故障，
    #   只是「一次最多补 6 天，剩下的留给下次」，本来就是预期行为。
    #   只有「一个都没补上」才是真异常（那时才需要人在日志里看一眼）。
    return 0 if ok > 0 else 2


if __name__ == '__main__':
    sys.exit(main())
