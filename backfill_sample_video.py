# -*- coding: utf-8 -*-
"""
🎬 過去投稿へのサンプル動画バックフィルスクリプト

wordpress_blog_poster.py で以前に投稿した記事（<video>タグで埋め込んでいたため
再生できなかったもの、または video セクションを未生成のまま投稿されたもの）に対して、
DMM APIからサンプル動画URLを再取得し、iframe埋め込みを追記する。

【前提】
- 各記事のスラッグ（slug）が DMM の content_id になっていること
  （wordpress_blog_poster.py の _make_slug() の仕様に依存）。
- 環境変数は wordpress_blog_poster.py と同じものを使う
  （DMM_API_ID / DMM_AFFILIATE_ID / WP_URL / WP_USERNAME / WP_APP_PASSWORD /
   CONTENT_TYPE など）。

【動作】
1. WordPressの全記事（publish/draft/pending/private）をページングして取得
2. 本文にすでに ona-sample-video（iframe埋め込み）が含まれる記事はスキップ
3. スラッグ(=content_id)を使いDMM APIへ cid 指定で問い合わせ、
   サンプル動画URLを取得
4. 取得できた場合、本文中のサンプル画像ギャラリー直後（無ければCTAボタン直前、
   それも無ければ本文末尾）に <iframe> 埋め込みを追記して更新

【安全設計】
- デフォルトは DRY_RUN=true。実際にWordPressへ書き込む前に、
  「何件中何件が更新対象か」をログでまず確認できるようにしている。
- BACKFILL_LIMIT で1回の実行での更新件数に上限を設ける（DMM API・WPへの
  リクエスト集中を避けるため）。
"""

import os
import sys
import time

import requests

# wordpress_blog_poster.py と同じディレクトリに置いて実行する想定。
# 既存スクリプトの設定・関数（WP認証、DMM API設定、parse_product、
# _sample_video_html等）をそのまま再利用する。
import wordpress_blog_poster as base


DRY_RUN = os.environ.get('DRY_RUN', 'true').strip().lower() not in ('false', '0', 'no')
BACKFILL_LIMIT = int(os.environ.get('BACKFILL_LIMIT', '30'))
REQUEST_INTERVAL_SEC = float(os.environ.get('REQUEST_INTERVAL_SEC', '1.0'))

# 検索対象にするWordPressの投稿ステータス
WP_STATUSES = os.environ.get('BACKFILL_WP_STATUSES', 'publish,draft,pending,private')

VIDEO_MARKER = 'ona-sample-video'
GALLERY_ANCHOR_OPEN = '<div class="ona-sample-gallery">'
CTA_ANCHOR_OPEN = '<div style="text-align:center;margin:20px 0 8px;">'


def fetch_all_wp_posts() -> list:
    """WordPressの全記事（本文を編集用の生HTMLで）をページングして取得する。"""
    posts = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f'{base.WP_URL}/wp-json/wp/v2/posts',
                params={
                    'per_page': 50,
                    'page': page,
                    'status': WP_STATUSES,
                    'context': 'edit',  # content.raw を取得するため（要認証）
                },
                auth=base._wp_auth(),
                timeout=20,
            )
        except Exception as e:
            print(f'❌ WordPress記事一覧の取得に失敗しました（page={page}）: {e}')
            break

        if resp.status_code == 400 and page > 1:
            # ページ範囲外（WordPressは最終ページ+1で400を返す仕様）
            break
        if resp.status_code != 200:
            print(f'❌ WordPress記事一覧の取得に失敗しました（page={page}, status={resp.status_code}）: {resp.text[:200]}')
            break

        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        print(f'📄 記事一覧取得: page={page}（{len(batch)}件、累計{len(posts)}件）')
        page += 1
        time.sleep(REQUEST_INTERVAL_SEC)

    return posts


def fetch_dmm_item_by_cid(cid: str):
    """DMM APIへ content_id（cid）を指定して単一作品を取得する。"""
    params = {
        'api_id':       base.DMM_API_ID,
        'affiliate_id': base.DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      base.SERVICE,
        'floor':        base.FLOOR,
        'hits':         1,
        'cid':          cid,
        'output':       'json',
    }
    try:
        resp = requests.get(f'{base.DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        return items[0] if items else None
    except Exception as e:
        print(f'❌ DMM API取得エラー（cid={cid}）: {e}')
        return None


def insert_video_html(content: str, video_html: str) -> str:
    """本文中の適切な位置にサンプル動画HTMLを挿入する。
    優先順位: ①サンプル画像ギャラリーの直後 → ②CTAボタンの直前 → ③本文末尾"""

    # ①ギャラリー直後に挿入（ギャラリーはネストが浅いdiv2重構造なので、
    #   最初の '</div></div>' で閉じきる想定。non-greedyで安全に一致させる）
    gallery_idx = content.find(GALLERY_ANCHOR_OPEN)
    if gallery_idx != -1:
        close_idx = content.find('</div></div>', gallery_idx)
        if close_idx != -1:
            insert_at = close_idx + len('</div></div>')
            return content[:insert_at] + '\n' + video_html + content[insert_at:]

    # ②CTAボタン直前に挿入
    cta_idx = content.find(CTA_ANCHOR_OPEN)
    if cta_idx != -1:
        return content[:cta_idx] + video_html + '\n' + content[cta_idx:]

    # ③どちらのアンカーも見つからない場合は本文末尾に追加
    return content.rstrip() + '\n' + video_html


def update_post_content(post_id: int, new_content: str) -> bool:
    try:
        resp = requests.post(
            f'{base.WP_URL}/wp-json/wp/v2/posts/{post_id}',
            json={'content': new_content},
            auth=base._wp_auth(),
            timeout=20,
        )
    except Exception as e:
        print(f'❌ 更新リクエストでエラー（post_id={post_id}）: {e}')
        return False

    if resp.status_code not in (200, 201):
        print(f'❌ 更新失敗（post_id={post_id}, status={resp.status_code}）: {resp.text[:200]}')
        return False

    result = resp.json()
    if not isinstance(result, dict) or 'id' not in result:
        print(f'❌ 更新失敗（post_id={post_id}）: WordPressから投稿データが返りませんでした。')
        return False
    return True


def main():
    print('=' * 60)
    print('🎬 過去投稿サンプル動画バックフィル')
    print(f'   モード: {"DRY RUN（書き込みなし・確認のみ）" if DRY_RUN else "本番実行（WordPressを更新します）"}')
    print(f'   更新上限: {BACKFILL_LIMIT}件 / 対象ステータス: {WP_STATUSES}')
    print('=' * 60)

    posts = fetch_all_wp_posts()
    print(f'\n📚 取得した記事総数: {len(posts)}件')

    target_posts = []
    for post in posts:
        content_raw = (post.get('content') or {}).get('raw', '') or (post.get('content') or {}).get('rendered', '')
        if VIDEO_MARKER in content_raw:
            continue  # すでに動画埋め込み済み
        slug = post.get('slug', '')
        if not slug:
            continue
        target_posts.append((post, content_raw, slug))

    print(f'🔍 動画未埋め込みの記事: {len(target_posts)}件')

    updated = 0
    skipped_no_match = 0
    skipped_no_video = 0

    for post, content_raw, slug in target_posts:
        if updated >= BACKFILL_LIMIT:
            print(f'\n⏸️  更新上限（{BACKFILL_LIMIT}件）に達したため終了します。残りは次回実行してください。')
            break

        post_id = post.get('id')
        title = (post.get('title') or {}).get('rendered', '')[:40]

        item = fetch_dmm_item_by_cid(slug)
        time.sleep(REQUEST_INTERVAL_SEC)

        if not item:
            print(f'   ⏭️  DMM側で該当作品が見つかりません（post_id={post_id}, slug={slug}, {title}）')
            skipped_no_match += 1
            continue

        product = base.parse_product(item)
        sample_movie_url = product.get('sample_movie_url', '')
        if not sample_movie_url:
            print(f'   ⏭️  サンプル動画なし（post_id={post_id}, slug={slug}, {title}）')
            skipped_no_video += 1
            continue

        video_html = base._sample_video_html(sample_movie_url)
        new_content = insert_video_html(content_raw, video_html)

        if DRY_RUN:
            print(f'   ✅ [DRY RUN] 更新対象（post_id={post_id}, slug={slug}, {title}）')
            updated += 1
            continue

        if update_post_content(post_id, new_content):
            print(f'   ✅ 更新完了（post_id={post_id}, slug={slug}, {title}）')
            updated += 1
        else:
            print(f'   ❌ 更新失敗（post_id={post_id}, slug={slug}, {title}）')

        time.sleep(REQUEST_INTERVAL_SEC)

    print('\n' + '=' * 60)
    print(f'✅ 完了。更新{"対象" if DRY_RUN else "済み"}: {updated}件 / '
          f'DMM側で見つからず: {skipped_no_match}件 / サンプル動画なし: {skipped_no_video}件')
    if DRY_RUN:
        print('   ※ DRY_RUN=true のため実際の書き込みは行っていません。')
        print('   ※ 本番実行するには環境変数 DRY_RUN=false を指定してください。')
    print('=' * 60)


if __name__ == '__main__':
    main()
