# -*- coding: utf-8 -*-
"""
🖼️ 既存の下書き記事に「アイキャッチ画像」を一括設定するスクリプト

すでにWordPressに投稿済み（下書き含む）の記事本文から、
先頭のパッケージ画像URLを抽出し、WordPressのメディアライブラリに
アップロードしたうえで、その投稿のアイキャッチ画像として設定します。

- 既にアイキャッチが設定されている記事はスキップします（再実行しても安全）
- status は draft と publish の両方を対象にします
- 対象を絞りたい場合は環境変数 TARGET_STATUS で 'draft' のみ等に変更可能

必要な環境変数（wordpress_blog_poster.pyと共通）:
  WP_URL, WP_USERNAME, WP_APP_PASSWORD
"""

import os
import re
import sys
import json
import mimetypes
import requests

WP_URL          = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME     = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '')
TARGET_STATUS   = os.environ.get('TARGET_STATUS', 'draft,publish,pending')

if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)

_AUTH = (WP_USERNAME, WP_APP_PASSWORD)
_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
}

_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


def fetch_target_posts():
    """対象ステータスの投稿を全件（ページング込みで）取得する。"""
    all_posts = []
    page = 1
    while True:
        resp = requests.get(
            f'{WP_URL}/wp-json/wp/v2/posts',
            params={'status': TARGET_STATUS, 'per_page': 50, 'page': page, 'context': 'edit'},
            auth=_AUTH, headers=_JSON_HEADERS, timeout=20,
        )
        if resp.status_code != 200:
            print(f'❌ 投稿一覧の取得に失敗 status={resp.status_code}: {resp.text[:200]}')
            break
        batch = resp.json()
        if not batch:
            break
        all_posts.extend(batch)
        total_pages = int(resp.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return all_posts


def extract_first_image_url(html_content: str):
    m = _IMG_SRC_RE.search(html_content or '')
    return m.group(1) if m else None


def upload_image_to_media(image_url: str, title: str, post_id: int):
    """外部画像URLをダウンロードし、WordPressメディアライブラリにアップロードする。"""
    try:
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext = mimetypes.guess_extension(content_type) or '.jpg'
        # HTTPヘッダーはASCII(latin-1)のみ許容されるため、日本語タイトルではなく
        # 投稿IDベースの英数字のみのファイル名にする
        filename = f'featured-{post_id}{ext}'

        resp = requests.post(
            f'{WP_URL}/wp-json/wp/v2/media',
            data=img_resp.content,
            auth=_AUTH,
            headers={
                'Content-Type': content_type,
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Accept': 'application/json',
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return resp.json()['id']
        else:
            print(f'    ⚠️ メディアアップロード失敗 status={resp.status_code}: {resp.text[:200]}')
            return None
    except Exception as e:
        print(f'    ⚠️ 画像ダウンロード/アップロードエラー: {e}')
        return None


def set_featured_media(post_id: int, media_id: int) -> bool:
    resp = requests.post(
        f'{WP_URL}/wp-json/wp/v2/posts/{post_id}',
        data=json.dumps({'featured_media': media_id}).encode('utf-8'),
        auth=_AUTH, headers=_JSON_HEADERS, timeout=20,
    )
    if resp.status_code in (200, 201):
        return True
    print(f'    ⚠️ アイキャッチ設定失敗 status={resp.status_code}: {resp.text[:200]}')
    return False


def main():
    posts = fetch_target_posts()
    print(f'📚 対象記事: {len(posts)}件（status={TARGET_STATUS}）')

    updated, skipped, failed = 0, 0, 0

    for post in posts:
        post_id = post['id']
        title = post.get('title', {}).get('rendered', f'post-{post_id}')

        if post.get('featured_media'):
            skipped += 1
            continue

        content_html = post.get('content', {}).get('rendered', '')
        image_url = extract_first_image_url(content_html)
        if not image_url:
            print(f'⚠️ 画像が見つかりませんでした: {title[:40]}')
            failed += 1
            continue

        print(f'🖼️ 処理中: {title[:40]}')
        media_id = upload_image_to_media(image_url, title, post_id)
        if not media_id:
            failed += 1
            continue

        if set_featured_media(post_id, media_id):
            print(f'    ✅ アイキャッチ設定完了')
            updated += 1
        else:
            failed += 1

    print(f'\n✅ 完了！ 更新 {updated}件 / スキップ（設定済み）{skipped}件 / 失敗 {failed}件')


if __name__ == '__main__':
    main()
