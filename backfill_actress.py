#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_actress.py
--------------------------------------------------------------
過去に投稿した「動画」（AV）カテゴリーの記事のうち、
出演者専用タクソノミー（onavi_actress）が未設定のものに対して、
DMM APIから出演者情報を再取得し、一括で付加するバックフィルスクリプト。

前提:
  - wordpress_blog_poster.py と同じ環境変数（DMM_API_ID, DMM_AFFILIATE_ID,
    WP_URL, WP_USERNAME, WP_APP_PASSWORD）が設定されていること
  - WordPress側で onavi_actress タクソノミーが REST API に公開されていること
    （register_taxonomy の show_in_rest => true、rest_base => 'onavi_actress'）
  - 投稿のスラッグ（例: 1stars00984 / dvaj00724 など）が、
    そのままDMMの商品番号（cid）になっていること
    （wordpress_blog_poster.py の _make_slug() がそのように生成しているため、
    通常は一致するはず。一致しない記事はスキップしてログに出す）

実行方法:
  python3 backfill_actress.py
  （最初は DRY_RUN = True のまま実行し、対象件数・出演者名を確認してから
    DRY_RUN = False にして再実行することを推奨）
"""

import os
import time
import requests

# ================================================================
# 設定（wordpress_blog_poster.py と同じ環境変数を使う）
# ================================================================
DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')
DMM_API_BASE     = 'https://api.dmm.com/affiliate/v3'

WP_URL          = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME     = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '')

# AV記事が属しているWordPressのカテゴリー名（このサイトでは「動画」）
AV_CATEGORY_NAME = '動画'

# True のうちは実際の更新を行わず、対象件数と取得予定の出演者名だけを表示する。
# 内容を確認できたら False にして再実行する。
DRY_RUN = True

# DMM APIへの過度なリクエストを避けるための待機秒数
SLEEP_SECONDS = 0.5

_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}


def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


def _check_env():
    missing = [
        name for name, val in [
            ('DMM_API_ID', DMM_API_ID),
            ('DMM_AFFILIATE_ID', DMM_AFFILIATE_ID),
            ('WP_URL', WP_URL),
            ('WP_USERNAME', WP_USERNAME),
            ('WP_APP_PASSWORD', WP_APP_PASSWORD),
        ] if not val
    ]
    if missing:
        print(f'❌ 環境変数が未設定です: {", ".join(missing)}')
        raise SystemExit(1)


def get_av_category_id():
    """「動画」カテゴリーのIDを取得する。"""
    resp = requests.get(
        f'{WP_URL}/wp-json/wp/v2/categories',
        params={'search': AV_CATEGORY_NAME, 'per_page': 100},
        auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
    )
    resp.raise_for_status()
    for term in resp.json():
        if term.get('name') == AV_CATEGORY_NAME:
            return term['id']
    return None


def fetch_all_av_posts(category_id):
    """「動画」カテゴリーの全投稿を取得する（ページング対応）。
    onavi_actress の値も一緒に取得し、既に設定済みの投稿は除外する。"""
    posts = []
    page = 1
    while True:
        resp = requests.get(
            f'{WP_URL}/wp-json/wp/v2/posts',
            params={
                'categories': category_id,
                'per_page':   100,
                'page':       page,
                '_fields':    'id,slug,title,onavi_actress',
            },
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=20,
        )
        if resp.status_code == 400:
            # ページ数を超えた場合、WPは400を返すことがある
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        total_pages = int(resp.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return posts


def fetch_actress_names_by_cid(cid):
    """DMM APIから、指定した商品番号(cid)の出演者名リストを取得する。
    services/floorが分からない場合に備え、複数のservice/floorの組み合わせを試す。"""
    combos = [
        ('digital', 'videoa'),   # 単体・企画AV
        ('digital', 'videoc'),   # その他AVフロア
        ('mono',    'dvd'),      # パッケージ系
    ]
    for service, floor in combos:
        params = {
            'api_id':       DMM_API_ID,
            'affiliate_id': DMM_AFFILIATE_ID,
            'site':         'FANZA',
            'service':      service,
            'floor':        floor,
            'cid':          cid,
            'hits':         1,
            'output':       'json',
        }
        try:
            resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
            data = resp.json()
            items = data.get('result', {}).get('items', [])
            if isinstance(items, dict):
                items = items.get('item', [])
            if items:
                actress_list = items[0].get('iteminfo', {}).get('actress') or []
                names = [a.get('name', '') for a in actress_list if a.get('name')]
                if names:
                    return names[:3]
        except Exception as e:
            print(f'    ⚠️ DMM API取得エラー（cid={cid}, service={service}/{floor}）: {e}')
        time.sleep(SLEEP_SECONDS)
    return []


_actress_cache = {}


def get_or_create_actress_term(name):
    if name in _actress_cache:
        return _actress_cache[name]
    endpoint = f'{WP_URL}/wp-json/wp/v2/onavi_actress'
    resp = requests.get(
        endpoint, params={'search': name, 'per_page': 100},
        auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
    )
    if resp.status_code == 200:
        for term in resp.json():
            if isinstance(term, dict) and term.get('name') == name:
                _actress_cache[name] = term['id']
                return term['id']
    # 無ければ新規作成
    resp = requests.post(
        endpoint, json={'name': name},
        auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
    )
    if resp.status_code in (200, 201):
        term_id = resp.json().get('id')
        _actress_cache[name] = term_id
        return term_id
    print(f'    ❌ タームの作成に失敗しました（{name}）: {resp.status_code} {resp.text[:200]}')
    return None


def update_post_actress(post_id, actress_ids):
    resp = requests.post(
        f'{WP_URL}/wp-json/wp/v2/posts/{post_id}',
        json={'onavi_actress': actress_ids},
        auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
    )
    return resp.status_code in (200, 201)


def main():
    _check_env()

    print('🔍 「動画」カテゴリーIDを取得しています...')
    category_id = get_av_category_id()
    if not category_id:
        print('❌ 「動画」カテゴリーが見つかりませんでした。')
        return
    print(f'✅ カテゴリーID: {category_id}')

    print('📥 AV記事の一覧を取得しています...')
    posts = fetch_all_av_posts(category_id)
    print(f'✅ {len(posts)} 件のAV記事を取得しました。')

    targets = [p for p in posts if not p.get('onavi_actress')]
    print(f'🎯 出演者未設定の記事: {len(targets)} 件')

    if DRY_RUN:
        print('\n⚠️  DRY_RUN = True のため、実際の更新は行いません。')
        print('    対象記事と取得予定の出演者名を確認してください。\n')

    updated, skipped, failed = 0, 0, 0

    for i, post in enumerate(targets, 1):
        slug = post.get('slug', '')
        title = post.get('title', {}).get('rendered', '')
        print(f'[{i}/{len(targets)}] {slug} 「{title}」')

        names = fetch_actress_names_by_cid(slug)
        if not names:
            print('    → DMM APIから出演者情報を取得できませんでした（スキップ）')
            skipped += 1
            continue

        print(f'    → 出演者: {", ".join(names)}')

        if DRY_RUN:
            continue

        term_ids = []
        for name in names:
            tid = get_or_create_actress_term(name)
            if tid:
                term_ids.append(tid)

        if not term_ids:
            print('    ❌ タームの作成/取得に失敗したためスキップします')
            failed += 1
            continue

        if update_post_actress(post['id'], term_ids):
            print('    ✅ 更新しました')
            updated += 1
        else:
            print('    ❌ 投稿の更新に失敗しました')
            failed += 1

        time.sleep(SLEEP_SECONDS)

    print('\n================================')
    print(f'完了: 更新 {updated} 件 / スキップ {skipped} 件 / 失敗 {failed} 件')
    if DRY_RUN:
        print('※ DRY_RUN = True だったため、実際の更新は行われていません。')
        print('  内容を確認できたら、スクリプト冒頭の DRY_RUN を False にして再実行してください。')
    print('================================')


if __name__ == '__main__':
    main()
