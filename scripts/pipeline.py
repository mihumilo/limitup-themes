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
# 连板天梯接口：返回**真实连续涨停高度**（height/continue_num）。
# 涨停池的 high_days 是「N天M板」（N 天窗口内 M 次涨停），M 可能因中间断板
# 而大于真实连续高度（例：美盈森「5天3板」实际 2 连板、百花医药「4天3板」实际 2 连板），
# 所以连续高度必须用连板天梯的 continue_num 校准，high_days 只作「N天M板」备注。
CONTINUOUS_API = 'https://data.10jqka.com.cn/dataapi/limit_up/continuous_limit_up'

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
MAX_STREAK = 9                   # 连板数上限；超过这个值基本是 OCR 拼错（如竖排 '3'+'3'→33）
STREAK_WORDS = ('连板', '首板', '天数', '连板天数', '连板数', '几连板')


def parse_streak(sv):
    """从连板列文本取连板数：首板→1；N天M板→M；N连板→N；N板→N。

    无法判定、或明显异常时返回 None（由调用方回落到涨停池值 / 1）。
    踩过的坑：连板列是**竖排**文字，OCR 会把「3」和「3板」拼成 '33'，
    旧逻辑的宽松兜底 `re.match(r'^(\\d{1,2})')` 会直接取成 33 板。"""
    s = str(sv or '').replace(' ', '')
    if not s:
        return None
    if s.startswith('首板'):
        return 1
    m = re.match(r'^(\d{1,2})天(\d{1,2})板$', s)
    if m:
        v = int(m.group(2))
        return v if 1 <= v <= MAX_STREAK else None
    m = re.match(r'^(\d{1,2})连板$', s)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= MAX_STREAK else None
    m = re.match(r'^(\d{1,2})板$', s)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= MAX_STREAK else None
    # 只剩「单个数字」这种残片可信；两位数（'33' / '22'）基本都是竖排粘连 → 不采信
    m = re.match(r'^(\d)$', s)
    if m:
        return int(m.group(1))
    return None


def pool_streak_of(high_days, high_days_value):
    """同花顺 high_days（"4天3板"）→ 从文本取 M（**仅作备注/体检用**）。

    ⚠️ 注意：M 是「N 天窗口内涨停次数」，**不是连续高度**（中间断板时 M > 真实连续高度）。
    看板真实连续高度已改用连板天梯 continue_num（见 fetch_continuous），
    本函数返回值只用于「N天M板」备注与 OCR 差异体检，不再决定档位。
    """
    s = str(high_days or '').replace(' ', '')
    m = re.match(r'^(\d{1,2})天(\d{1,2})板$', s)
    if m:
        k = int(m.group(2))
        if 1 <= k <= MAX_STREAK:
            return k
    if s.startswith('首板'):
        return 1
    v = (int(high_days_value or 0) >> 16) or 1
    return v if 1 <= v <= MAX_STREAK else 1


def fetch_continuous(date):
    """连板天梯 → {code: continue_num}（真实连续高度）。

    天梯只覆盖「连续 2 板及以上」的票；涨停了但不在天梯里 = 前一天没涨停 = 首板（1）。
    失败时返回空 dict —— 调用方回落到「天梯缺失 = 首板 1」的保守口径。"""
    out = {}
    try:
        r = get(CONTINUOUS_API, referer=POOL_REFERER, params={
            'page': 1, 'limit': 200, 'date': date, '_': 1})
        j = r.json()
        if not j or j.get('status_code') != 0:
            return out
        for group in j.get('data') or []:
            h = int(group.get('height') or 0)
            for s in group.get('code_list') or []:
                code = str(s.get('code', '')).zfill(6)
                if code and h:
                    out[code] = h
    except Exception:
        pass
    return out


def hd_note_of(high_days):
    """「N天M板」里 N>M（中间断板）时返回原文备注，否则空串。

    看板用它在个股卡片上标出「4天3板」—— 归属仍是 M 板档，只是提示读者
    这不是一条纯 M 连板。N==M 时不标（与「M连板」同义）。"""
    s = str(high_days or '').replace(' ', '')
    m = re.match(r'^(\d{1,2})天(\d{1,2})板$', s)
    if m and int(m.group(1)) > int(m.group(2)):
        return s
    return ''
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


def is_review_post(post):
    """是否真正的「一图看懂涨停股」复盘长图帖（排除同天的功能宣传帖）。

    ★ 2026-09-24 事故：同花顺同一天会发两类帖 ——
      · 复盘长图帖：标题「一图看懂涨停股…」，图 `xxx_1921_7584_middle.png`（宽1921/高7584）
      · 功能宣传帖：标题「“实时复盘”功能上线啦！」，图 `xxx_475_834_middle.png`（宽475/高834）
      定位只判「发布日期 == 目标日」会命中宣传帖（它还发布得更晚、排在列表更前）→
      下载到 475 宽的缩略图 → OCR 认不出标题和股票代码 → 0 主题 0 个股 → 任务 exit 2。

    判据：图片 URL 里的宽（`_W_H.png` 的 W）≥ 1000 才算长图；宣传小图只有 400~500。
    """
    if not post or not post.get('imgs'):
        return False
    mx = 0
    for u in post['imgs']:
        g = LONG_RE.search(u)
        if g:
            mx = max(mx, int(g.group(1)))
    return mx >= 1000


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
        """逐个抓详情，命中**且是长图帖**才返回；记录是否『全部取不到』以便诊断。

        ★ 只判日期会命中同天的功能宣传帖（图是 475 宽小图）——所以命中后还要过
          is_review_post；当天只有宣传帖时先记进 fallback，继续往下找真复盘帖。
        """
        ok = 0
        fallback = None
        for k, pid in enumerate(pids if limit is None else pids[:limit]):
            post = fetch_post(pid)
            if post:
                ok += 1
                if post['date'] == date:
                    if is_review_post(post):
                        return post
                    if fallback is None:
                        fallback = post
                        print('  [warn] pid %s 日期匹配但不是长图帖（标题「%s」）→ 继续找同天的复盘长图帖'
                              % (pid, (post.get('title') or '')[:36]))
            if not post:
                _probed.append(pid)
        if ok == 0 and pids:
            print('  [warn] %d 个 pid 详情页全部取不到（多为 401：Cookie 失效 / 出口 IP 被封）' % len(pids))
        return fallback

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
    fb = None
    for i in range(0, len(pids), 6):
        for pid in pids[i:i + 6]:
            post = fetch_post(pid)
            if post and post['date'] == date:
                if is_review_post(post):
                    return post
                if fb is None:
                    fb = post
    return fb


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
    """重叠切片会让同一段文字被识别两次。按 (图片, 文本) 分桶，y/x 接近的视为同一次识别。
    阈值放宽到 80/40：切片边缘的 OCR 框常有十几像素抖动，之前 25px 阈值漏掉了部分重复。

    ★ 分桶必须带**图片名**（2026-09-22 修）：官方长图被拆成多张，每张的 y 都从 0 开始。
      只用文本分桶时，A 图 y=2130 的「09:25:00」与 B 图 y=2130 的同名文本
      （dy=0、dx=0）会被判成重复而**丢掉一张图的数据** —— 表现为该股时间/字段整列变空。
    """
    buckets = {}
    for it in sorted(items, key=lambda r: (r[0], r[4], r[2], r[3])):
        lst = buckets.setdefault((it[0], it[4]), [])
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


def merge_vertical_streak(items):
    """合并竖排的连板文本。

    官方图的「连板天数」列是**竖排**（「首」在上、「板」在下，「2」在上、「板」在下），
    OCR 会把它切成两个单字块 → 拼不出「首板」/「2板」→ 连板数解析失败 →
    回落到涨停池的 high_days（口径与图不同，会把首板错标成 2 板）。
    这里把同一列上下紧邻、且拼接后正好构成连板格式的两个短块合并。

    ★ 排序键必须带**图片名**（2026-09-22 修）：多张图的 y 都从 0 开始，
      只按 (x, y) 排序会让不同图的块交错误判为「上下紧邻」而错误合并。
    """
    order = sorted(range(len(items)), key=lambda i: (items[i][0], items[i][3], items[i][2]))
    out = list(items)
    drop, taken = set(), set()
    for a in range(len(order)):
        ia = order[a]
        if ia in drop or ia in taken:
            continue
        sa = out[ia][4].replace(' ', '')
        if not sa or len(sa) > 4:
            continue
        for b in range(a + 1, min(a + 6, len(order))):
            ib = order[b]
            if ib in drop or ib in taken:
                continue
            if out[ia][0] != out[ib][0]:                 # 必须同一张图
                continue
            if abs(out[ia][3] - out[ib][3]) > 70:        # 必须同一列
                continue
            sb = out[ib][4].replace(' ', '')
            if not sb or len(sb) > 4:
                continue
            dy = out[ib][2] - out[ia][2]
            if not (0 < dy < 90):                        # 必须紧邻下方
                continue
            joined = sa + sb
            if STREAK_RE.match(joined):
                out[ia] = (out[ia][0], out[ia][1],
                           (out[ia][2] + out[ib][2]) // 2, out[ia][3], joined)
                drop.add(ib)
                break
        taken.add(ia)
    return [it for i, it in enumerate(out) if i not in drop]


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


def classify(items, width=1921, prof=None, stats=None):
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
    w = width or 1921                        # 调用方必须传**本图**的真实宽度
    tx0, tx1 = w * 0.30, w * 0.70            # 标题居中带
    rx0, rx1 = w * 0.35, w * 0.65            # 救援候选的居中带（更严）
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

    # ★ 行间距随图缩放：窄图（实测 730~1160 宽）上文字更小、行更密，
    #   写死 120px 会把真标题判成"落在股票行上" —— 这是 8/12、8/17 那几个窄图
    #   日期标题大量丢失的直接原因（宽图 1900+ 上 120px 完全够用，所以一直没暴露）。
    #   改成用「相邻股票代码中位数间距」的比例值：宽图算出来≈120，与历史经验一致；
    #   窄图自动收紧到 50~120 之间。夹住上下限，避免过松（误判正文）或过紧（漏标题）。
    _spac = sorted(code_ys[i + 1] - code_ys[i] for i in range(len(code_ys) - 1))
    # 门槛取 2：3 只股票就能给出 2 个间距，够了（原来写 3，需要 4 只股票才生效）
    if len(_spac) >= 2:
        _med_spac = _spac[len(_spac) // 2]
        row_gap = max(50, min(TITLE_MIN_GAP, int(_med_spac * 0.25)))
    else:
        _med_spac, row_gap = None, TITLE_MIN_GAP
    if stats is not None:
        stats['row_gap'] = row_gap
        stats['med_spacing'] = _med_spac

    def on_stock_row(y):
        return dist_to_code(y) < row_gap

    # ---- 标题兜底（rescue）：标题被 OCR 切碎时（如「大消费」+「*11」分成两块），
    #      在中央区域找短中文块救回来。硬条件：
    #      ① 行独占 + 「名称 + 图注」整体居中（几何特征，不随图宽变）
    #      ② 必带图注家数「*N」—— 上一版的假标题（海峡两岸 / 控制权变更 / 军工通信 /
    #         光伏玻璃）全是图注 0，被这条全部拒掉
    #      ③ 词表命中：**带星号的图注**（`*11`）本身就是强信号，这种情况不再要求词表
    #         （否则 8 月那种"新主题 + 标题被切碎"的组合永远救不回来）；
    #         不带星号时仍要求命中词表，避免把正文里的短词误当标题
    #  ★ 候选来源必须是 names + keywords：标题「大消费」落在中央，横坐标正好在
    #    关键词列的区间里，classify 会把它收进 keywords —— 只看 names 就永远找不到它。
    _rescue_cands = list(names) + [k for k in keywords
                                   if (k['y'], k['x'], k['v']) not in
                                   {(n['y'], n['x'], n['v']) for n in names}]
    for n in _rescue_cands:
        if not (rx0 <= n['x'] <= rx1) or not (2 <= len(n['v']) <= 14):
            continue
        if on_stock_row(n['y']):
            continue
        if any(_norm_theme(t['name']) == _norm_theme(n['v']) for t in titles):
            continue
        prior = theme_prior(n['v'])
        declare = 0
        starred = False
        for it in items:
            raw = it[4].replace(' ', '')
            s = raw.lstrip('*＊✱x×X·•・')
            if not NUM13_RE.match(s):
                continue
            if abs(it[2] - n['y']) > 30:
                continue
            dx = it[3] - n['x']
            # 图注在标题名右侧、距离不超过图宽的 1/4（容纳「名 + *N」整块）
            if not (0 < dx <= w * 0.25):
                continue
            # ★ 真正的判据：「标题名 + 图注」的**整体中点**必须落在这张图的居中带里。
            #   以前写的是「名称到图注距离 ≤110px」——那是拿 1921 宽图的字号当标准，
            #   窄图（960/1056/1152）上「大消费 + *11」的间距能到 200px 上下，
            #   于是真标题被整条挡掉（8/12 顶部那个标题就是这么丢的）。
            #   而「整体居中」是不随图宽变化的几何特征，用它才稳。
            if not (tx0 <= (n['x'] + it[3]) / 2.0 <= tx1):
                continue
            declare = int(s)
            starred = raw[:1] in ('*', '＊', '✱')
            break
        if declare <= 0:
            continue
        if not starred and prior < 1:
            continue          # 没有星号图注时，仍要命中历史主题词表才认
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


def image_width(fn):
    """该图片的真实像素宽度。

    ★ 为什么必须按图算：长图的宽度并不固定 —— 实测同一天的三张图可能是
      1152 / 1056 / 1152，也有 960、1440、1921 各种宽度。
      以前这里写死 1921，于是「标题居中带」= 576~1345：
        · 宽 1152 的图，正确带是 346~806 → 只有 576~806 这段还能命中
        · 宽 1056 的图，正确带是 317~739 → 标题（x≈528）**全部落在带外**
      后果：那天的标题被整张漏掉，个股全被塞进上一个主题，
      家数校验必然不过（verified=false）—— 8/12、8/17、8/18 就是这么失败的。
    """
    try:
        with Image.open(fn) as im:
            return int(im.size[0])
    except Exception:
        return None


def image_width_fallback(sub_items):
    """图文件不在时的退化方案（--from-ocr 复用缓存、或图已清理）：
    用本图所有文字块的 x 上限反推图宽（右侧一般还剩一点留白）。"""
    xs = [it[3] for it in sub_items]
    if not xs:
        return 1921
    return int(max(xs) * 1.06)


def parse_rows(items, pool=None, width=None):
    """按坐标还原「主题 -> 个股明细」

    与旧版的关键区别（旧版用『最近匹配』，会把成交额当关键词、把上一行名称串到本行）：
      ① 先按 (文本, y, x) 去重，消掉重叠切片产生的双份 OCR
      ② 用格式明确的字段（时间/成交额/连板）反推各列中心 x —— 数据驱动，不写死列宽
      ③ 以 6 位代码为行锚点，行区间 = 相邻锚点的中点，保证每个字段只归属一行
      ④ 关键词只从「时间列右侧那一列」取，并排除成交额 / 价格 / 时间 / 连板格式
      ⑤ 名称以涨停池为准（100% 准确），OCR 名称仅作兜底
    """
    items = dedupe_items(items)
    items = merge_vertical_streak(items)             # 连板列是竖排，先合并单字块
    items = dedupe_items(items)                      # 合并后可能重复（重叠切片各合并一次）
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
    used_pool, used_ocr = [0], [0]     # 明细字段来源统计
    streak_conflict = []               # 图上连板数与池不一致的样本（口径差异，便于核对）
    streak_bad = []                    # 连板列读不出来（竖排粘连等）→ 回落池值的样本
    carry = None          # 跨图片延续的主题名（上一张图最后一个主题）

    for fn in files:
        sub = by_file[fn]
        # ★ 每张图各自算宽度（不能全图共用一个值，见 image_width 的说明）
        w_img = width or image_width(fn) or image_width_fallback(sub)
        # ★ 列定位同样按图算：注释曾假设"列位置全图一致"，但实测同一天的三张图
        #   宽度能差 27%（如 928 / 730 / 915），全局列位置会让窄图的「名称/关键词」
        #   分流错位，标题碎片进错桶 → rescue 找不到它。单图样本不足时回落全局值。
        p_img = column_profile(sub)
        p_use = p_img if (p_img and p_img.get('time') is not None) else prof
        st = {}
        titles, codes, times, streaks, keywords, names, amounts, _ = classify(sub, w_img, p_use, st)
        titles.sort(key=lambda t: t['y'])
        codes.sort(key=lambda c: (c['y'], c['x']))
        print('  [%s] 宽 %d 标题 %d 个 / 代码 %d 个'
              '  ｜列x 时%s/额%s/板%s  ｜行距中位 %s → 独占行判据 %dpx'
              % (fn, w_img, len(titles), len(codes),
                 p_use.get('time'), p_use.get('amount'), p_use.get('streak'),
                 st.get('med_spacing') if st.get('med_spacing') is not None else '-',
                 st.get('row_gap', TITLE_MIN_GAP)))
        print('        标题：%s' % '；'.join(
            '%s[y=%d,%s,图注%d]' % (t['name'], t['y'], t.get('src', '?'), t.get('declare', 0))
            for t in titles) or '        标题：（无）')
        # 自检：图里有卡片却一个标题都没有 → 多半是宽度/版式导致的标题漏识别，
        # 一旦发生，这张图的个股会全部被塞进上一个主题，家数校验必然失败。
        if len(codes) >= 5 and not titles:
            print('    [warn] 本图有 %d 个代码但标题 0 个 —— 可能是标题居中带与实际图宽不符'
                  '（当前按宽 %d 计算），或该图版式特殊' % (len(codes), w_img))

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

            # ---- 明细字段：以涨停池为准，OCR 兜底 ----
            # 池的 reason_type / last_limit_up_time / high_days 与官方图的
            # 涨停关键词列 / 最终涨停时间列 / 连板天数列同源（已逐只核对），
            # 且不会像 OCR 那样把「涨停原因内容」的残片混进关键词。
            ps = pool_get(pool, r['code']) or {}
            name = ps.get('name') or (nm['v'] if nm else '')
            # 连板数：**一律以同花顺连板天梯的 continue_num 为准**（真实连续高度，需求 1）。
            # 池里有这只票 → 直接用池值（已由连板天梯校准），图上 OCR 值只用于**体检**（不一致就打印）；
            # 池里没有（北交所等） → 才回落图上 OCR 值，并过滤竖排粘连产生的异常值（'33'/'22'）。
            ocr_streak = (parse_streak(st['v']) or 0) if st else 0
            pool_streak = ps.get('streak') or 0
            if ocr_streak and pool_streak and ocr_streak != pool_streak:
                streak_conflict.append('%s %s 图=%d 池=%d'
                                        % (r['code'], name, ocr_streak, pool_streak))
            if st and not ocr_streak:
                streak_bad.append('%s %s 图上连板列=%r' % (r['code'], name, st['v']))
            streak = pool_streak or ocr_streak or 1
            hd_v = ps.get('hd') or ''
            gap_v = ps.get('gap') or ''
            time_v = ps.get('time') or (tm['v'] if tm else '')
            kw_v = ps.get('reason') or (kw['v'] if kw else '')
            if ps:
                used_pool[0] += 1
            else:
                used_ocr[0] += 1

            th_stocks = by_name[th['name']]['stocks']
            if any(s['code'] == r['code'] for s in th_stocks):
                continue
            th_stocks.append({
                'code': r['code'], 'name': name,
                'time': time_v,
                'streak': streak,
                'hd': hd_v,
                'gap': gap_v,
                'keyword': kw_v,
            })

        # 下一张图开头的股票（出现在该图第一个标题之前）延续本图最后一个主题
        if themes:
            carry = themes[-1]['name']

    print('  明细字段来源：涨停池 %d 只 / OCR 兜底 %d 只' % (used_pool[0], used_ocr[0]))
    if streak_bad:
        print('  连板列无法判定 %d 只（已回落池值；样例：%s）'
              % (len(streak_bad), '；'.join(streak_bad[:3])))
    if streak_conflict:
        print('  连板口径差异 %d 只（看板最终以池值为准；样例：%s）'
              % (len(streak_conflict), '；'.join(streak_conflict[:3])))

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
def ts_to_bj(ts):
    """Unix 秒 → 北京时间 HH:MM:SS"""
    n = int(ts or 0)
    if not n:
        return ''
    d = datetime.fromtimestamp(n, timezone.utc) + timedelta(hours=8)
    return d.strftime('%H:%M:%S')


def pool_get(pool, code):
    """兼容两种池结构：{code: name}（旧/测试用）与 {code: {...}}（fetch_pool 产出）"""
    v = (pool or {}).get(code)
    if v is None:
        return None
    return v if isinstance(v, dict) else {'name': v}


def fetch_pool(date):
    """涨停池：既用于交叉校验，也是**明细字段的权威来源**。

    关键事实（已逐只核对）：池的 reason_type == 官方图「涨停关键词」列、
    last_limit_up_time == 「最终涨停时间」列、high_days == 「连板天数」列，
    三者与官方图完全同源。所以明细字段以池为准，OCR 仅作兜底 ——
    OCR 读「涨停原因内容」那一大段时会产生「托。」「560.06%。」这类残片。

    ⚠️ 连板高度例外：high_days 的「N天M板」是窗口内涨停次数，**不是连续高度**，
    故 streak 改用**连板天梯 continue_num**（fetch_continuous）校准；
    天梯里没有（已断板）→ 首板 1。high_days 原文与「N>M」备注仍保留在 hd/gap。
    """
    cont = fetch_continuous(date)
    out = {}
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
                code = str(s.get('code', '')).zfill(6)
                hd = s.get('high_days') or ''
                # 真实连续高度：连板天梯优先，天梯缺失 = 首板 1
                st = cont.get(code) or 1
                out[code] = {
                    'name': s.get('name', '') or '',
                    'reason': s.get('reason_type', '') or '',
                    'time': ts_to_bj(s.get('last_limit_up_time') or s.get('first_limit_up_time')),
                    'streak': st,
                    'hd': hd,                      # high_days 原文（"4天3板"）
                    'gap': hd_note_of(hd),         # N>M 时的卡片备注
                }
            if len(info) < 200:
                break
        except Exception:
            break
    return out


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
    # ★ 池外股的预期构成是**北交所**（fuyao 涨停池不含北交所，官方图上有 → 全部落到池外）。
    #   30 天实测：21 只池外股全部 920 开头，无一例外。
    #   旧规则 `bse <= 2` 在北交所活跃日（池外 3+ 只）会误判 verified=false
    #   （20260901/20260902 就是这么误伤的，bse=3 全是 920 开头）。
    #   真正要防的是「OCR 把不存在的沪深代码识别进图」——那才是转录错误：
    #   池外股里只要出现非北交所前缀（60/68/00/30 之外又不是北交所前缀）就不通过。
    bse_non_bj = [c for c in bad if not c.startswith(('92', '83', '87', '43'))]
    # ★★ 2026-09-25 修复：旧逻辑「只统计、不清理」——噪声照样写进 JSON 污染数据，
    #     同时又因「池外出现非北交所代码」判 verified=False，把整天 100+ 只有效数据作废。
    #     实测（2026-06~07 批量回填 25 个失败日）：26 个这类代码 **100% 不在当天涨停池** ——
    #     000004/002808/600696 在多日反复出现（真实股票不可能天天涨停），
    #     还有 060000/066009/696000/660000/606009/966009 这类前缀非法的错位识别。
    #     结论：它们是 OCR 把成交额、序号等数字误当成代码列的**噪声**，不是真股票。
    #     现在改为：**先把噪声真正剔除，再判定**（数据干净 + 不再误废）。
    if bse_non_bj:
        drop = set(bse_non_bj)
        kept = []
        for t in themes:
            t['stocks'] = [s for s in t['stocks'] if s['code'] not in drop]
            t['codes'] = [c for c in t['codes'] if c not in drop]
            t['count'] = len(t['codes'])
            if t['count'] > 0:
                kept.append(t)
        themes = kept
        total = sum(t['count'] for t in themes)
        bse = bse - len(bse_non_bj)          # 剩下的池外都是北交所（预期内）
    # 容差：OCR 难免漏掉个别行，只要不是成片漏解析就认为可用（100 只以内允许差 1 只）
    tol = max(1, len(pool_codes) // 100)
    verified = bool(pool_codes) and abs(total - (len(pool_codes) + bse)) <= tol
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

    print('  主题 %d 个 / 个股 %d 只 / 涨停池 %d 只 / 池外 %d（全为北交所）'
          % (len(themes), total, len(pool_codes), bse))
    if bse_non_bj:
        print('    [已剔除 OCR 噪声代码 %d 个] %s'
              % (len(bse_non_bj), ','.join(bse_non_bj[:10])))
    for t in themes:
        print('    %-22s %2d 只 (图注 %d)' % (t['name'], t['count'], t['declare']))
    if bad:
        print('    池外代码(北交所): %s' % ','.join([c for c in bad if c not in set(bse_non_bj)][:10]))
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
