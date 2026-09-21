#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据备份同步 —— 把本仓库的产出同步到一个或多个备份目标。

设计原则
--------
1. **不绑定任何特定平台**：目标是「本地目录」或「任意 git 远端」，由配置指定。
2. **核心逻辑独立**：只依赖标准库 + 仓库里已有的 requests（仅用于写结果文件时的占位，
   实际上这里是纯 subprocess/文件操作），不引入新依赖。
3. **一个目标失败不影响其它目标**，最后统一汇总。
4. **日志 + 结果可追溯**：控制台分级输出；结果写 `_work/backup_result.json`；
   在 GitHub Actions 里还会写 `$GITHUB_STEP_SUMMARY`（作业页直接能看到表格）。

目标类型
--------
* `local`  本地目录（或已挂载的网盘 / NAS 目录）
  → 生成快照 `<path>/<YYYYMMDD-HHMMSS>/`，再把 `latest/` 指向最新一份，
    按 `keep_snapshots` 保留最近 N 份。

* `git`    另一个 git 远端（自建服务 / 任意代码平台）
  → **每次全新 clone 到临时目录**（仓库很小，几百 KB~几 MB），
    复制文件 → commit → push。
  ★ 为什么不用「在本地仓库加 remote 再 push」：GitHub Actions 的 checkout
    默认是浅克隆（fetch-depth=1），浅仓库推到别的远端会被拒
    （`shallow update not allowed`）。全新 clone 从根上绕开这个问题。

配置
----
默认读仓库根的 `backup.config.json`（可用 `--config` 指定）。
字符串里的 `${VAR}` 会替换成同名环境变量 —— **凭据一律走环境变量，不写进配置文件**。
未取到环境变量的条目会被跳过并告警（不会带着空 token 去请求）。

用法
----
    python scripts/backup_sync.py                 # 按配置同步所有启用的目标
    python scripts/backup_sync.py --dry           # 只打印计划，不实际执行
    python scripts/backup_sync.py --only nas      # 只跑指定名字的目标
    python scripts/backup_sync.py --verbose       # 打印 git 命令输出（排障用）

退出码
------
    0 = 全部成功（或没有启用任何目标）
    1 = 至少一个目标失败
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_CONFIG = os.path.join(ROOT, 'backup.config.json')
DEFAULT_INCLUDE = ['themes', 'review', 'nontrading-days.json']
RESULT_PATH = os.path.join(ROOT, '_work', 'backup_result.json')

_log_lines = []


# ---------------------------------------------------------------- 日志
def log(level, msg):
    line = '[%s] %-5s %s' % (datetime.now().strftime('%H:%M:%S'), level, msg)
    print(line, flush=True)
    _log_lines.append(line)


def info(m):
    log('INFO', m)


def ok(m):
    log('OK  ', m)


def warn(m):
    log('WARN', m)


def err(m):
    log('ERR ', m)


def _mask(url):
    """把 URL 里的凭据打码，避免泄漏到日志"""
    if not url:
        return url
    if '@' in url and '://' in url:
        scheme, rest = url.split('://', 1)
        if '@' in rest:
            userinfo, host = rest.split('@', 1)
            if ':' in userinfo:
                user = userinfo.split(':', 1)[0]
            else:
                user = userinfo
            return '%s://%s:***@%s' % (scheme, user, host)
    return url


def _expand(v):
    """把字符串里的 ${VAR} 替换成环境变量。返回 (结果, 缺失的变量列表)"""
    if not isinstance(v, str):
        return v, []
    missing = []
    out = v
    for _ in range(10):
        if '${' not in out:
            break
        i = out.index('${')
        j = out.find('}', i)
        if j < 0:
            break
        name = out[i + 2:j]
        val = os.environ.get(name)
        if val is None or val == '':
            missing.append(name)
            val = ''
        out = out[:i] + val + out[j + 1:]
    return out, missing


# ---------------------------------------------------------------- 配置
def load_config(path):
    if not os.path.exists(path):
        return None, '配置文件不存在：%s' % path
    try:
        cfg = json.load(open(path, encoding='utf-8'))
    except Exception as e:
        return None, '配置文件解析失败：%s' % e
    if not isinstance(cfg, dict):
        return None, '配置文件顶层必须是对象'
    return cfg, None


def targets_of(cfg, only=None):
    cfg = cfg or {}
    include = cfg.get('include') or DEFAULT_INCLUDE
    out = []
    for t in (cfg.get('targets') or []):
        if not isinstance(t, dict):
            warn('跳过一条无法识别的目标配置（不是对象）')
            continue
        name = t.get('name') or '(未命名)'
        if t.get('enabled') is False:
            info('目标 %-12s 已在配置里禁用，跳过' % name)
            continue
        if only and name != only:
            continue
        out.append((name, t, include))
    return out


# ---------------------------------------------------------------- 源文件
def collect_sources(include):
    """返回 [(相对路径, 绝对路径)]，不存在的一律跳过并告警"""
    srcs = []
    for rel in include:
        abs_p = os.path.join(ROOT, rel)
        if not os.path.exists(abs_p):
            warn('待备份路径不存在，跳过：%s' % rel)
            continue
        srcs.append((rel, abs_p))
    if not srcs:
        warn('没有任何待备份内容 —— themes/ 是空的？')
    return srcs


def _copy(srcs, dst_root):
    """把源复制进 dst_root，返回复制的条目数"""
    n = 0
    for rel, abs_p in srcs:
        d = os.path.join(dst_root, rel)
        if os.path.isdir(abs_p):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(abs_p, d)
        else:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(abs_p, d)
        n += 1
    return n


def _manifest(srcs):
    """★ 这里**不能放时间戳**：git 目标靠 `git diff --cached` 判断有没有变化，
       清单里带个每次都变的字段 → 每次都会被判定为"有更新" → 每次都提交一版空提交。
       时间信息由快照目录名（local）和 commit message（git）承载就够了。"""
    m = {
        'source_commit': _git(['rev-parse', 'HEAD'], cwd=ROOT) or '',
        'items': {},
    }
    for rel, abs_p in srcs:
        if os.path.isdir(abs_p):
            try:
                m['items'][rel] = len([f for f in os.listdir(abs_p)
                                       if f.endswith('.json')])
            except Exception:
                m['items'][rel] = -1
        else:
            m['items'][rel] = 1
    return m


def _mask_cmd(args):
    """给命令行里的 URL 打码后再打印（url 里可能带 token）"""
    return ' '.join(_mask(a) if ('://' in str(a)) else str(a) for a in args)


def _git(args, cwd=None, verbose=False, check=False, silent=False):
    """跑 git 命令。失败返回 None（check=True 时抛异常）。

    ★ 失败时**必须把 git 的原话打出来**（打码后）—— 否则像
      "transport 'file' not allowed" 这类错误会被 --quiet 吞掉，完全没法排障。
      silent=True 用于「失败本来就是预期结果」的探测（如判断远端是否为空仓库）。"""
    cmd = ['git'] + args
    if verbose:
        info('$ ' + _mask_cmd(cmd))
    try:
        p = subprocess.run(cmd, cwd=cwd or ROOT, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=600)
        out = (p.stdout or b'').decode('utf-8', 'replace').strip()
        if p.returncode != 0:
            msg = out or '(git 无输出)'
            if not silent:
                warn('git %s 失败 → %s' % (_mask_cmd(args), msg[:400]))
            if check:
                raise RuntimeError(msg)
            return None
        return out
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError('git %s 超时（600s）' % ' '.join(args))
        return None
    except FileNotFoundError:
        if check:
            raise RuntimeError('找不到 git 命令')
        return None


# ---------------------------------------------------------------- 目标：本地目录
def sync_local(name, t, srcs, dry, verbose):
    raw = t.get('path') or ''
    path, miss = _expand(raw)
    if miss:
        return False, '缺少环境变量：%s' % ', '.join(miss)
    if not path:
        return False, '未配置 path'
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        warn('当前运行在 GitHub Actions 上：local 目标写的是**临时 runner 的磁盘**，'
             '作业结束后会被丢弃（如需留存请改用 git 目标）')

    keep = int(t.get('keep_snapshots') or 7)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    snap = os.path.join(path, stamp)
    latest = os.path.join(path, 'latest')

    if dry:
        return True, '[dry] 将写入 %s（保留最近 %d 份）' % (snap, keep)

    try:
        os.makedirs(snap, exist_ok=True)
        n = _copy(srcs, snap)
        mf = _manifest(srcs)
        json.dump(mf, open(os.path.join(snap, '_manifest.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

        # latest 指向最新快照
        if os.path.isdir(latest):
            shutil.rmtree(latest)
        shutil.copytree(snap, latest)

        # 轮转：只保留最近 keep 份快照目录
        snaps = sorted([d for d in os.listdir(path)
                        if os.path.isdir(os.path.join(path, d))
                        and d not in ('latest',)
                        and len(d) == 15 and d[8] == '-'])
        removed = 0
        while len(snaps) > max(1, keep):
            old = snaps.pop(0)
            shutil.rmtree(os.path.join(path, old), exist_ok=True)
            removed += 1
        return True, '已写入 %s（%d 项；轮转删除 %d 份旧快照）' % (snap, n, removed)
    except Exception as e:
        return False, '写入失败：%s' % e


# ---------------------------------------------------------------- 目标：git 远端
def sync_git(name, t, srcs, dry, verbose):
    raw = t.get('url') or ''
    url, miss = _expand(raw)
    if miss:
        return False, '缺少环境变量：%s（凭据请走环境变量，别写进配置）' % ', '.join(miss)
    if not url:
        return False, '未配置 url'
    branch = t.get('branch') or 'main'
    user = t.get('user') or 'backup-bot'
    email = t.get('email') or 'backup-bot@example.com'
    safe = _mask(url)

    if dry:
        return True, '[dry] 将 clone %s 并推送到 %s' % (safe, branch)

    tmp = tempfile.mkdtemp(prefix='backup_')
    try:
        info('克隆 %s …' % safe)
        if _git(['clone', '--quiet', url, tmp], verbose=verbose) is None:
            return False, '克隆失败（检查 url / 凭据 / 网络；已打码：%s）' % safe

        # 目标分支不存在就新建（空仓库探测失败是预期的，不打 WARN）
        if _git(['rev-parse', '--verify', 'HEAD'], cwd=tmp, silent=True) is None:
            info('远端是空仓库，初始化 %s 分支' % branch)
            _git(['checkout', '-b', branch], cwd=tmp, check=True, verbose=verbose)
        else:
            cur = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=tmp)
            if cur != branch:
                if _git(['checkout', '-B', branch], cwd=tmp, verbose=verbose) is None:
                    return False, '切换到分支 %s 失败' % branch

        # 覆盖式同步：先清掉旧的，避免残留已被上游删除的日期文件
        for rel, _abs in srcs:
            d = os.path.join(tmp, rel)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            elif os.path.exists(d):
                os.remove(d)
        n = _copy(srcs, tmp)
        mf = _manifest(srcs)
        json.dump(mf, open(os.path.join(tmp, '_manifest.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

        _git(['config', 'user.name', user], cwd=tmp, check=True)
        _git(['config', 'user.email', email], cwd=tmp, check=True)
        _git(['add', '-A', '--'] + [r for r, _ in srcs] + ['_manifest.json'],
             cwd=tmp, check=True)

        # 有没有真正的变化：列出暂存区的文件名，空 = 远端已是最新
        changed = _git(['diff', '--cached', '--name-only'], cwd=tmp) or ''
        if not changed.strip():
            return True, '远端已是最新，无需提交（%s）' % branch

        msg = 'backup: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if _git(['commit', '-m', msg], cwd=tmp, check=True, verbose=verbose) is None:
            return False, '提交失败'
        if _git(['push', 'origin', branch], cwd=tmp, check=True, verbose=verbose) is None:
            return False, '推送失败（检查凭据权限 / 分支保护）'
        return True, '已推送到 %s 的 %s（%d 项）' % (safe, branch, n)
    except Exception as e:
        return False, '同步失败：%s' % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--dry', action='store_true', help='只打印计划，不实际执行')
    ap.add_argument('--only', help='只跑指定 name 的目标')
    ap.add_argument('--verbose', action='store_true')
    a = ap.parse_args()

    t0 = time.time()
    info('备份同步开始（配置：%s%s）' % (a.config, '，dry-run' if a.dry else ''))

    cfg, cerr = load_config(a.config)
    if cfg is None:
        err(cerr)
        write_result([], cerr)
        return 1
    if cfg.get('enabled') is False:
        info('back.enabled=false，整个备份功能已关闭，直接结束')
        write_result([], 'disabled')
        return 0

    srcs = collect_sources(cfg.get('include') or DEFAULT_INCLUDE)
    ts = targets_of(cfg, a.only)
    if not ts:
        info('没有启用的备份目标（在 %s 的 targets 里加一条并把 enabled 设为 true）' % a.config)
        write_result([], 'no-target')
        return 0

    results = []
    for name, t, include in ts:
        typ = (t.get('type') or '').lower()
        info('—— 目标 %s（%s）——' % (name, typ))
        try:
            if typ == 'local':
                good, msg = sync_local(name, t, srcs, a.dry, a.verbose)
            elif typ == 'git':
                good, msg = sync_git(name, t, srcs, a.dry, a.verbose)
            else:
                good, msg = False, '未知的 type：%s（只支持 local / git）' % (t.get('type'),)
        except Exception as e:                       # 兜底：任何异常都不影响其它目标
            good, msg = False, '未预期的异常：%s' % e
        (ok if good else err)('%s → %s' % (name, msg))
        results.append({'name': name, 'type': typ, 'ok': good, 'message': msg})

    good_n = sum(1 for r in results if r['ok'])
    info('完成：成功 %d / 共 %d，用时 %.1fs' % (good_n, len(results), time.time() - t0))
    write_result(results, None)
    write_step_summary(results, a.dry)
    return 0 if good_n == len(results) else 1


def write_result(results, error):
    try:
        os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
        json.dump({
            'time': datetime.now().isoformat(timespec='seconds'),
            'error': error,
            'results': results,
            'log': _log_lines,
        }, open(RESULT_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        info('结果已写入 %s' % os.path.relpath(RESULT_PATH, ROOT))
    except Exception as e:
        warn('结果文件写入失败：%s' % e)


def write_step_summary(results, dry):
    """GitHub Actions：把结果写成作业摘要（$GITHUB_STEP_SUMMARY）"""
    p = os.environ.get('GITHUB_STEP_SUMMARY')
    if not p:
        return
    try:
        lines = ['### 数据备份同步%s' % '（dry-run）' if dry else '',
                 '', '| 目标 | 类型 | 结果 | 说明 |', '|---|---|---|---|']
        for r in results:
            lines.append('| %s | %s | %s | %s |' % (
                r['name'], r['type'], '✅' if r['ok'] else '❌',
                r['message'].replace('|', '\\|')))
        open(p, 'a', encoding='utf-8').write('\n'.join(lines) + '\n')
    except Exception as e:
        warn('写入作业摘要失败：%s' % e)


if __name__ == '__main__':
    sys.exit(main())
