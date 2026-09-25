# -*- coding: utf-8 -*-
"""
📝 同人作品 手動入力 → WordPress 記事生成・下書き投稿ツール

wordpress_blog_poster.py（DMM API自動取得版）をベースに、
DMM APIからの自動取得・AI本文生成・年齢確認フィルターを取り除き、
作品名・ジャンル・価格・アフィリエイトリンク等を「手動入力」して
1記事を生成し、WordPress REST APIへ下書き（draft）として投稿する版。

【自動版との違い】
  ・DMM_API_ID / DMM_AFFILIATE_ID は不要（DMM APIを一切呼ばない）
  ・age_safety_filter（年少者連想ワードの自動除外）は使わない
    → 手動入力のため、投稿者自身が内容を確認する前提
  ・GEMINI_API_KEY による本文自動生成は行わない
    → 本文（あらすじ・ポイント）は環境変数でそのまま入力する
  ・1回の実行につき「1作品」を投稿する（複数作品をまとめて回す機能は無し）

【入力方法】
  すべて環境変数で指定する（GitHub Actionsのworkflow_dispatch inputsから
  そのまま渡す運用を想定）。詳細は下記「⚙️ 設定」を参照。
--------------------------------------------------------------
"""

import os
import re
import sys
import json
import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests

JST = datetime.timezone(datetime.timedelta(hours=9))

# ================================================================
# 📌 スクリプトバージョン（デプロイ確認用）
# ================================================================
SCRIPT_VERSION = '2026-09-25-manual-01'

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

WP_URL          = os.environ.get('WP_URL', '').rstrip('/')      # 例: https://example.com
WP_USERNAME     = os.environ.get('WP_USERNAME', '')             # WordPressのログインユーザー名
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '')         # アプリケーションパスワード

# 投稿ステータス。draft（下書き）/ pending（承認待ち）/ publish（本公開）
WP_POST_STATUS = os.environ.get('WP_POST_STATUS', 'draft').lower()

# --- 作品情報（手動入力）------------------------------------------------
# 必須項目
WORK_TITLE         = os.environ.get('WORK_TITLE', '').strip()
WORK_AFFILIATE_URL = os.environ.get('WORK_AFFILIATE_URL', '').strip()

# 任意項目
WORK_CONTENT_ID   = os.environ.get('WORK_CONTENT_ID', '').strip()       # スラッグ生成に使用（英数字推奨）
WORK_GENRES       = os.environ.get('WORK_GENRES', '').strip()           # カンマ区切り 例: "学園,NTR,フルボイス"
WORK_MAKER        = os.environ.get('WORK_MAKER', '').strip()            # サークル名/メーカー名
WORK_ACTORS       = os.environ.get('WORK_ACTORS', '').strip()           # カンマ区切り（出演者。無ければ空でOK）
WORK_PRICE        = os.environ.get('WORK_PRICE', '').strip()            # 例: "880" または "¥880"
WORK_OVERVIEW     = os.environ.get('WORK_OVERVIEW', '').strip()         # あらすじ・紹介文。空行（\n\n）区切りで段落分け
WORK_POINTS       = os.environ.get('WORK_POINTS', '').strip()           # ポイント一覧。改行またはカンマ区切り
WORK_PACKAGE_IMAGE  = os.environ.get('WORK_PACKAGE_IMAGE', '').strip()  # パッケージ画像URL（アイキャッチに使用）
WORK_SAMPLE_IMAGES  = os.environ.get('WORK_SAMPLE_IMAGES', '').strip()  # サンプル画像URL。改行またはカンマ区切り
WORK_CATEGORY_LABEL = os.environ.get('WORK_CATEGORY_LABEL', '同人').strip()  # 投稿カテゴリー名

if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)

if WP_POST_STATUS not in ('draft', 'pending', 'publish'):
    print(f'⚠️ WP_POST_STATUS="{WP_POST_STATUS}" は不明な値です。'
          f' draft / pending / publish のいずれかを指定してください。draft にフォールバックします。')
    WP_POST_STATUS = 'draft'

if not WORK_TITLE:
    print('❌ 環境変数 WORK_TITLE（作品名）が設定されていません。')
    sys.exit(1)

if not WORK_AFFILIATE_URL:
    print('❌ 環境変数 WORK_AFFILIATE_URL（アフィリエイトリンク）が設定されていません。')
    sys.exit(1)

print('✅ 認証情報・作品情報を読み込みました。')
print(f'🏷️ スクリプトバージョン: {SCRIPT_VERSION}')
if WP_POST_STATUS == 'publish':
    print('🚨 投稿ステータス: publish（本公開）が指定されています。'
          '生成された記事は目視確認なしにそのままサイトへ公開されます。')
else:
    print(f'📌 投稿ステータス: {WP_POST_STATUS}（公開は必ず手動で行ってください）')


# ================================================================
# 🧩 入力の整形（カンマ/改行区切り文字列 → リスト）
# ================================================================

def _split_list(raw: str) -> list:
    """カンマ区切り・改行区切りのどちらにも対応してリストに変換する。"""
    if not raw:
        return []
    # 改行があれば改行優先、無ければカンマ区切りとして扱う
    parts = raw.splitlines() if '\n' in raw else raw.split(',')
    return [p.strip() for p in parts if p.strip()]


def _parse_price(raw: str):
    """"880" でも "¥880" でも受け付け、(表示用文字列, 数値) を返す。"""
    if not raw:
        return '', None
    digits = ''.join(c for c in raw if c.isdigit())
    if not digits:
        return raw, None
    price_num = int(digits)
    return f'¥{price_num:,}', price_num


genres = _split_list(WORK_GENRES)
actors = _split_list(WORK_ACTORS)
points = _split_list(WORK_POINTS)
sample_images = _split_list(WORK_SAMPLE_IMAGES)
price_str, price_num = _parse_price(WORK_PRICE)


# ================================================================
# 📝 HTML生成パーツ（自動版と共通のロジックを流用）
# ================================================================

def _paragraphs_to_html(text: str) -> str:
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    html_parts = []
    for p in paragraphs:
        p_html = escape(p).replace('\n', '<br>')
        html_parts.append(
            f'<p style="line-height:1.8;color:#333;margin:0 0 12px;">{p_html}</p>'
        )
    return '\n'.join(html_parts)


_BADGE_COLORS = ['#ff6f91', '#ff9671', '#845ec2', '#4b93ff', '#00c2a8']


def _genre_badges_html(genre_list: list) -> str:
    if not genre_list:
        return ''
    badges = []
    for i, g in enumerate(genre_list[:5]):
        color = _BADGE_COLORS[i % len(_BADGE_COLORS)]
        badges.append(
            f'<span style="display:inline-block;background:{color};color:#fff;'
            'padding:4px 12px;border-radius:999px;font-size:12px;font-weight:bold;'
            f'margin:2px 4px 2px 0;">{escape(g)}</span>'
        )
    return f'<div style="margin:8px 0;">{"".join(badges)}</div>'


def _points_list_html(point_list: list, heading: str = '✓ ここがポイント') -> str:
    if not point_list:
        return ''
    items = ''.join(
        f'<li style="margin:6px 0;line-height:1.6;">{escape(pt)}</li>'
        for pt in point_list
    )
    return (
        '<div class="ona-points-box">'
        f'<h2 class="ona-points-title" style="margin:0 0 8px;font-size:16px;">{escape(heading)}</h2>'
        f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
        '</div>'
    )


def _sample_gallery_html(affiliate_url: str, image_list: list, title: str,
                          heading: str = '作品サンプル') -> str:
    imgs = [u for u in (image_list or []) if u][:8]
    if not imgs:
        return ''
    cells = []
    for i, url in enumerate(imgs):
        alt_text = f'{title} サンプル画像' if i == 0 else f'サンプル画像{i + 1}'
        cells.append(
            f'<a href="{escape(affiliate_url)}" target="_blank" rel="nofollow" class="ona-sample-cell">'
            f'<img src="{escape(url)}" alt="{escape(alt_text)}" loading="lazy" class="ona-sample-img"></a>'
        )
    return (
        '<div class="ona-sample-gallery">'
        f'<h3 class="ona-sample-gallery-title" style="margin:0 0 8px;font-size:15px;">{escape(heading)}</h3>'
        '<div class="ona-sample-grid">' + ''.join(cells) + '</div>'
        '</div>'
    )


def _make_slug(content_id: str, title: str) -> str:
    base = (content_id or '').strip()
    base = re.sub(r'[^A-Za-z0-9\-]+', '-', base).strip('-').lower()
    if base:
        return base
    fallback = re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-').lower()
    return fallback[:60] or 'item'


def _make_excerpt(title: str, max_len: int = 90) -> str:
    plain = re.sub(r'\s+', ' ', title or '').strip()
    if len(plain) > max_len:
        plain = plain[:max_len - 1].rstrip() + '…'
    return plain


def _make_description_excerpt(overview_text: str, fallback_title: str, max_len: int = 90) -> str:
    plain = re.sub(r'\s+', ' ', (overview_text or '')).strip()
    if not plain:
        return _make_excerpt(fallback_title, max_len=max_len)
    if len(plain) > max_len:
        plain = plain[:max_len - 1].rstrip() + '…'
    return plain


def build_article() -> dict:
    """手動入力された環境変数から記事データを組み立てる。"""

    # フォーカスキーフレーズ: ジャンルの先頭1つ、無ければサークル名
    focus_keyphrase = genres[0] if genres else (WORK_MAKER or '')

    overview_text = WORK_OVERVIEW or WORK_TITLE
    overview_html = _paragraphs_to_html(overview_text)
    points_html = _points_list_html(points)
    genre_badges_html = _genre_badges_html(genres)
    gallery_html = _sample_gallery_html(WORK_AFFILIATE_URL, sample_images, WORK_TITLE)

    meta_line_parts = []
    if WORK_MAKER:
        meta_line_parts.append(f'サークル: {escape(WORK_MAKER)}')
    meta_line_html = ''
    if meta_line_parts:
        meta_line_html = (
            '<div style="color:#666;font-size:13px;margin:4px 0 10px;">'
            + ' ／ '.join(meta_line_parts) + '</div>'
        )

    price_badge_html = ''
    if price_str:
        price_badge_html = (
            '<div style="display:inline-block;background:#fff0f5;color:#e0507a;'
            'border:1px solid #ffc2d6;border-radius:8px;padding:6px 14px;'
            f'font-size:15px;font-weight:bold;margin:10px 0;">価格 {escape(price_str)}</div>'
        )

    overview_section_html = (
        '<h2 style="margin:14px 0 8px;font-size:17px;">作品の魅力</h2>'
        f'<div>{overview_html}</div>'
    )

    cta_html = (
        '<div style="text-align:center;margin:20px 0 8px;">'
        f'<a href="{escape(WORK_AFFILIATE_URL)}" target="_blank" rel="nofollow" '
        'style="display:inline-block;padding:14px 36px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:16px;font-weight:bold;'
        'box-shadow:0 4px 12px rgba(224,80,122,0.35);">'
        '▶ 作品ページを見る</a></div>'
    )

    disclaimer_html = (
        '<p style="color:#999;font-size:12px;line-height:1.6;margin-top:16px;">'
        '※成人向けコンテンツを含みます。18歳未満の方はご利用いただけません。</p>'
    )

    internal_link_html = ''
    if WP_URL:
        internal_link_html = (
            f'<p style="font-size:13px;margin-top:14px;">'
            f'<a href="{escape(WP_URL)}/category/{escape(WORK_CATEGORY_LABEL)}/">'
            f'他の{escape(WORK_CATEGORY_LABEL)}作品もチェックする →</a></p>'
        )

    # セクション順は固定（手動投稿は件数が少なく、バリエーション管理は不要なため）
    card_inner = '\n'.join(filter(None, [
        overview_section_html,
        meta_line_html,
        genre_badges_html,
        price_badge_html,
        points_html,
        gallery_html,
        cta_html,
        internal_link_html,
        disclaimer_html,
    ]))

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    excerpt = _make_description_excerpt(overview_text, WORK_TITLE, max_len=55)

    return {
        'title':             WORK_TITLE,
        'slug':              _make_slug(WORK_CONTENT_ID, WORK_TITLE),
        'excerpt':           excerpt,
        'body':              body_html,
        'tags':              genres,
        'actors':            actors,
        'category_label':    WORK_CATEGORY_LABEL,
        'featured_image_url': WORK_PACKAGE_IMAGE,
        'content_id':        WORK_CONTENT_ID,
        'focus_keyphrase':   focus_keyphrase,
        'seo_title':         WORK_TITLE[:32],
        'affiliate_url':     WORK_AFFILIATE_URL,
    }


# ================================================================
# 🔐 WordPress REST API 投稿（アプリケーションパスワード認証）
# ================================================================

_category_cache = {}
_tag_cache = {}
_actress_cache = {}


def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}


def _get_or_create_term(taxonomy: str, name: str, cache: dict):
    """WordPressのカテゴリー/タグを名前で検索し、無ければ作成してIDを返す。"""
    if not name:
        return None
    if name in cache:
        return cache[name]

    endpoint = f'{WP_URL}/wp-json/wp/v2/{taxonomy}'
    try:
        resp = requests.get(
            endpoint, params={'search': name, 'per_page': 100},
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
        )
        if resp.status_code == 200:
            try:
                results = resp.json()
            except ValueError:
                results = None
            if isinstance(results, list):
                for term in results:
                    if isinstance(term, dict) and term.get('name') == name:
                        cache[name] = term['id']
                        return term['id']

        resp = requests.post(
            endpoint, data=json.dumps({'name': name}).encode('utf-8'),
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
        )
        if resp.status_code in (200, 201):
            try:
                created = resp.json()
            except ValueError:
                created = None
            if isinstance(created, dict) and 'id' in created:
                term_id = created['id']
                cache[name] = term_id
                return term_id
            print(f'    ⚠️ タクソノミー"{name}"の作成レスポンスが想定外の形式です: {resp.text[:200]}')
            return None

        try:
            err = resp.json()
        except ValueError:
            err = None
        if isinstance(err, dict) and err.get('code') == 'term_exists':
            existing_id = (err.get('data') or {}).get('term_id')
            if existing_id:
                cache[name] = existing_id
                return existing_id

        print(f'    ⚠️ タクソノミー"{name}"の作成に失敗 status={resp.status_code}: {resp.text[:200]}')
        return None
    except Exception as e:
        print(f'    ⚠️ タクソノミー"{name}"の取得/作成エラー: {e}')
        return None


def _upload_featured_image(image_url: str, content_id: str):
    """パッケージ画像をWordPressメディアライブラリにアップロードし、attachment IDを返す。"""
    if not image_url:
        return None
    try:
        import mimetypes
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext = mimetypes.guess_extension(content_type) or '.jpg'
        safe_id = (content_id or 'item').replace(' ', '_')
        filename = f'featured-{safe_id}{ext}'

        resp = requests.post(
            f'{WP_URL}/wp-json/wp/v2/media',
            data=img_resp.content,
            auth=_wp_auth(),
            headers={
                'Content-Type': content_type,
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Accept': 'application/json',
                'User-Agent': _JSON_HEADERS['User-Agent'],
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return resp.json()['id']
        print(f'    ⚠️ アイキャッチ画像のアップロードに失敗 status={resp.status_code}: {resp.text[:200]}')
        return None
    except Exception as e:
        print(f'    ⚠️ アイキャッチ画像の取得/アップロードエラー: {e}')
        return None


def post_draft_to_wordpress(article: dict) -> bool:
    endpoint = f'{WP_URL}/wp-json/wp/v2/posts'

    category_label = article.get('category_label') or '同人'
    category_ids = []
    base_category_id = _get_or_create_term('categories', category_label, _category_cache)
    if base_category_id:
        category_ids.append(base_category_id)

    tag_ids = []
    for tag_name in article.get('tags', []):
        if not tag_name or tag_name == category_label:
            continue
        tid = _get_or_create_term('tags', tag_name, _tag_cache)
        if tid and tid not in tag_ids:
            tag_ids.append(tid)

    # 出演者を専用タクソノミー「onavi_actress」に登録する（WordPress側で
    # register_taxonomy()によりREST APIに公開されている必要がある）
    actress_ids = []
    for actor_name in article.get('actors', []):
        if not actor_name:
            continue
        aid = _get_or_create_term('onavi_actress', actor_name, _actress_cache)
        if aid and aid not in actress_ids:
            actress_ids.append(aid)

    payload = {
        'title':      article['title'],
        'slug':       article.get('slug') or '',
        'excerpt':    article.get('excerpt') or '',
        'content':    article['body'],
        'status':     WP_POST_STATUS,
        'categories': category_ids,
        'tags':       tag_ids,
    }
    if actress_ids:
        payload['onavi_actress'] = actress_ids

    meta = {}
    focus_keyphrase = article.get('focus_keyphrase') or ''
    if focus_keyphrase:
        meta['_yoast_wpseo_focuskw'] = focus_keyphrase
    seo_title = article.get('seo_title') or ''
    if seo_title:
        meta['_yoast_wpseo_title'] = seo_title
    metadesc = article.get('excerpt') or ''
    if metadesc:
        meta['_yoast_wpseo_metadesc'] = metadesc
    meta['_onavi_script_version'] = SCRIPT_VERSION

    if article.get('affiliate_url'):
        meta['_onavi_affiliate_url'] = article['affiliate_url']

    if meta:
        payload['meta'] = meta

    media_id = _upload_featured_image(article.get('featured_image_url', ''), article.get('content_id', ''))
    if media_id:
        payload['featured_media'] = media_id

    try:
        resp = requests.post(
            endpoint, data=json.dumps(payload).encode('utf-8'),
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=20,
        )

        if resp.history:
            redirect_chain = ' -> '.join(r.url for r in resp.history) + f' -> {resp.url}'
            print(f"    🔎 リダイレクトが発生しています: {redirect_chain}")
        print(f"    🔎 HTTPステータス: {resp.status_code}")

        if resp.status_code in (200, 201):
            try:
                result = resp.json()
            except ValueError:
                result = None
            if not isinstance(result, dict) or 'id' not in result:
                print(f"    ❌ 投稿失敗：WordPressから投稿データが返りませんでした"
                      f"（サーバー側のbot対策等でブロックされた可能性があります）: {resp.text[:300]}")
                return False
            actual_status = result.get('status')
            if actual_status != WP_POST_STATUS:
                print(f"    ⚠️ 指定したステータス（{WP_POST_STATUS}）と異なる値が返りました"
                      f"（status={actual_status}）。念のため内容をご確認ください: {result.get('link', '')}")
            print(f"    ✅ {actual_status}として投稿成功: {article['title'][:40]}")
            print(f"    🔗 編集画面等の確認は WordPress管理画面からご確認ください（link: {result.get('link', '')}）")
            return True
        else:
            print(f"    ❌ 投稿失敗 status={resp.status_code}: {resp.text[:300]}")
            return False
    except Exception as e:
        print(f"    ❌ 投稿エラー: {e}")
        return False


# ================================================================
# 🚀 メイン実行
# ================================================================

def main():
    print(f'\n📝 記事生成中: {WORK_TITLE[:40]}')
    article = build_article()
    ok = post_draft_to_wordpress(article)
    if ok:
        print(f'\n✅ 完了！WordPressに{WP_POST_STATUS}として投稿しました。')
        print('   ※ 公開前に必ず内容をご確認ください。')
    else:
        print('\n❌ 投稿に失敗しました。ログを確認してください。')
        sys.exit(1)


if __name__ == '__main__':
    main()
