#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度存活 + 变更巡检：读 index.html 里的赛事 URL，写 status.json。
只产出"三态标记"，绝不修改主数据。"""
import json, os, re, sys, time, hashlib, datetime
from urllib.parse import urlsplit

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'index.html')
STATUS = os.path.join(ROOT, 'status.json')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AwardsTimelineBot/1.0')
TIMEOUT = 25
SLEEP = 1.0
BLOCK_CODES = {401, 403, 406, 407, 409, 418, 429, 503, 999}   # 反爬/风控，不等于站点失效
UA2 = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
       '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
YEAR_RE = re.compile(r'(20\d{2})')


def load_dates():
    """从 index.html 抽出 [(id, name, url)]，跳过 qr / note 类。"""
    html = open(INDEX, encoding='utf-8').read()
    out = []
    for block in re.split(r'\n  \{\n', html):
        m = re.search(r"id:'([a-z0-9_]+)'", block)
        if not m:
            continue
        did = m.group(1)
        nm = re.search(r"name:'([^']+)'", block)
        url = re.search(r"url:'([^']+)'", block)
        if not url:
            continue
        out.append((did, nm.group(1) if nm else did, url.group(1)))
    return out


def fingerprint(html):
    """稳定指纹：标题 + 页面出现的日期集合 + 年份集合。
    忽略新闻轮播/阅读数这类月度噪声，只盯「日期口径有没有变」。"""
    title = ''
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if m:
        title = re.sub(r'\s+', ' ', m.group(1)).strip()
    text = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', html, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    dates = sorted(set(
        '%s-%s-%s' % (y, int(mo) , int(d) if d else 0)
        for y, mo, d in re.findall(r'(20\d{2})\s*[年./\-]\s*(\d{1,2})(?:\s*[月./\-]\s*(\d{1,2}))?', text)
    ))
    years = sorted(set(YEAR_RE.findall(text)))
    payload = title + '|' + ','.join(dates) + '|' + ','.join(years)
    return hashlib.sha256(payload.encode('utf-8', 'ignore')).hexdigest()[:16], years, len(text), dates


def check_one(did, name, url):
    rec = {'name': name, 'url': url, 'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')}
    try:
        hdr = {
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Cache-Control': 'no-cache',
        }
        r = requests.get(url, headers=hdr, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in BLOCK_CODES:          # 换手机 UA 再试一次，仍被拦才算 blocked
            time.sleep(1.0)
            r = requests.get(url, headers=dict(hdr, **{'User-Agent': UA2}), timeout=TIMEOUT, allow_redirects=True)
        rec['http'] = r.status_code
        rec['finalUrl'] = r.url
        if urlsplit(r.url).netloc != urlsplit(url).netloc:
            rec['moved'] = True
        if r.status_code in BLOCK_CODES:
            rec['state'] = 'blocked'          # 被反爬/风控拦，不代表链接坏了
            rec['http'] = r.status_code
            return rec
        if r.status_code >= 400:
            rec['state'] = 'dead'             # 404/410 等：确实失效
            return rec
        r.encoding = r.apparent_encoding or r.encoding
        fp, years, size, dates = fingerprint(r.text)
        rec.update(fp=fp, years=years, size=size, ndates=len(dates))
        # 反爬 / 跳登录：内容不可信，标记 blocked 并跳过变更比对，避免每月误报
        host = urlsplit(r.url).netloc.lower()
        if r.status_code in (202, 403, 429) or size < 1500 or 'login' in host or '/login' in r.url:
            rec['state'] = 'blocked'
            rec.pop('fp', None)
            return rec
        rec['state'] = 'ok'
        return rec
    except Exception as e:                  # 超时、TLS、DNS、被 WAF 拦
        rec['state'] = 'unreachable'
        rec['error'] = type(e).__name__ + ': ' + str(e)[:120]
        return rec


def main():
    limit = None
    for i, a in enumerate(sys.argv):
        if a == '--limit':
            limit = int(sys.argv[i + 1])
    targets = load_dates()
    if limit:
        targets = targets[:limit]

    prev = {}
    if os.path.exists(STATUS):
        try:
            prev = {k: v for k, v in json.load(open(STATUS, encoding='utf-8')).get('items', {}).items()}
        except Exception:
            prev = {}

    items, dead, changed, pending, moved = {}, [], [], [], []
    for n, (did, name, url) in enumerate(targets, 1):
        rec = check_one(did, name, url)
        old = prev.get(did, {})
        if rec['state'] == 'dead':
            dead.append(did)
        elif old and rec.get('state') == 'blocked':
            pass                            # 反爬/登录页，不参与变更判定
        elif old:
            if old.get('fp') and rec.get('fp') and old['fp'] != rec['fp']:
                rec['state'] = 'changed'
                new_years = sorted(set(rec.get('years', [])) - set(old.get('years', [])))
                if new_years:
                    rec['newYears'] = new_years
                changed.append(did)
            elif old.get('years') and rec.get('years') and sorted(old['years']) != sorted(rec['years']):
                rec['state'] = 'changed'
                changed.append(did)
            if rec.get('moved') and not old.get('moved'):
                moved.append(did)
        else:
            rec['baseline'] = True        # 首次建立基线，保留 dead/unreachable/blocked 原判
        items[did] = rec
        print('[%d/%d] %-12s %-22s %s' % (n, len(targets), did, rec.get('http', 'ERR'), rec['state']), flush=True)
        time.sleep(SLEEP)

    out = {
        'generatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
        'total': len(targets),
        'counts': {
            'ok': sum(1 for v in items.values() if v['state'] == 'ok'),
            'baseline': sum(1 for v in items.values() if v.get('baseline')),
            'changed': len(changed),
            'dead': len(dead),
            'unreachable': sum(1 for v in items.values() if v['state'] == 'unreachable'),
            'blocked': sum(1 for v in items.values() if v['state'] == 'blocked'),
        },
        'dead': dead,
        'changed': changed,
        'moved': moved,
        'items': items,
    }
    json.dump(out, open(STATUS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, sort_keys=True)
    print('\n写好 status.json：', json.dumps(out['counts'], ensure_ascii=False))
    if dead:
        print('失效：', ', '.join(dead))
    if changed:
        print('有变更：', ', '.join(changed))


if __name__ == '__main__':
    main()
