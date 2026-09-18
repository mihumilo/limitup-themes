#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析逻辑回归测试（不依赖 OCR，用模拟的表格坐标驱动 parse_rows）

覆盖的四个历史 bug：
  ① 关键词被抓成成交额（"4.96亿"）
  ② 名称串号（把上一行的名字安到本行）
  ③ 重叠切片导致同一只股票被重复收录
  ④ 主题标题漏识别 / 被 OCR 切成两块 → 归属串区

用法：python scripts/test_parse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
import pipeline as P

W = 1921
C_CODE, C_AMT, C_TIME, C_STREAK, C_KW = 45, 210, 300, 365, 430
items = []


def add(y, x, t):
    items.append(('f.png', 0, y, x, t))


# ---- 主题一：完整标题 ----
add(700, 960, '算力/半导体产业链*20')
add(1180, C_CODE, '华瓷股份')
add(1210, C_CODE, '001216')
add(1210, C_AMT, '4.96亿')                       # 成交额：绝不能被当成关键词
add(1210, C_TIME, '09:56:39')
add(1210, C_STREAK, '首板')
add(1210, C_KW, '氧化锆粉体+MLCC验证+越南基地')
add(1210, 900, '1、据2026年9月16日互动易回复，公司氧化锆粉体已通过验证')
add(1580, C_CODE, '华软科技')
add(1610, C_CODE, '002453')
add(1610, C_AMT, '5.34亿')
add(1610, C_TIME, '10:41:15')
add(1610, C_STREAK, '2连板')
add(1610, C_KW, '光引发剂+并购莱恩光电+造纸化学品')
# 干扰项：原因列文本里出现的「业绩增长」（在中央区域、且带数字）。
# 它长在股票行上（同一行有代码 001216），必须被「行独占性」判定为杂质，
# 绝不能变成主题——上一版就是它把区间切开，导致算力只剩 3 只。
add(1210, 1100, '业绩增长')
add(1210, 1150, '*2')
# 重叠切片产生的重复项（同一文本、几乎同一 y）—— 应被 dedupe 消掉
add(1211, C_CODE, '001216')
add(1211, C_AMT, '4.96亿')
add(1211, C_KW, '氧化锆粉体+MLCC验证+越南基地')

# ---- 主题二：标题被 OCR 切成「并购重组」+「*3」两块 ----
add(8913, 900, '并购重组')
add(8913, 980, '*3')
add(9200, C_CODE, '真爱美家')
add(9230, C_CODE, '003041')
add(9230, C_AMT, '1.5亿')
add(9230, C_TIME, '10:11:51')
add(9230, C_STREAK, '首板')
add(9230, C_KW, '家纺+跨境电商')

# ---- 主题三：完整标题，且股票排在标题之后 ----
add(9600, 960, '大消费*10')
add(9700, C_CODE, '会稽山')
add(9730, C_CODE, '601579')
add(9730, C_AMT, '7.27亿')
add(9730, C_TIME, '09:43:04')
add(9730, C_STREAK, '3连板')
add(9730, C_KW, '黄酒+中秋国庆')

pool = {'001216': '华瓷股份', '002453': '华软科技',
        '003041': '真爱美家', '601579': '会稽山'}

themes = P.parse_rows(items, pool, W)
got = {t['name']: t['stocks'] for t in themes}

print('\n--- 解析结果 ---')
ok = True
expect = {
    '算力/半导体产业链': [
        ('001216', '华瓷股份', '09:56:39', 1, '氧化锆粉体+MLCC验证+越南基地'),
        ('002453', '华软科技', '10:41:15', 2, '光引发剂+并购莱恩光电+造纸化学品'),
    ],
    '并购重组': [('003041', '真爱美家', '10:11:51', 1, '家纺+跨境电商')],
    '大消费': [('601579', '会稽山', '09:43:04', 3, '黄酒+中秋国庆')],
}
for name, rows in expect.items():
    g = got.get(name)
    if not g:
        print('  [FAIL] 缺少主题', name)
        ok = False
        continue
    for (code, nm, tm, st, kw) in rows:
        m = [s for s in g if s['code'] == code]
        if not m:
            print('  [FAIL] %s 缺 %s' % (name, code))
            ok = False
            continue
        s = m[0]
        bad = []
        if s['name'] != nm:
            bad.append('名称 %s≠%s' % (s['name'], nm))
        if s['time'] != tm:
            bad.append('时间 %s≠%s' % (s['time'], tm))
        if s['streak'] != st:
            bad.append('连板 %s≠%s' % (s['streak'], st))
        if s['keyword'] != kw:
            bad.append('关键词 %s≠%s' % (s['keyword'], kw))
        print('  %s %s %s %s板 %s  %s'
              % (name, s['code'], s['name'], s['streak'], s['time'],
                 '[OK]' if not bad else '[FAIL] ' + '; '.join(bad)))
        if bad:
            ok = False

allstocks = [s for t in themes for s in t['stocks']]
print('\n--- 回归检查 ---')
for label, passed in [
    ('成交额未被当成关键词', not any('亿' in s['keyword'] for s in allstocks)),
    ('无重复股票（重叠切片已去重）',
     len(allstocks) == len(set(s['code'] for s in allstocks))),
    ('名称未串号', all(s['name'] == pool.get(s['code'], s['name'])
                   for s in allstocks)),
    ('归属未串区（含被切开的标题）',
     len(got.get('算力/半导体产业链', [])) == 2
     and len(got.get('并购重组', [])) == 1
     and len(got.get('大消费', [])) == 1),
    ('原因列杂质未变成假主题（无「业绩增长」）', '业绩增长' not in got),
]:
    print('  %s %s' % ('[OK]' if passed else '[FAIL]', label))
    ok = ok and passed

# ---------------------------------------------------------------- 规模仿真
# 用仓库里最新一天的**真实**（主题数 / 家数 / 代码 / 时间 / 连板 / 关键词）
# 还原成坐标，走一遍完整解析，逐主题比对。列位置取自线上实测日志：
# 成交额 x=363、时间 x=513、连板 x=622，关键词列约 x=800
import glob
import json

themes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'themes')
cands = sorted(glob.glob(os.path.join(themes_dir, '*.json')))
if not cands:
    print('\n[跳过] 规模仿真：themes/ 下暂无真实产物')
else:
    gt = json.load(open(cands[-1], encoding='utf-8'))
    sim, cur = [], 700

    def sadd(y, x, t):
        sim.append(('full_1.png', 0, y, x, t))

    for th in gt['themes']:
        sadd(cur, 960, '%s*%d' % (th['name'], th['declare']))
        for st in th['stocks']:
            cur += 420
            sadd(cur - 25, 45, st['name'])
            sadd(cur, 45, st['code'])
            sadd(cur, 363, '4.96亿')
            sadd(cur, 513, st['time'] or '09:30:00')
            sadd(cur, 622, '首板' if st['streak'] == 1 else '%d连板' % st['streak'])
            sadd(cur, 800, st['keyword'])
            # 原因列干扰：中央区域里的杂质短词，绝不能变成标题
            sadd(cur, 1100, '业绩增长')
            sadd(cur, 1200, '*2')
            sadd(cur, 1300, '大消费')
            # 模拟重叠切片：同一行被识别两次（y 抖动 5px）
            if st['code'].endswith('6'):
                sadd(cur + 5, 45, st['code'])
                sadd(cur + 5, 800, st['keyword'])
        cur += 700

    sim_pool = {}
    for th in gt['themes']:
        for st in th['stocks']:
            sim_pool[st['code']] = st['name']

    sim_themes = P.parse_rows(sim, sim_pool, W)
    sim_got = {t['name']: t for t in sim_themes}
    print('\n--- 规模仿真（%s：%d 主题 / %d 只）---'
          % (os.path.basename(cands[-1]), len(gt['themes']), gt['total']))
    same_theme = set(sim_got) == set(t['name'] for t in gt['themes'])
    print('  %s 主题集合一致' % ('[OK]' if same_theme else '[FAIL]'))
    ok = ok and same_theme

    diff_total = 0
    for th in gt['themes']:
        g = sim_got.get(th['name'])
        gc = [s['code'] for s in g['stocks']] if g else []
        ec = th['codes']
        hit = (gc == ec)
        if not hit:
            diff_total += 1
            missing = [c for c in ec if c not in gc]
            extra = [c for c in gc if c not in ec]
            print('  [FAIL] %-16s 期望 %d 实得 %d ｜ 缺 %s ｜ 多 %s'
                  % (th['name'], len(ec), len(gc), missing[:5], extra[:5]))
    if diff_total == 0:
        print('  [OK] 全部 %d 个主题的成员代码与真实数据逐一吻合' % len(gt['themes']))
    else:
        print('  [FAIL] %d 个主题成员不吻合' % diff_total)
    ok = ok and diff_total == 0

    # 字段抽查：名称/时间/连板/关键词
    fbad = 0
    for th in gt['themes']:
        g = sim_got.get(th['name'])
        if not g:
            continue
        gm = {s['code']: s for s in g['stocks']}
        for st in th['stocks']:
            x = gm.get(st['code'])
            if not x:
                continue
            for k in ('name', 'time', 'streak', 'keyword'):
                if not st[k]:
                    continue      # 真实数据里该字段本身缺失（如北交所个股），不比对
                if x[k] != st[k]:
                    fbad += 1
                    if fbad <= 5:
                        print('  [FAIL] %s.%s 期望 %r 实得 %r'
                              % (st['code'], k, st[k], x[k]))
    print('  %s 字段比对%s' % ('[OK]' if fbad == 0 else '[FAIL]',
                            '全对' if fbad == 0 else (' 有 %d 处不一致' % fbad)))
    ok = ok and fbad == 0

print('\n自测结论:', '全部通过' if ok else '存在失败项')
sys.exit(0 if ok else 1)
