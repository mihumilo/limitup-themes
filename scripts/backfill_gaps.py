#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 themes/ 里缺失的日期并补跑，使仓库始终保留最近 N 个有数据的日子。

★ 保留窗口统一为 **90 个交易日**（2026-09-24 定，与 Worker 侧 KV 的 TTL/裁剪保持一致）。
  为什么是 90：情绪周期复盘要看「近 3 个月」的空间板/晋级率走势，30 天太短（只够 1 个多月），
  90 天能覆盖 3~5 个完整情绪周期。代价只是仓库 JSON 变多（约 90 个文件，纯文本，不成负担）。

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
    python scripts/backfill_gaps.py                 # 默认保留 90，一次最多补 6 天
    python scripts/backfill_gaps.py --keep 90 --max 6
    python scripts/backfill_gaps.py --dry           # 只列出缺口，不真的跑
    python scripts/backfill_gaps.py --reset-tries   # 清空失败计数与已判定名单

做法：
  1. 取最近 KEEP+15 个工作日做候选，剔掉已判定非交易日的，再取最后 KEEP 个
  2. 与 themes/*.json 求差集 → 缺口
  3. **从最早的缺口开始补**，一次最多 MAX 个（OCR 一天约 2~4 分钟，
     分批补可以让后续定时运行接着补，不会把单次任务拖太长）
  4. 逐个调用 pipeline.run_one()（幂等：已 verified 的日期会被自动跳过）

退出码（工作流依赖它，不要随意改）
--------
  0 = 正常：无缺口 / 本次有进展 / `--dry` 只查看 / `--reset-only` 只清空
  2 = 真异常：尝试了但**一个都没补上**（需要人看一眼日志）

★ 为什么不能让「仍有缺口」也返回 2：Actions 的每个 step 都是 `bash -e` 执行的，
  脚本返回非 0 会**当场中断该 step**，后面的命令全部不执行，并把作业判失败。
  而"一次最多补 max 个、剩下的留给下次"是设计内的预期行为，不是故障。
"""
import argparse
import json
import os
import sys
import time
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
    ap.add_argument('--keep', type=int, default=90)
    ap.add_argument('--max', type=int, default=6)
    ap.add_argument('--end')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--grace-days', type=int, default=3,
                    help='距今不足 N 天的日期失败时只计数、不判定为非交易日（默认 3）')
    ap.add_argument('--reset-tries', action='store_true',
                    help='先清空已判定名单与失败计数，再做本次检查')
    ap.add_argument('--reset-only', action='store_true',
                    help='只清空名单与失败计数，不做缺口检查（退出码恒为 0）')
    a = ap.parse_args()

    # ★ --reset-only：给工作流「只做清空」这一步专用。
    #   以前复用 `--reset-tries --dry`，而 --dry 会以 2 退出（语义是"仍有缺口"），
    #   Actions 用 bash -e 跑步骤 → 步骤当场中断 → 后面的 git commit/push 全没执行，
    #   prepare 作业失败 → 依赖它的 transcribe 被跳过 → 整个批量回填什么都没干。
    if a.reset_only:
        reset_tries()
        return 0

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

    # ★ --dry 只查看，**不算失败**。它以前返回 2（照搬"仍有缺口"的语义），
    #   在 Actions（bash -e）里会把步骤判失败，见 --reset-only 处的说明。
    if a.dry:
        print('  --dry：只查看，不实际执行')
        return 0

    # 依赖缺失（requests / rapidocr 没装）必须落在退出码契约内：
    # 否则 Python 直接以 1 崩出，工作流侧没法区分"环境坏了"和"数据没补上"。
    try:
        pipeline = _pipeline()
    except Exception as e:
        print('  ✗ 无法导入 pipeline：%s' % e)
        print('    通常意味着依赖没装好（pip install -r requirements.txt）')
        return 2

    tally = {'ok': 0, 'grace': 0, 'new_skip': 0, 'fail': 0, 'error': 0}
    t0 = time.time()
    for i, d in enumerate(todo, 1):
        print('\n  [%d/%d] %s' % (i, len(todo), d))
        try:
            got = pipeline.run_one(d)
        except Exception as e:
            # 单日异常不中断整批：记下来，继续下一个（部分失败不影响已成功的部分）
            tally['error'] += 1
            print('  ✗ %s 异常：%s（已跳过，不影响前面已补齐的日期）' % (d, e))
            continue
        if got:
            tally['ok'] += 1
            print('  ✓ %s 已补齐' % d)
            continue
        newly, n, in_grace = mark_nontrading(d, a.grace_days)
        if in_grace:
            tally['grace'] += 1
            print('  ✗ %s 本次没拿到（累计 %d 次）→ 还在 %d 天宽限期内，'
                  '不判定为非交易日，下次继续尝试' % (d, n, a.grace_days))
        elif newly:
            tally['new_skip'] += 1
            print('  ✗ %s 补不到 → 已连续失败 %d 次，后续不再尝试'
                  '（误判请从 nontrading-days.json 删掉，或跑 --reset-tries）' % (d, n))
        else:
            tally['fail'] += 1
            print('  ✗ %s 补不到（累计 %d 次；再失败一次才会跳过）' % (d, n))

    left = len(missing) - tally['ok']
    print('\n' + '=' * 52)
    print('补跑汇总（用时 %.0fs）' % (time.time() - t0))
    print('  已补齐 %d / 本次尝试 %d' % (tally['ok'], len(todo)))
    print('  宽限期内（下次继续）%d   单次失败 %d   新判定跳过 %d   异常 %d'
          % (tally['grace'], tally['fail'], tally['new_skip'], tally['error']))
    print('  总缺口 %d → 仍缺 %d（剩余留给下次运行）' % (len(missing), left))
    print('=' * 52)

    # ★ 退出码契约（工作流依赖它，不要随意改）：
    #   0 = 正常：无缺口 / 本次有进展 / --dry 只查看
    #   2 = 真异常：尝试了但一个都没补上（需要人看一眼日志）
    #   仅当「全部尝试都失败且没有任何进展」才返回 2；只要补上 1 个就算成功 ——
    #   "一次最多补 max 个，剩下的留给下次"本来就是预期行为，不该被判失败。
    return 0 if tally['ok'] > 0 else 2


if __name__ == '__main__':
    sys.exit(main())
