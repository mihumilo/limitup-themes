#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺「决战涨停板」官方复盘长图  ->  结构化主题分类 JSON

流程：定位帖子 -> 下载长图 -> RapidOCR(带坐标) -> 按坐标还原行
      -> 解析「主题 -> 个股(代码/名称/涨停时间/连板天数/涨停关键词)」
      -> 与同花顺涨停池交叉校验

输出：themes/YYYYMMDD.json（概念驱动板块 + 连板梯队用）
      review/YYYYMMDD.json（官方图索引用）

用法：
  python scripts/pipeline.py --date 20260918
  python scripts/pipeline.py --range 20260901 20260918
  python scripts/pipeline.py --date 20260918 --force
  python scripts/pipeline.py --date 20260918 --from-ocr _work   # 复用已缓存的 OCR 结果，不重新识图
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES_DIR = os.path.join(ROOT, 'themes')
REVIEW_DIR = os.path.join(ROOT, 'review')
WORK_DIR = os.path.join(ROOT, '_work')

CIRCLE = 'https://t.10jqka.com.cn/469885108/'
PID_COOKIE = 'v=1; vvvv=1'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
POOL_API = 'https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool'
POOL_REFERER = 'https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html'
POOL_FIELDS = ('199112,10,9001,330323,330324,330325,9002,330329,'
               '133971,133970,1968584,3475914,9003,9004')

SEG, OVERLAP = 900, 150          # 切片高度 / 重叠，保证跨切片的行不被切断
ROW_TOL = 38                     # 同一表格行的 y 容差
ROW_H = 240                      # 首/末行的行高上限（行区间由相邻代码中点切分得出）
TITLE_MIN_GAP = 120              # 标题与最近股票代码的最小纵向距离（标题独占一行）
                                 # 真实排版约 300px；取 120 留足容错，行间隙里的杂质
                                 # 再由「必带图注家数」这条兜住

# 历史官方主题词表（来自 2026-09 各日复盘图）。用途：
#   ① 救回被 OCR 切碎/变形的标题（如「大消费*10」被切成两块）
#   ② 拒绝中央区域里长得像标题的杂质（如原因文本里的「业绩增长」）
# 新主题（词表没有的）仍可通过 TITLE_RE 正则识别，不会被误杀。
KNOWN_THEMES = [
    '算力/半导体产业链', '大消费', '电力/风电', 'AI应用', '光通信', '地产产业链',
    '玻璃/LED', '机器人', '医药', '并购重组', '次新股', '高端装备', '其他概念',
    'PCB产业链', '锂电池', '消费电子', '海峡两岸', '炭黑', 'MLCC', '风电/电力',
    '固态电池', 'MLCC/电容', 'AI应用/网络安全', '光通信/铜缆', '汽车/机器人产业链',
    '汽车产业链', '磷化工/化肥', '大农业', '大金融', '军工/航天', '煤炭/煤化工',
    '控制权变更', '被动元件', '无人驾驶', '数字货币', '有色金属', '光伏玻璃',
    '液冷', '电力', '军工', '卫星', '煤炭', '燃气', '化工', '航运', '油气', '养猪',
]
NAME_ABOVE = 60                  # 名称在代码上方多少像素内
NAME_DX = 150                    # 名称与代码的水平距离上限

CODE_RE = re.compile(r'^\d{6}$')
AMOUNT_RE = re.compile(r'^\d+(\.\d+)?\s*[亿万]$')
NUM13_RE = re.compile(r'^\d{1,3}$')
TIME_RE = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?$')
# 连板列的图上格式是「首板」或「N天M板」（如 4天4板 / 3天2板 / 6天3板），
# 连板数取 M。同花顺涨停池的 high_days 同为此格式，high_days_value>>16 == M。
STREAK_RE = re.compile(r'^(首板|\d{1,2}\s*天\s*\d{1,2}\s*板|\d{1,2}\s*连板|\d{1,2}\s*板)$')
STREAK_WORDS = ('连板', '首板', '天数', '连板天数', '连板数', '几连板')


def parse_streak(sv):
    """从连板列文本取连板数：首板→1；N天M板→M；N连板→N；N板→N"""
    s = str(sv or '').replace(' ', '')
    if not s:
        return 1
    if s.startswith('首板'):
        return 1
    m = re.match(r'^(\d{1,2})天(\d{1,2})板$', s)
    if m:
        return int(m.group(2))
    m = re.match(r'^(\d{1,2})连板$', s)
    if m:
        return int(m.group(1))
    m = re.match(r'^(\d{1,2})板$', s)
    if m:
        return int(m.group(1))
    m = re.match(r'^(\d{1,2})', s)
    if m:
        return int(m.group(1))
    return 1
TITLE_RE = re.compile(r'^(.{2,20}?)[\s]*[\*＊✱x×X·•・]?[\s]*(\d{1,3})$')
TITLE_NAME_RE = re.compile(r'^[\u4e00-\u9fa5A-Za-z0-9/]+$')   # 标题名形态：中文/字母/数字/斜杠
LONG_RE = re.compile(r'_(\d+)_(\d+)\.(?:png|jpe?g|webp)$', re.I)
IMG_RE = re.compile(r'https?://u\.thsi\.cn/imgsrc/[^\s"\'\\()<>]+\.(?:png|jpe?g|webp)', re.I)
CJK_RE = re.compile(r'^[\u4e00-\u9fa5A-Za-z0-9\*\.·\-]{2,8}$')

_engine = None


def ocr_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def bj_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y%m%d')


def get(url, referer=None, params=None, timeout=30, cookie=None, retries=1):
    """带重试的 GET。cookie 用于 pid 详情页（同花顺对无 Cookie 请求返回 401）"""
    h = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'}
    if referer:
        h['Referer'] = referer
    if cookie:
        h['Cookie'] = cookie
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=h, params=params, timeout=timeout)
            # 401/403 是上游明确拒绝，重试没意义，直接返回
            if r.status_code in (401, 403):
                return r
            if r.status_code == 200:
                return r
            last = r
        except Exception as e:
            last = e
        if i < retries:
            time.sleep(1.5 * (i + 1))
    if isinstance(last, Exception):
        raise last
    return last


# ---------------------------------------------------------------- 定位帖子
def circle_page(n):
    try:
        r = get(CIRCLE + '?page=%d' % n)
        if r.status_code != 200:
            return []
        out = []
        for m in re.finditer(r'pid_(\d+)\.shtml', r.text):
            if m.group(1) not in out:
                out.append(m.group(1))
        return out
    except Exception:
        return []


def fetch_post(pid):
    """抓 pid 详情页。同花顺对无 Cookie 请求返回 401，必须带 PID_COOKIE。"""
    try:
        r = get('https://t.10jqka.com.cn/pid_%s.shtml' % pid,
                referer=CIRCLE, cookie=PID_COOKIE, timeout=25, retries=2)
        if r.status_code in (401, 403):
            print('  [warn] pid %s HTTP %s：同花顺拒绝了请求（Cookie 失效 或 出口 IP 被限制）'
                  % (pid, r.status_code))
            return None
        if r.status_code != 200:
            print('  [warn] pid %s HTTP %s' % (pid, r.status_code))
            return None
        html = r.text
        dm = re.search(r'class="detail-date[^"]*">\s*([^<]{6,24})<', html)
        if not dm:
            print('  [warn] pid %s 无 detail-date（版式变化 / 被限流），页首片段: %s'
                  % (pid, re.sub(r'\s+', ' ', html[:160])))
            return None
        imgs = []
        for m in IMG_RE.finditer(html):
            u = m.group(0).replace('_middle.', '.')
            if u not in imgs:
                imgs.append(u)
        longs = [u for u in imgs if (lambda g: (not g) or int(g.group(2)) >= 2000)(LONG_RE.search(u))]
        tm = re.search(r'class="detail-time[^"]*">([^<]{3,8})<', html)
        return {
            'pid': pid,
            'date': dm.group(1).replace('-', ''),
            'publishedAt': dm.group(1) + ' ' + (tm.group(1) if tm else ''),
            'title': (re.search(r'<title>([^<]{4,80})</title>', html) or [None, ''])[1],
            'imgs': longs if longs else imgs,
        }
    except Exception as e:
        print('  [warn] fetch_post(%s) 异常: %r' % (pid, e))
        return None


def fetch_post_from_api(date):
    """可选旁路：从已部署 Worker 的 /api/review 直接取长图 URL。

    用途：GitHub Actions 在美国出口常被同花顺判为异常（详情页 401）。
    这条链路让 Worker（Cloudflare 出口，已验证可访问）负责抓圈子页，
    GitHub 只负责下载图片做 OCR——两边各做各自擅长的事。

    配置：仓库 Settings → Secrets and variables → Actions → Variables
          REVIEW_API_BASE = https://<你的worker域名>/api/review
    """
    base = os.environ.get('REVIEW_API_BASE', '').strip()
    if not base:
        return None
    url = base + ('&' if '?' in base else '?') + 'date=' + date
    try:
        r = get(url, timeout=30, retries=1)
        j = r.json()
    except Exception as e:
        print('  [warn] 图源接口不可用: %r' % (e,))
        return None
    j = j or {}
    imgs = [u.replace('_middle.', '.') for u in (j.get('imgs') or [])]
    imgs = [u for u in imgs if IMG_RE.match(u)]
    longs = [u for u in imgs if (lambda g: (not g) or int(g.group(2)) >= 2000)(LONG_RE.search(u))]
    longs = longs or imgs
    if not longs:
        print('  [warn] 图源接口未返回图片，响应片段: %s' % str(j)[:200])
        return None
    print('  从图源接口（Worker）取到 %d 张长图' % len(longs))
    return {
        'pid': str(j.get('pid') or ''), 'date': date,
        'publishedAt': j.get('publishedAt') or '',
        'title': j.get('title') or '', 'imgs': longs,
    }


def days_ago(d):
    s, t = str(d), bj_today()
    a = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    b = datetime(int(t[0:4]), int(t[4:6]), int(t[6:8]))
    return max(0, (b - a).days)


def probe_page(n):
    pids = circle_page(n)
    if not pids:
        return None
    p = fetch_post(pids[0])
    return {'page': n, 'date': p['date'] if p else None}


def find_post(date):
    """定位当日复盘帖
       ① 先精扫列表页最近几篇（覆盖今天/昨天，绝大多数情况直接命中）
       ② 未命中再走「自然日->交易日」估页码 + 抽样探测 + 精扫
    """
    print('  定位 %s 的复盘帖…' % date)
    _probed = []

    def try_pids(pids, limit=None):
        """逐个抓详情，命中即返回；记录是否『全部取不到』以便诊断"""
        ok = 0
        for k, pid in enumerate(pids if limit is None else pids[:limit]):
            post = fetch_post(pid)
            if post:
                ok += 1
                if post['date'] == date:
                    return post
            if not post:
                _probed.append(pid)
        if ok == 0 and pids:
            print('  [warn] %d 个 pid 详情页全部取不到（多为 401：Cookie 失效 / 出口 IP 被封）' % len(pids))
        return None

    recent = []
    for n in (1, 2, 3):
        for p in circle_page(n):
            if p not in recent:
                recent.append(p)
    if not recent:
        print('  [warn] 圈子列表页未取到任何 pid（列表页也被限制？）')
    hit = try_pids(recent, 15)
    if hit:
        return hit

    est = max(1, int(days_ago(date) * 5 / 7 / 5) - 2)
    probes = [est + i * 8 for i in range(6)]
    pr = [probe_page(n) for n in probes]
    anchor, best = est, None
    for r in pr:
        if r and r['date'] and r['date'] >= date:
            if best is None or r['page'] > best['page']:
                best = r
    if best:
        anchor = best['page']
    else:
        cand = sorted([r for r in pr if r and r['date']], key=lambda x: x['page'])
        if cand:
            anchor = cand[0]['page']
    pages = [p for p in range(anchor - 3, anchor + 5) if p >= 1]
    pids = []
    for n in pages:
        for p in circle_page(n):
            if p not in pids:
                pids.append(p)
    for i in range(0, len(pids), 6):
        for pid in pids[i:i + 6]:
            post = fetch_post(pid)
            if post and post['date'] == date:
                return post
    return None


# ---------------------------------------------------------------- 下载 + OCR
def download(post, date):
    os.makedirs(WORK_DIR, exist_ok=True)
    paths = []
    for i, u in enumerate(post['imgs']):
        fp = os.path.join(WORK_DIR, '%s_%d.png' % (date, i))
        if os.path.exists(fp) and os.path.getsize(fp) > 50000:
            paths.append(fp)
            continue
        try:
            r = get(u, referer='https://t.10jqka.com.cn/', timeout=90)
            if r.status_code == 200 and len(r.content) > 50000:
                with open(fp, 'wb') as f:
                    f.write(r.content)
                paths.append(fp)
        except Exception as e:
            print('  下载失败 %s: %s' % (u, e))
    return paths


def ocr_with_pos(fp):
    """切片 OCR，返回 [(file, slice_idx, y, x, text)]，保留坐标以还原阅读顺序"""
    eng = ocr_engine()
    im = Image.open(fp).convert('RGB')
    w, h = im.size
    tmp = os.path.join(WORK_DIR, '_slice_tmp.png')
    out = []
    top, idx = 0, 0
    while top < h:
        bot = min(h, top + SEG)
        im.crop((0, top, w, bot)).save(tmp)
        res, _ = eng(tmp)
        for r in (res or []):
            box = r[0]
            cy = top + sum(p[1] for p in box) / len(box)
            cx = sum(p[0] for p in box) / len(box)
            txt = str(r[1]).replace('|', ' ').replace('\n', ' ').strip()
            if txt:
                out.append((os.path.basename(fp), idx, int(cy), int(cx), txt))
        idx += 1
        if bot >= h:
            break
        top = bot - OVERLAP
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass
    return out


def save_ocr(items, date):
    os.makedirs(WORK_DIR, exist_ok=True)
    fp = os.path.join(WORK_DIR, 'ocr_%s.json' % date)
    json.dump(items, open(fp, 'w', encoding='utf-8'), ensure_ascii=False)
    return fp


def load_ocr(date):
    fp = os.path.join(WORK_DIR, 'ocr_%s.json' % date)
    if os.path.exists(fp):
        return [tuple(x) for x in json.load(open(fp, encoding='utf-8'))]
    return None


# ---------------------------------------------------------------- 解析
def _file_key(fn):
    """按文件名尾部的序号排序：20260918_0.png < _1.png < … < _10.png（字典序会错）"""
    m = re.search(r'(\d+)(?=\.[A-Za-z]+$)', str(fn))
    return (int(m.group(1)) if m else 0, str(fn))


def dedupe_items(items):
    """重叠切片会让同一段文字被识别两次。按文本分桶，y/x 接近的视为同一次识别。
    阈值放宽到 80/40：切片边缘的 OCR 框常有十几像素抖动，之前 25px 阈值漏掉了部分重复。"""
    buckets = {}
    for it in sorted(items, key=lambda r: (r[4], r[2], r[3])):
        lst = buckets.setdefault(it[4], [])
        dup = False
        for k in lst:
            if abs(k[2] - it[2]) < 80 and abs(k[3] - it[3]) < 40:
                dup = True
                break
        if not dup:
            lst.append(it)
    out = []
    for lst in buckets.values():
        out.extend(lst)
    out.sort(key=lambda r: (r[2], r[3]))
    return out


def column_profile(items):
    """数据驱动发现各列中心 x：用格式明确的字段（时间/成交额/连板）反推表格列位置。
    比写死列宽更稳，因为不同日期的长图宽度/排版可能略有差异。"""
    def med(arr):
        if not arr:
            return None
        v = sorted(a[3] for a in arr)
        return v[len(v) // 2]
    ts = [it for it in items if TIME_RE.match(it[4].replace(' ', ''))]
    am = [it for it in items if AMOUNT_RE.match(it[4].replace(' ', ''))]
    st = [it for it in items if STREAK_RE.match(it[4].replace(' ', ''))]
    return {'time': med(ts), 'amount': med(am), 'streak': med(st),
            'n_time': len(ts), 'n_amount': len(am), 'n_streak': len(st)}


def _norm_theme(s):
    return re.sub(r'[\s\*＊✱x×X·•・:：,，。、/／]+', '', str(s or ''))


def _editdist(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def theme_prior(s):
    """与历史官方主题名匹配：2=精确 1=近似（前缀/编辑距离≤1） 0=不匹配"""
    n = _norm_theme(s)
    if len(n) < 2:
        return 0
    knowns = [_norm_theme(k) for k in KNOWN_THEMES]
    if n in knowns:
        return 2
    for k in knowns:
        if min(len(n), len(k)) >= 2 and abs(len(n) - len(k)) <= 2 \
                and (n.startswith(k) or k.startswith(n)):
            return 1
    for k in knowns:
        if len(n) >= 4 and abs(len(n) - len(k)) <= 1 and _editdist(n, k) <= 1:
            return 1
    return 0


def is_field_noise(s):
    """判断文本是不是其它列的字段（成交额/价格/时间/连板/表头词），
    用来防止它们被当成涨停关键词。s 需已去空格。"""
    if not s:
        return True
    if s in STREAK_WORDS or s in ('涨停', '涨停时间', '成交额', '价格',
                                  '涨停关键词', '名称', '代码', '股票简称'):
        return True
    if AMOUNT_RE.match(s) or TIME_RE.match(s) or STREAK_RE.match(s):
        return True
    if re.match(r'^\d+(\.\d+)?$', s):                 # 纯数字（价格 / 家数）
        return True
    if re.match(r'^\d{1,2}天(\d{1,2}板?)?$', s):      # 「3天」「3天2板」残片
        return True
    if re.match(r'^\d{1,2}连$', s):                    # 「2连」残片
        return True
    return False


def classify(items, width=1921, prof=None):
    """按「格式 + 列位置」分流，避免把成交额误当关键词。

    注意：必须**按单张图**调用（每张图的 y 都从 0 开始），否则行锚点会互相穿插。
    """
    if prof is None:
        prof = column_profile(items)
    # 关键词列 = 连板列右边那一列。下界必须避开连板列，否则「4天3板」「连板」
    # 这些连板列文本会被当成涨停关键词（历史 bug）。
    if prof['time'] is None:
        kw_lo = kw_hi = None
    else:
        kw_lo = prof['time'] + 15
        if prof['streak'] is not None and prof['streak'] > prof['time']:
            kw_lo = max(kw_lo, prof['streak'] + 20)
        kw_hi = prof['time'] + 430
    tx0, tx1 = width * 0.30, width * 0.70    # 标题居中带
    rx0, rx1 = width * 0.35, width * 0.65    # 救援候选的居中带（更严）
    titles, codes, times, streaks, keywords, names, amounts = [], [], [], [], [], [], []
    for it in items:
        f, si, y, x, t = it
        s = t.replace(' ', '')
        if not s:
            continue
        m = TITLE_RE.match(s)
        nm = m.group(1).strip() if m else ''
        if m and tx0 <= x <= tx1 and 2 <= len(nm) <= 20 and TITLE_NAME_RE.match(nm):
            titles.append({'y': y, 'x': x, 'name': nm,
                           'declare': int(m.group(2)), 'src': 'strict'})
            continue
        if CODE_RE.match(s):
            codes.append({'y': y, 'x': x, 'v': s})
            continue
        if TIME_RE.match(s):
            times.append({'y': y, 'x': x, 'v': s})
            continue
        if STREAK_RE.match(s):
            streaks.append({'y': y, 'x': x, 'v': s})
            continue
        if AMOUNT_RE.match(s):
            amounts.append({'y': y, 'x': x, 'v': s})
            continue
        # 关键词列：直接按「列区间」收集，**不限制长度** ——
        # 关键词常是长词块（如「投资半导体及机器人」11 字），
        # 若走 CJK_RE（2~8 字）会被整块丢掉（历史 bug）。
        if kw_lo is not None and kw_lo <= x <= kw_hi and 2 <= len(s) <= 40:
            if not is_field_noise(s):
                keywords.append({'y': y, 'x': x, 'v': s})
                continue
        if CJK_RE.match(s) and not s.replace('.', '').isdigit():
            names.append({'y': y, 'x': x, 'v': s})

    # ---- 标题判定的两个几何条件（在单张图内进行）----
    import bisect
    code_ys = sorted(c['y'] for c in codes)

    def dist_to_code(y):
        """到最近一只股票代码的纵向距离。标题独占一行，必然远离所有股票行。"""
        if not code_ys:
            return 10 ** 9
        i = bisect.bisect_left(code_ys, y)
        d = 10 ** 9
        if i < len(code_ys):
            d = min(d, code_ys[i] - y)
        if i > 0:
            d = min(d, y - code_ys[i - 1])
        return d

    def on_stock_row(y):
        return dist_to_code(y) < TITLE_MIN_GAP

    # ---- 标题兜底（rescue）：标题被 OCR 切碎时（如「大消费」+「*10」分成两块），
    #      在中央区域找与历史官方主题名匹配的短中文块救回来。三个硬条件：
    #      ① 先验匹配：命中历史官方主题词表（精确/前缀/编辑距离≤1）
    #      ② 必带图注家数：真标题一定有「*N」，正文里的同名杂质没有
    #         —— 上一版的假标题（海峡两岸 / 控制权变更 / 军工通信 / 光伏玻璃）
    #            全是图注 0，被这条全部拒掉
    #      ③ 行独占 + 居中
    for n in names:
        if not (rx0 <= n['x'] <= rx1) or not (2 <= len(n['v']) <= 14):
            continue
        if theme_prior(n['v']) < 1:
            continue
        if on_stock_row(n['y']):
            continue
        if any(_norm_theme(t['name']) == _norm_theme(n['v']) for t in titles):
            continue
        declare = 0
        for it in items:
            s = it[4].replace(' ', '').lstrip('*＊✱x×X·•・')
            if not (NUM13_RE.match(s) and rx0 <= it[3] <= rx1):
                continue
            if abs(it[2] - n['y']) <= 30 and 0 < it[3] - n['x'] <= 110:
                declare = int(s)
                break
        if declare <= 0:
            continue
        titles.append({'y': n['y'], 'x': n['x'], 'name': n['v'],
                       'declare': declare, 'src': 'rescue'})

    # ---- 标题统一清理：居中 + 行独占 + 同名去重 ----
    cleaned, seen_t = [], set()
    for t in titles:
        if not (tx0 <= t['x'] <= tx1):
            continue
        if on_stock_row(t['y']):
            continue
        k = _norm_theme(t['name'])
        if not k or k in seen_t:
            continue
        seen_t.add(k)
        cleaned.append(t)
    titles = cleaned
    return titles, codes, times, streaks, keywords, names, amounts, prof


def pick(cands, y, x_min=None, dx=ROW_TOL, right_only=False):
    """在 y 容差内挑离 x 最近的候选；right_only 时只取右侧"""
    best, bd = None, None
    for c in cands:
        if abs(c['y'] - y) > dx:
            continue
        if right_only and c['x'] < x_min:
            continue
        d = abs(c['x'] - x_min) if x_min is not None else abs(c['y'] - y)
        if bd is None or d < bd:
            best, bd = c, d
    return best


def parse_rows(items, pool=None, width=1921):
    """按坐标还原「主题 -> 个股明细」

    与旧版的关键区别（旧版用『最近匹配』，会把成交额当关键词、把上一行名称串到本行）：
      ① 先按 (文本, y, x) 去重，消掉重叠切片产生的双份 OCR
      ② 用格式明确的字段（时间/成交额/连板）反推各列中心 x —— 数据驱动，不写死列宽
      ③ 以 6 位代码为行锚点，行区间 = 相邻锚点的中点，保证每个字段只归属一行
      ④ 关键词只从「时间列右侧那一列」取，并排除成交额 / 价格 / 时间 / 连板格式
      ⑤ 名称以涨停池为准（100% 准确），OCR 名称仅作兜底
    """
    items = dedupe_items(items)
    prof = column_profile(items)                     # 列位置全图一致，用全局数据算一次

    # ★ 长图常被拆成多张（如 3 张），**每张图的 y 都从 0 开始**。
    #   必须按图片分组、在单张图内完成「标题 ↔ 股票」归属，否则多张图的
    #   y 会互相穿插，标题与股票全部错配（这是归属错乱的总根源）。
    by_file = {}
    for it in items:
        by_file.setdefault(it[0], []).append(it)
    files = sorted(by_file.keys(), key=_file_key)
    print('  图片 %d 张：%s' % (len(files), '、'.join(
        '%s(%d 项)' % (f, len(by_file[f])) for f in files)))
    print('  列定位：时间x=%s 成交额x=%s 连板x=%s'
          % (prof['time'], prof['amount'], prof['streak']))

    themes, by_name = [], {}
    kw_used, name_used = set(), set()
    carry = None          # 跨图片延续的主题名（上一张图最后一个主题）

    for fn in files:
        sub = by_file[fn]
        titles, codes, times, streaks, keywords, names, amounts, _ = classify(sub, width, prof)
        titles.sort(key=lambda t: t['y'])
        codes.sort(key=lambda c: (c['y'], c['x']))
        print('  [%s] 标题 %d 个 / 代码 %d 个' % (fn, len(titles), len(codes)))
        print('        标题：%s' % '；'.join(
            '%s[y=%d,%s,图注%d]' % (t['name'], t['y'], t.get('src', '?'), t.get('declare', 0))
            for t in titles) or '        标题：（无）')

        for t in titles:
            if t['name'] not in by_name:
                by_name[t['name']] = {'name': t['name'], 'declare': t['declare'], 'stocks': []}
                themes.append(by_name[t['name']])

        # 以代码为行锚点，用相邻锚点的中点切分上下边界
        rows = []
        for i, c in enumerate(codes):
            prev = codes[i - 1]['y'] if i > 0 else None
            nxt = codes[i + 1]['y'] if i + 1 < len(codes) else None
            top = (prev + c['y']) / 2 if prev is not None else c['y'] - ROW_H
            bot = (c['y'] + nxt) / 2 if nxt is not None else c['y'] + ROW_H
            rows.append({'code': c['v'], 'y': c['y'], 'x': c['x'], 'top': top, 'bot': bot})

        def in_row(arr, r):
            return [a for a in arr if r['top'] <= a['y'] <= r['bot']]

        def theme_of(y, _titles=titles, _carry=carry):
            """本图内上方最近的标题；本图标题之前的部分延续上一张图的主题"""
            cur = None
            for t in _titles:
                if t['y'] <= y + 10:
                    cur = t
                else:
                    break
            if cur is None and _carry:
                return by_name.get(_carry)
            return cur

        for r in rows:
            th = theme_of(r['y'])
            if th is None:
                continue

            tms = in_row(times, r)
            tm = min(tms, key=lambda a: abs(a['x'] - (prof['time'] or a['x']))) if tms else None
            sts = in_row(streaks, r)
            st = min(sts, key=lambda a: abs(a['x'] - (prof['streak'] or a['x']))) if sts else None

            # 关键词：只取「时间列右侧那一列」，排除成交额/价格/时间/连板
            # 注意：去重键用「坐标+文本」，不能用 id(对象) ——
            # dedupe_items 丢弃的对象会被 GC，其 id 会被后续新对象复用，导致误判为已取用
            kws = [k for k in in_row(keywords, r)
                   if not is_field_noise(k['v'].replace(' ', ''))
                   and (k['y'], k['x'], k['v']) not in kw_used]
            kw = None
            if kws:
                # 关键词列常是多行，OCR 会拆成多块 → 按纵向顺序拼回「A+B+C」
                kws.sort(key=lambda a: (a['y'], a['x']))
                raw = []
                for k in kws:
                    kw_used.add((k['y'], k['x'], k['v']))
                    v = k['v'].strip('+＋ ')
                    if v:
                        raw.append(v)
                # 重叠切片可能同时给出「整串」和「拆分词」→ 丢掉被整串包含的短块。
                # 只有**含 + 的整串块**才有资格吃掉别的块：
                # 否则会把「数据库」这种本身就是短词的独立关键词误删
                # （它恰好是「多模态数据库」的子串）。
                big = [v for v in raw if ('+' in v or '＋' in v)]
                kept = [v for v in raw
                        if v in big or not any(v != w and v in w for w in big)]
                parts = []
                for v in kept:
                    if v not in parts:
                        parts.append(v)
                if parts:
                    kw = {'y': kws[0]['y'], 'x': kws[0]['x'], 'v': '+'.join(parts)}

            # 名称：代码正上方、同一列（水平接近），且未被其他行占用
            nms = [n for n in names
                   if r['y'] - NAME_ABOVE <= n['y'] <= r['y'] + 10
                   and abs(n['x'] - r['x']) <= NAME_DX
                   and (n['y'], n['x'], n['v']) not in name_used]
            nm = max(nms, key=lambda a: a['y']) if nms else None
            if nm is not None:
                name_used.add((nm['y'], nm['x'], nm['v']))

            streak = parse_streak(st['v']) if st else 1

            name = ''
            if pool and pool.get(r['code']):
                name = pool[r['code']]           # 涨停池名称最可靠
            elif nm:
                name = nm['v']

            th_stocks = by_name[th['name']]['stocks']
            if any(s['code'] == r['code'] for s in th_stocks):
                continue
            th_stocks.append({
                'code': r['code'], 'name': name,
                'time': tm['v'] if tm else '',
                'streak': streak,
                'keyword': kw['v'] if kw else '',
            })

        # 下一张图开头的股票（出现在该图第一个标题之前）延续本图最后一个主题
        if themes:
            carry = themes[-1]['name']

    # 归属体检：解析家数与图上标注家数偏差过大，多半是某个主题标题漏识别导致串区
    for th in themes:
        n = len(th['stocks'])
        if th['declare'] and abs(n - th['declare']) > 2:
            print('  [warn] 主题「%s」图上标注 %d 只，实际归入 %d 只 —— 可能漏识别了该主题的标题'
                  % (th['name'], th['declare'], n))

    out = []
    for th in themes:
        st = th['stocks']
        if not st:
            continue
        out.append({
            'name': th['name'],
            'declare': th['declare'],
            'count': len(st),
            'codes': [s['code'] for s in st],
            'stocks': st,
        })
    return out


# ---------------------------------------------------------------- 校验
def fetch_pool(date):
    codes = {}
    for page in range(1, 5):
        try:
            r = get(POOL_API, referer=POOL_REFERER, params={
                'page': page, 'limit': 200, 'field': POOL_FIELDS,
                'filter': 'HS,GEM2STAR', 'order_field': '330324',
                'order_type': 0, 'date': date, '_': 1})
            j = r.json()
            if not j or j.get('status_code') != 0:
                break
            info = (j.get('data') or {}).get('info') or []
            for s in info:
                codes[str(s.get('code', '')).zfill(6)] = s.get('name', '')
            if len(info) < 200:
                break
        except Exception:
            break
    return codes


# ---------------------------------------------------------------- 主流程
def run_one(date, force=False, from_ocr=False, locate_only=False, ocr_only=False):
    t0 = time.time()
    print('=' * 60)
    print('[%s] 开始' % date)
    os.makedirs(THEMES_DIR, exist_ok=True)
    os.makedirs(REVIEW_DIR, exist_ok=True)

    theme_fp = os.path.join(THEMES_DIR, date + '.json')
    if os.path.exists(theme_fp) and not force:
        try:
            old = json.load(open(theme_fp, encoding='utf-8'))
            if old.get('verified'):
                print('  已存在且已校验，跳过（--force 可重跑）')
                return True
        except Exception:
            pass

    post = fetch_post_from_api(date) or find_post(date)
    if not post:
        print('  未找到该日复盘帖。可能原因：① 今天非交易日 ② 官方尚未发布 ③ 上游 401 限制')
        print('  自查建议：Actions 里用 workflow_dispatch 跑一次 --locate-only，看是否有 401 告警；')
        print('            若 GitHub 出口被封，给仓库配变量 REVIEW_API_BASE 指向 Worker 的 /api/review。')
        return False
    print('  帖子 pid=%s 发布=%s 长图=%d 张' % (post['pid'], post['publishedAt'], len(post['imgs'])))

    json.dump({'date': date, 'pid': post['pid'], 'publishedAt': post['publishedAt'],
               'title': post['title'], 'imgs': post['imgs']},
              open(os.path.join(REVIEW_DIR, date + '.json'), 'w', encoding='utf-8'),
              ensure_ascii=False)

    files = download(post, date)
    if not files:
        print('  长图下载失败')
        return False
    if locate_only:
        print('  --locate-only：连通性验证完成（%d 张长图已就绪），跳过 OCR' % len(files))
        return True

    items = load_ocr(date) if from_ocr else None
    if items is not None:
        print('  复用已缓存 OCR：%d 条' % len(items))
    else:
        print('  下载 %d 张，开始 OCR…' % len(files))
        items = []
        for fp in files:
            got = ocr_with_pos(fp)
            print('    %s -> %d 条' % (os.path.basename(fp), len(got)))
            items.extend(got)
        save_ocr(items, date)
        if ocr_only:
            print('  --ocr-only：OCR 缓存已保存（%d 条），跳过解析' % len(items))
            return True

    pool = fetch_pool(date)
    print('  涨停池 %d 只' % len(pool))
    themes = parse_rows(items, pool)
    total = sum(t['count'] for t in themes)
    pool_codes = set(pool.keys())
    all_codes = [c for t in themes for c in t['codes']]
    bad = [c for c in all_codes if pool_codes and c not in pool_codes]
    bse = len(bad)
    verified = bool(pool_codes) and total == len(pool_codes) + bse and bse <= 2
    # 名称回填：官方图未识别出名称时，用涨停池名称补齐
    for t in themes:
        for s in t['stocks']:
            if not s['name'] and pool.get(s['code']):
                s['name'] = pool[s['code']]

    json.dump({'date': date, 'source': 'ocr', 'verified': verified,
               'total': total, 'poolSize': len(pool_codes), 'bse': bse,
               'pid': post['pid'], 'publishedAt': post['publishedAt'],
               'themes': themes},
              open(theme_fp, 'w', encoding='utf-8'), ensure_ascii=False)

    print('  主题 %d 个 / 个股 %d 只 / 涨停池 %d 只 / 池外 %d'
          % (len(themes), total, len(pool_codes), bse))
    for t in themes:
        print('    %-22s %2d 只 (图注 %d)' % (t['name'], t['count'], t['declare']))
    if bad:
        print('    池外代码(多为北交所): %s' % ','.join(bad[:10]))
    print('  verified=%s  用时 %.0fs' % (verified, time.time() - t0))
    return verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')
    ap.add_argument('--range', nargs=2, metavar=('START', 'END'))
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--from-ocr', action='store_true', help='复用 _work/ocr_<date>.json，不重新识图')
    ap.add_argument('--locate-only', action='store_true',
                    help='只定位帖子并下载长图，不做 OCR（秒级完成，用于验证连通性）')
    ap.add_argument('--ocr-only', action='store_true',
                    help='只做 OCR 并缓存到 _work/ocr_<date>.json，跳过解析')
    a = ap.parse_args()

    if a.date:
        dates = [a.date.replace('-', '')]
    elif a.range:
        s = datetime.strptime(a.range[0], '%Y%m%d')
        e = datetime.strptime(a.range[1], '%Y%m%d')
        dates = []
        d = s
        while d <= e:
            if d.weekday() < 5:
                dates.append(d.strftime('%Y%m%d'))
            d += timedelta(days=1)
    else:
        dates = [bj_today()]

    ok = bad = 0
    for d in dates:
        try:
            if run_one(d, a.force, a.from_ocr, a.locate_only, a.ocr_only):
                ok += 1
            else:
                bad += 1
        except Exception as e:
            bad += 1
            print('  EXCEPTION %s: %s' % (d, e))
    print('\n完成：成功 %d / 未通过 %d' % (ok, bad))
    sys.exit(0 if bad == 0 else 2)


if __name__ == '__main__':
    main()
