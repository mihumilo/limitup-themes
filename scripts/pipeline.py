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
NAME_ABOVE = 60                  # 名称在代码上方多少像素内
NAME_DX = 150                    # 名称与代码的水平距离上限

CODE_RE = re.compile(r'^\d{6}$')
TIME_RE = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?$')
STREAK_RE = re.compile(r'^(首板|\d{1,2}\s*连板|\d{1,2}\s*板)$')
TITLE_RE = re.compile(r'^(.{2,20}?)[\s]*[\*＊✱x×X][\s]*(\d{1,3})$')
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


def get(url, referer=None, params=None, timeout=30):
    h = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'}
    if referer:
        h['Referer'] = referer
    return requests.get(url, headers=h, params=params, timeout=timeout)


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
    try:
        r = get('https://t.10jqka.com.cn/pid_%s.shtml' % pid,
                referer=CIRCLE, timeout=25)
        if r.status_code != 200:
            return None
        html = r.text
        dm = re.search(r'class="detail-date[^"]*">([^<]{8,12})<', html)
        if not dm:
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
    """自然日->交易日换算估页码，抽样探测定位，再小范围精扫"""
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
def classify(items):
    """把 OCR 文本按类型分流：主题标题 / 代码 / 时间 / 连板 / 关键词 / 候选名称"""
    titles, codes, times, streaks, keywords, names = [], [], [], [], [], []
    for it in items:
        f, si, y, x, t = it
        s = t.replace(' ', '')
        if not s:
            continue
        m = TITLE_RE.match(s)
        if m and 2 <= len(m.group(1).strip()) <= 20:
            titles.append({'y': y, 'name': m.group(1).strip(), 'declare': int(m.group(2))})
            continue
        if CODE_RE.match(s):
            codes.append({'y': y, 'x': x, 'v': s})
            continue
        if TIME_RE.match(s):
            times.append({'y': y, 'x': x, 'v': s})
            continue
        if STREAK_RE.match(s.replace(' ', '')):
            streaks.append({'y': y, 'x': x, 'v': s})
            continue
        # 关键词：短串、含 + 号（图中用 + 连接多个关键词），或纯中文短词
        if ('+' in s or '＋' in s) and 2 <= len(s) <= 30 and not CODE_RE.match(s):
            keywords.append({'y': y, 'x': x, 'v': s})
            continue
        if CJK_RE.match(s) and not s.replace('.', '').isdigit():
            names.append({'y': y, 'x': x, 'v': s})
    return titles, codes, times, streaks, keywords, names


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


def parse_rows(items):
    """按坐标还原「主题 -> 个股明细」"""
    titles, codes, times, streaks, keywords, names = classify(items)
    titles.sort(key=lambda t: t['y'])
    codes.sort(key=lambda c: c['y'])

    def theme_of(y):
        cur = None
        for t in titles:
            if t['y'] <= y + 10:
                cur = t
            else:
                break
        return cur

    themes = []
    by_name = {}
    for t in titles:
        if t['name'] not in by_name:
            by_name[t['name']] = {'name': t['name'], 'declare': t['declare'], 'stocks': []}
            themes.append(by_name[t['name']])

    for c in codes:
        th = theme_of(c['y'])
        if th is None:
            continue
        # 名称：代码上方、水平接近的最近中文字段
        nm = None
        bd = None
        for n in names:
            dy = c['y'] - n['y']
            if not (0 < dy <= NAME_ABOVE):
                continue
            if abs(n['x'] - c['x']) > NAME_DX:
                continue
            if bd is None or dy < bd:
                nm, bd = n, dy
        tm = pick(times, c['y'], c['x'], ROW_TOL, True)
        st = pick(streaks, c['y'], c['x'], ROW_TOL, True)
        kw = pick(keywords, c['y'], c['x'], ROW_TOL, True)
        if kw is None:
            # 关键词列有时不带 +，退化为同行右侧最近的中文短词
            cand = [n for n in names if abs(n['y'] - c['y']) <= ROW_TOL
                    and n['x'] > c['x'] + 200 and n['x'] < c['x'] + 900]
            if cand:
                kw = min(cand, key=lambda n: abs(n['x'] - c['x']))
        streak = 1
        if st:
            sv = st['v'].replace(' ', '')
            if sv.startswith('首板'):
                streak = 1
            else:
                mm = re.match(r'^(\d{1,2})', sv)
                if mm:
                    streak = int(mm.group(1))
        th_stocks = by_name[th['name']]['stocks']
        if any(s['code'] == c['v'] for s in th_stocks):
            continue
        th_stocks.append({
            'code': c['v'],
            'name': nm['v'] if nm else '',
            'time': tm['v'] if tm else '',
            'streak': streak,
            'keyword': kw['v'] if kw else '',
        })

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
def run_one(date, force=False, from_ocr=False):
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

    post = find_post(date)
    if not post:
        print('  未找到该日复盘帖（非交易日 / 未发布 / 上游限制）')
        return False
    print('  帖子 pid=%s 发布=%s 长图=%d 张' % (post['pid'], post['publishedAt'], len(post['imgs'])))

    json.dump({'date': date, 'pid': post['pid'], 'publishedAt': post['publishedAt'],
               'title': post['title'], 'imgs': post['imgs']},
              open(os.path.join(REVIEW_DIR, date + '.json'), 'w', encoding='utf-8'),
              ensure_ascii=False)

    items = load_ocr(date) if from_ocr else None
    if items is not None:
        print('  复用已缓存 OCR：%d 条' % len(items))
    else:
        files = download(post, date)
        if not files:
            print('  长图下载失败')
            return False
        print('  下载 %d 张，开始 OCR…' % len(files))
        items = []
        for fp in files:
            got = ocr_with_pos(fp)
            print('    %s -> %d 条' % (os.path.basename(fp), len(got)))
            items.extend(got)
        save_ocr(items, date)

    themes = parse_rows(items)
    total = sum(t['count'] for t in themes)
    pool = fetch_pool(date)
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
            if run_one(d, a.force, a.from_ocr):
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
