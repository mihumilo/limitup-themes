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
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# 本测试只驱动解析逻辑（不联网、不读图），但 pipeline 顶层 import requests/PIL。
# 本地/CI 若没装这两个包，用空模块顶上，免得测试因为环境问题跑不起来。
for _m in ('requests',):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
try:
    import PIL.Image  # noqa: F401
except ImportError:
    _pil = types.ModuleType('PIL')
    _pilimg = types.ModuleType('PIL.Image')
    _pil.Image = _pilimg
    sys.modules['PIL'] = _pil
    sys.modules['PIL.Image'] = _pilimg

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
# 连板列在官方图上是**竖排**（「首」在上、「板」在下），OCR 切成两块 → 必须能拼回
add(1210, C_STREAK, '首')
add(1245, C_STREAK, '板')
# 还可能残留这种碎块 —— 既不能被当成关键词，也不能干扰连板拼接
add(1212, C_STREAK, '连板')
# 关键词列常是多行，OCR 拆成多块 → 应拼回「A+B+C」
add(1200, C_KW, '氧化锆粉体')
add(1240, C_KW, 'MLCC验证')
add(1280, C_KW, '越南基地')
add(1210, 900, '1、据2026年9月16日互动易回复，公司氧化锆粉体已通过验证')
add(1580, C_CODE, '华软科技')
add(1610, C_CODE, '002453')
add(1610, C_AMT, '5.34亿')
add(1610, C_TIME, '10:41:15')
# 竖排 + N天M板：切成「3天」「2板」两块，拼回后连板数取 M = 2
add(1610, C_STREAK, '3天')
add(1645, C_STREAK, '2板')
add(1600, C_KW, '光引发剂')
add(1640, C_KW, '并购莱恩光电')
add(1680, C_KW, '造纸化学品')
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

# ---- 主题三：完整标题，且股票排在标题之后（间距按真实排版 ≈300px）----
add(9430, 960, '大消费*10')
add(9700, C_CODE, '会稽山')
add(9730, C_CODE, '601579')
add(9730, C_AMT, '7.27亿')
add(9730, C_TIME, '09:43:04')
# 竖排 + N天M板：切成「4」「天3板」两块，拼回后连板数取 M = 3
add(9730, C_STREAK, '4')
add(9765, C_STREAK, '天3板')
add(9720, C_KW, '黄酒')
add(9760, C_KW, '中秋国庆')

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
    # 连板列是竖排文字，OCR 会把「3」+「3板」粘成 '33'。旧的宽松兜底会读成 33 板
    ('竖排粘连的连板值不被采信（33/22 → None）',
     P.parse_streak('33') is None and P.parse_streak('22') is None
     and P.parse_streak('3') == 3 and P.parse_streak('首板') == 1
     and P.parse_streak('4天4板') == 4 and P.parse_streak('6天3板') == 3),
    # 同花顺池「N天M板」→ 连板数（连续口径）：N==M 取 M，否则是新一轮首板=1
    # 已用 20260918 官方图逐一核对：和顺石油 4天2板→1、远望谷 6天3板→1（都是首板）
    ('池「N天M板」换算成连板数（与官方图一致）',
     P.pool_streak_of('3天3板', 196611) == 3 and P.pool_streak_of('4天4板', 262148) == 4
     and P.pool_streak_of('4天2板', 131076) == 1 and P.pool_streak_of('6天3板', 196614) == 1
     and P.pool_streak_of('2天2板', 131074) == 2 and P.pool_streak_of('首板', None) == 1),
]:
    print('  %s %s' % ('[OK]' if passed else '[FAIL]', label))
    ok = ok and passed

# ---------------------------------------------------------------- 字段来源策略
# 名称/时间/关键词 以涨停池为准（与官方图同源、无 OCR 残片）。
# 连板数：pipeline 产出的这条 JSON 里「以图为准、池兜底」（图的连板列读得出就用图值）；
# 但**看板最终显示的连板数由 Worker 用同花顺涨停池的连续涨停天数覆盖**
# （worker 的 pickStreak()），因为官方图那列是竖排 OCR，容易拼错（'3'+'3'→33）。
pool2 = {'001216': {'name': '华瓷股份', 'reason': '氧化锆粉体+MLCC验证+越南基地',
                    'time': '09:56:39', 'streak': 4}}
t2 = {t['name']: t for t in P.parse_rows(items, pool2, W)}
s2 = [s for s in t2.get('算力/半导体产业链', {}).get('stocks', [])
      if s['code'] == '001216']
print('\n--- 字段来源策略 ---')
use_ok = bool(s2) and s2[0]['time'] == '09:56:39' \
    and s2[0]['keyword'] == '氧化锆粉体+MLCC验证+越南基地'
print('  001216 →', s2[0] if s2 else '(缺)')
print('  %s 时间/关键词取池（无 OCR 残片）' % ('[OK]' if use_ok else '[FAIL]'))
ok = ok and use_ok

# JSON 内连板以图为准：图上写「首板」，池写 4 板 → 这条 JSON 里必须是 1
streak_img_ok = bool(s2) and s2[0]['streak'] == 1
print('  %s JSON 内连板以图为准（图首板 vs 池4板 → 1）'
      % ('[OK]' if streak_img_ok else '[FAIL]'))
ok = ok and streak_img_ok

# 池兜底：图上没有连板列时，才用池的连板数
mini = [('f.png', 0, 700, 960, '算力/半导体产业链*1'),
        ('f.png', 0, 1210, 45, '001216'),
        ('f.png', 0, 1210, 363, '4.96亿')]
t4 = {t['name']: t for t in P.parse_rows(mini, pool2, W)}
s4 = [s for s in t4.get('算力/半导体产业链', {}).get('stocks', [])
      if s['code'] == '001216']
fb_ok = bool(s4) and s4[0]['streak'] == 4
print('  %s 图上无连板列时回落到池值（→4）'
      % ('[OK]' if fb_ok else '[FAIL]'))
ok = ok and fb_ok

# ---------------------------------------------------------------- 跨图延续
# 长图被拆成多张时，每张图的 y 都从 0 开始。上一张图末尾的主题会延续到
# 下一张图的开头（在下一张图第一个标题之前）。归属必须按图分组做，否则会串位。
multi = []


def madd(fn, y, x, t):
    multi.append((fn, 0, y, x, t))


# 第 1 张图：算力 3 只（只放下前 2 只）
madd('20260918_0.png', 700, 960, '算力/半导体产业链*3')
madd('20260918_0.png', 1150, 45, '001216')
madd('20260918_0.png', 1150, 513, '09:56:39')
madd('20260918_0.png', 1550, 45, '002453')
madd('20260918_0.png', 1550, 513, '10:41:15')
# 第 2 张图：开头是算力的第 3 只（无标题），之后才是新主题
madd('20260918_1.png', 300, 45, '601579')          # y 也从 0 起算，与上图重叠
madd('20260918_1.png', 300, 513, '09:43:04')
madd('20260918_1.png', 1200, 960, '大消费*1')
madd('20260918_1.png', 1600, 45, '000002')
madd('20260918_1.png', 1600, 513, '14:22:18')

mt = P.parse_rows(multi, pool, W)
mgot = {t['name']: [s['code'] for s in t['stocks']] for t in mt}
print('\n--- 跨图延续 ---')
print('  第1张图算力2只 + 第2张图开头1只 →', mgot.get('算力/半导体产业链'))
print('  第2张图新主题大消费 →', mgot.get('大消费'))
cross_ok = (mgot.get('算力/半导体产业链') == ['001216', '002453', '601579']
            and mgot.get('大消费') == ['000002'])
print('  %s 跨图归属正确（第2张图开头的股票延续上一主题）'
      % ('[OK]' if cross_ok else '[FAIL]'))
ok = ok and cross_ok

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
    HEIGHTS = [11058, 11528, 10377]      # 三张图的真实高度
    sim3, fi, y = [], 0, 500

    def sadd(x, t, yy=None):
        sim3.append(('%s_%d.png' % (gt['date'], fi), 0, y if yy is None else yy, x, t))

    def nxt_file():
        """换到下一张图（y 归零）——整行判断，绝不让一行被切在两图之间（真实图就是这样）"""
        global fi, y
        fi += 1
        y = 500

    for th in gt['themes']:
        if fi < 2 and y + 200 > HEIGHTS[fi]:
            nxt_file()
        sadd(960, '%s*%d' % (th['name'], th['declare']))
        y += 150
        for st in th['stocks']:
            if fi < 2 and y + 300 > HEIGHTS[fi]:
                nxt_file()
            y += 220
            sadd(45, st['name'], y - 25)
            sadd(45, st['code'])
            sadd(363, '4.96亿')
            sadd(513, st['time'] or '09:30:00')
            # 连板列真实格式：首板 / N天M板（连板数取 M）
            sadd(622, '首板' if st['streak'] == 1
                 else '%d天%d板' % (st['streak'] + 1, st['streak']))
            if st['streak'] > 1:
                sadd(622, '连板', y + 12)          # 连板列被切碎的残片
            for pi, part in enumerate([p for p in (st['keyword'] or '').split('+') if p]):
                sadd(800, part, y + pi * 40)        # 关键词列多行，需拼回
            # 原因列干扰：中央区域里的杂质短词，绝不能变成标题
            sadd(1100, '业绩增长')
            sadd(1200, '*2')
            sadd(1300, '大消费')
            # 模拟重叠切片：同一行被识别两次（整行每个词块都重复一遍，y 抖动 5px）
            if st['code'].endswith('6'):
                sadd(45, st['code'], y + 5)
                for pi, part in enumerate([p for p in (st['keyword'] or '').split('+') if p]):
                    sadd(800, part, y + pi * 40 + 5)
            y += 200

    sim_pool = {}
    for th in gt['themes']:
        for st in th['stocks']:
            sim_pool[st['code']] = st['name']

    sim_themes = P.parse_rows(sim3, sim_pool, W)
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

# ---- ⑤ 标题居中带必须按「本图」宽度算（2026-08-12 整张图标题漏识别的根因）----
#   长图宽度并不固定：实测 1152 / 1056 / 960 / 1440 / 1921 都出现过。
#   以前 classify 收到的 width 是写死的 1921，于是「居中带」恒为 576~1345：
#   · 宽 1152 的图正确带是 346~806，只剩 576~806 还能命中
#   · 宽 1056 的图正确带是 317~739，标题(x≈528) 全部落在带外 → 整张漏掉
print('\n[5] 标题居中带按图宽自适应')
for img_w in (1152, 1056, 960, 1440, 1921):
    x_title = int(img_w * 0.5)                    # 标题居中
    fake = [
        ('w%d.png' % img_w, 0, 100, x_title, '维生素*3'),
        ('w%d.png' % img_w, 0, 400, 60, '600000'),
        ('w%d.png' % img_w, 0, 400, 320, '09:31'),
        ('w%d.png' % img_w, 0, 400, 380, '首板'),
        ('w%d.png' % img_w, 0, 400, 420, '维生素'),
    ]
    prof = P.column_profile(fake)
    got_new = [t['name'] for t in P.classify(fake, img_w, prof)[0]]
    got_old = [t['name'] for t in P.classify(fake, 1921, prof)[0]]
    print('  宽 %-5d 按图宽→%-6s 按写死的1921→%s'
          % (img_w, (got_new or ['无'])[0], (got_old or ['无'])[0]))
    if not got_new:
        print('  [FAIL] 宽 %d 的图按真实宽度仍识别不到标题' % img_w)
        ok = False
# 顺带验证：窄图上「写死 1921」确实会漏（说明这个测试真的能抓住老 bug）
narrow = [('n.png', 0, 100, 528, '维生素*3'),
          ('n.png', 0, 400, 60, '600000'),
          ('n.png', 0, 400, 320, '09:31'),
          ('n.png', 0, 400, 380, '首板'),
          ('n.png', 0, 400, 420, '维生素')]
if P.classify(narrow, 1921, P.column_profile(narrow))[0]:
    print('  [FAIL] 写死 1921 时窄图标题竟然没被漏掉 —— 测试用例本身失效了')
    ok = False

# ---- ⑥ 标题被 OCR 切成「名」+「*N」两块（8/12 图0 顶部「大消费*11」就是这么丢的）----
#   rescue 的三个条件里，原来「名称到图注距离 ≤110px」是按 1921 宽图的字号定的，
#   窄图上「大消费 + *11」的间距能到 200px 上下 → 真标题被整条挡掉。
#   现在改用「整体中点是否居中」（不随图宽变化的几何特征）。
print('\n[6] 窄图上标题被切成两块时能否救回（「名」+「*N」）')
CASES = [(1152, 480, 690), (1056, 440, 630), (960, 400, 575), (1440, 600, 860), (1921, 800, 1150)]
for img_w, name_x, dec_x in CASES:
    fake = [
        ('t%d.png' % img_w, 0, 100, name_x, '大消费'),
        ('t%d.png' % img_w, 0, 100, dec_x, '*11'),
        ('t%d.png' % img_w, 0, 500, 60, '600000'),
        ('t%d.png' % img_w, 0, 500, 320, '09:31'),
        ('t%d.png' % img_w, 0, 500, 380, '首板'),
        ('t%d.png' % img_w, 0, 500, 420, '乳业'),
    ]
    prof = P.column_profile(fake)
    got_new = [t['name'] for t in P.classify(fake, img_w, prof)[0]]
    got_old = [t['name'] for t in P.classify(fake, 1921, prof)[0]]
    print('  宽 %-5d 按图宽→%-8s 按写死1921→%s'
          % (img_w, (got_new or ['无'])[0], (got_old or ['无'])[0]))
    if not got_new:
        print('  [FAIL] 宽 %d 的图上「名+图注」被切开后仍救不回来' % img_w)
        ok = False
# 窄图 + 切碎：这正是 8/12 的失败场景，老代码必须复现失败（否则用例没意义）
narrow2 = [('n2.png', 0, 100, 480, '大消费'),
           ('n2.png', 0, 100, 690, '*11'),
           ('n2.png', 0, 500, 60, '600000'),
           ('n2.png', 0, 500, 320, '09:31'),
           ('n2.png', 0, 500, 380, '首板'),
           ('n2.png', 0, 500, 420, '乳业')]
if P.classify(narrow2, 1921, P.column_profile(narrow2))[0]:
    print('  [FAIL] 老逻辑（1921）在窄图上竟然能救回 —— 用例没复现出原 bug')
    ok = False

print('\n自测结论:', '全部通过' if ok else '存在失败项')
sys.exit(0 if ok else 1)
