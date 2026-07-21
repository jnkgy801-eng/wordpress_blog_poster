# -*- coding: utf-8 -*-
"""
🏆 「今月のランキングTOP10」「ジャンル別まとめ」ページ自動生成ツール
--------------------------------------------------------------
wordpress_blog_poster.py（1作品=1記事）とは別に、
複数作品をまとめた「ランキング記事」「ジャンル別まとめ記事」を1本だけ生成し、
WordPress REST APIへ投稿（または既存記事の更新）します。

Googleの「Scaled Content Abuse（大量生成コンテンツ）」ポリシー対策として、
機械的な1作品1記事とは違う「編集的にまとめた記事」を用意する目的の機能です。

【モード】(env: ROUNDUP_MODE)
  ranking : 今月配信の作品のうち、rank順（人気順）TOP N をまとめる
  genre   : 指定ジャンル（article/article_id）のrank順TOP Nをまとめる

【使い方】
  # 今月のランキングTOP10（毎月1回など、定期実行を想定）
  ROUNDUP_MODE=ranking python wordpress_roundup_poster.py

  # ジャンル別まとめ（ジャンルごとに実行。dmm_genre_search.pyで事前にIDを調べる）
  ROUNDUP_MODE=genre GENRE_LABEL=NTR DMM_ARTICLE_ID=4111 python wordpress_roundup_poster.py

必要な環境変数（wordpress_blog_poster.pyと共通）:
  DMM_API_ID, DMM_AFFILIATE_ID, WP_URL, WP_USERNAME, WP_APP_PASSWORD
"""

import os
import sys
import json
import datetime
import requests
from xml.sax.saxutils import escape

from age_safety_filter import is_safe

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

WP_URL          = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME     = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '')
WP_POST_STATUS  = os.environ.get('WP_POST_STATUS', 'draft').lower()

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)
if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)
if WP_POST_STATUS not in ('draft', 'pending', 'publish'):
    WP_POST_STATUS = 'draft'

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'

# doujin（FANZA同人）/ av（FANZA動画）
CONTENT_TYPE = os.environ.get('CONTENT_TYPE', 'av').strip().lower()
_CONTENT_TARGETS = {
    'doujin': {'service': 'doujin', 'floor': 'digital_doujin', 'label': 'FANZA同人'},
    'av':     {'service': 'digital', 'floor': 'videoa',        'label': 'FANZA動画'},
}
if CONTENT_TYPE not in _CONTENT_TARGETS:
    CONTENT_TYPE = 'av'
SERVICE       = _CONTENT_TARGETS[CONTENT_TYPE]['service']
FLOOR         = _CONTENT_TARGETS[CONTENT_TYPE]['floor']
CONTENT_LABEL = _CONTENT_TARGETS[CONTENT_TYPE]['label']

ROUNDUP_MODE = os.environ.get('ROUNDUP_MODE', 'ranking').strip().lower()
if ROUNDUP_MODE not in ('ranking', 'genre'):
    print(f'❌ ROUNDUP_MODE="{ROUNDUP_MODE}" は不明な値です（ranking / genre のいずれか）。')
    sys.exit(1)

TOP_N = int(os.environ.get('ROUNDUP_TOP_N', '10'))

# genreモード用
GENRE_LABEL    = os.environ.get('GENRE_LABEL', '').strip()
DMM_ARTICLE_ID = os.environ.get('DMM_ARTICLE_ID', '').strip()
if ROUNDUP_MODE == 'genre' and (not GENRE_LABEL or not DMM_ARTICLE_ID):
    print('❌ ROUNDUP_MODE=genre のときは GENRE_LABEL と DMM_ARTICLE_ID の両方が必要です。')
    print('   （dmm_genre_search.py でジャンル名とIDを事前に調べてください）')
    sys.exit(1)

EXCLUDE_VR = os.environ.get('EXCLUDE_VR', 'true').strip().lower() not in ('false', '0', 'no')

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW_JST = datetime.datetime.now(JST)

# ranking モードの集計期間: week（今週配信分。過去分をアーカイブとして蓄積する用途）
#                        / month（今月配信分。1本のページを毎回上書き更新する用途）
RANKING_PERIOD = os.environ.get('RANKING_PERIOD', 'week').strip().lower()
if RANKING_PERIOD not in ('week', 'month'):
    RANKING_PERIOD = 'week'

if RANKING_PERIOD == 'week':
    # ISO週（月曜始まり）。同じ週内に何度実行しても同じ週として扱われる。
    _week_start_date = (NOW_JST - datetime.timedelta(days=NOW_JST.weekday())).date()
    _week_end_date = _week_start_date + datetime.timedelta(days=6)
    PERIOD_START = datetime.datetime.combine(_week_start_date, datetime.time(0, 0, 0), tzinfo=JST)
    PERIOD_END = NOW_JST
else:
    PERIOD_START = NOW_JST.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    PERIOD_END = NOW_JST

print('✅ 認証情報を読み込みました。')
print(f'🎯 モード: {ROUNDUP_MODE}' + (f'（ジャンル: {GENRE_LABEL} / article_id={DMM_ARTICLE_ID}）' if ROUNDUP_MODE == 'genre' else ''))


# ================================================================
# 🔧 DMM API
# ================================================================

def _is_vr_product(product: dict) -> bool:
    text = (product.get('title', '') + ' ' + ' '.join(product.get('genres', []))).upper()
    return 'VR' in text


def fetch_ranked_products(limit: int):
    """rank順（人気順）で、安全フィルター・VR除外を通過した上位limit件の商品情報を集めて返す。"""
    collected = []
    seen_ids = set()
    offset = 1
    hits = 100
    max_pages = 20  # 安全装置（最大2,000件まで遡る）

    params_base = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
        'sort':         'rank',
        'output':       'json',
    }
    if ROUNDUP_MODE == 'ranking':
        params_base['gte_date'] = PERIOD_START.strftime('%Y-%m-%dT%H:%M:%S')
        params_base['lte_date'] = PERIOD_END.strftime('%Y-%m-%dT%H:%M:%S')
    else:
        params_base['article'] = 'genre'
        params_base['article_id'] = DMM_ARTICLE_ID

    for page in range(max_pages):
        if len(collected) >= limit:
            break
        params = dict(params_base, hits=hits, offset=offset)
        try:
            resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
            data = resp.json()
            items = data.get('result', {}).get('items', [])
            if isinstance(items, dict):
                items = items.get('item', [])
        except Exception as e:
            print(f'❌ DMM APIエラー（offset={offset}）: {e}')
            break

        if not items:
            print('   ⏹ これ以上データがありません。')
            break

        for item in items:
            product = parse_product(item)
            if not product.get('content_id') or product['content_id'] in seen_ids:
                continue
            if EXCLUDE_VR and _is_vr_product(product):
                continue
            ok, _matched = is_safe(product)
            if not ok:
                continue
            seen_ids.add(product['content_id'])
            collected.append(product)
            if len(collected) >= limit:
                break

        offset += hits

    return collected[:limit]


def parse_product(item):
    content_id    = item.get('content_id', '') or item.get('product_id', '')
    title         = item.get('title', '')
    affiliate_url = item.get('affiliateURL', '') or item.get('URL', '')

    prices = item.get('prices', {}) or {}
    price_str, price_num = '', None
    price_val = prices.get('price') or prices.get('list_price') or ''
    if price_val:
        digits = ''.join(c for c in str(price_val) if c.isdigit())
        if digits:
            price_num = int(digits)
            price_str = f'¥{price_num:,}'

    genres = [g.get('name', '') for g in (item.get('iteminfo', {}).get('genre') or [])]

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg, review_count = 0.0, 0

    img = item.get('imageURL', {}) or {}
    package_image = img.get('large') or img.get('small') or ''

    return {
        'content_id':    content_id,
        'title':         title,
        'affiliate_url': affiliate_url,
        'price':         price_str,
        'price_num':     price_num,
        'genres':        genres,
        'review_avg':    round(review_avg, 2) if review_avg else None,
        'review_count':  review_count if review_count else None,
        'package_image': package_image,
    }


# ================================================================
# 📝 記事コンテンツ生成
# ================================================================

def _rank_blurb(product: dict, rank: int) -> str:
    parts = []
    if product.get('review_avg') and product.get('review_count'):
        parts.append(f"レビュー平均{product['review_avg']}（{product['review_count']}件）")
    if product.get('genres'):
        parts.append('・'.join(product['genres'][:3]))
    return ' / '.join(parts)


def build_ranking_title():
    if RANKING_PERIOD == 'week':
        start = _week_start_date
        end = _week_end_date
        if start.month == end.month:
            period_label = f'{start.month}月{start.day}日〜{end.day}日'
        else:
            period_label = f'{start.month}月{start.day}日〜{end.month}月{end.day}日'
        return f"【{start.year}年{period_label}】{CONTENT_LABEL}人気ランキングTOP{TOP_N}"
    return f"【{NOW_JST.year}年{NOW_JST.month}月】{CONTENT_LABEL}人気ランキングTOP{TOP_N}"


def build_genre_title():
    return f"【{GENRE_LABEL}】{CONTENT_LABEL}人気まとめTOP{TOP_N}"


def build_roundup_body(products: list) -> str:
    intro = (
        f"<p>{CONTENT_LABEL}の中から、rank順（人気順）でTOP{len(products)}をまとめました。"
        f"気になった作品はサンプルを確認してみてください。</p>"
    )
    items_html = []
    for i, p in enumerate(products, start=1):
        title = escape(p['title'])
        url = escape(p['affiliate_url'])
        img = escape(p['package_image'])
        price = escape(p['price']) if p['price'] else ''
        blurb = escape(_rank_blurb(p, i))

        items_html.append(f'''
<div class="ona-rank-item" style="display:flex;gap:14px;align-items:flex-start;margin:0 0 22px;padding-bottom:18px;border-bottom:1px solid #333;">
  <div class="ona-rank-num" style="flex:0 0 auto;font-size:22px;font-weight:bold;min-width:36px;">{i}</div>
  <a href="{url}" style="flex:0 0 auto;display:block;">
    <img src="{img}" alt="{title}" loading="lazy" style="width:140px;height:140px;object-fit:cover;border-radius:6px;display:block;">
  </a>
  <div class="ona-rank-info" style="flex:1 1 auto;">
    <a href="{url}" style="font-weight:bold;font-size:16px;text-decoration:none;">{title}</a>
    {f'<div style="margin-top:6px;">{price}</div>' if price else ''}
    {f'<div style="margin-top:4px;font-size:13px;color:#b3b3b3;">{blurb}</div>' if blurb else ''}
    <div style="margin-top:8px;"><a href="{url}" style="display:inline-block;padding:6px 14px;background:#cc2222;color:#fff;border-radius:999px;font-size:12px;text-decoration:none;">サンプル・詳細を見る</a></div>
  </div>
</div>''')

    return intro + '\n'.join(items_html)


def build_excerpt(products: list) -> str:
    top3 = '、'.join(p['title'][:20] for p in products[:3])
    return f"{top3} など、TOP{len(products)}をまとめました。"[:120]


# ================================================================
# 🔧 WordPress REST API
# ================================================================

def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
}

_category_cache = {}
_tag_cache = {}


def _get_or_create_term(taxonomy: str, name: str, cache: dict):
    if not name:
        return None
    if name in cache:
        return cache[name]
    endpoint = f'{WP_URL}/wp-json/wp/v2/{taxonomy}'
    try:
        resp = requests.get(endpoint, params={'search': name, 'per_page': 100},
                             auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15)
        if resp.status_code == 200:
            results = resp.json()
            if isinstance(results, list):
                for term in results:
                    if isinstance(term, dict) and term.get('name') == name:
                        cache[name] = term['id']
                        return term['id']
        resp = requests.post(endpoint, data=json.dumps({'name': name}).encode('utf-8'),
                              auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15)
        if resp.status_code in (200, 201):
            created = resp.json()
            if isinstance(created, dict) and 'id' in created:
                cache[name] = created['id']
                return created['id']
        err = resp.json() if resp.content else None
        if isinstance(err, dict) and err.get('code') == 'term_exists':
            existing_id = (err.get('data') or {}).get('term_id')
            if existing_id:
                cache[name] = existing_id
                return existing_id
    except Exception as e:
        print(f'    ⚠️ タクソノミー"{name}"の取得/作成エラー: {e}')
    return None


def _upload_featured_image(image_url: str, slug: str):
    if not image_url:
        return None
    try:
        import mimetypes
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext = mimetypes.guess_extension(content_type) or '.jpg'
        filename = f'featured-{slug}{ext}'
        resp = requests.post(
            f'{WP_URL}/wp-json/wp/v2/media', data=img_resp.content, auth=_wp_auth(),
            headers={'Content-Type': content_type,
                     'Content-Disposition': f'attachment; filename="{filename}"',
                     'Accept': 'application/json'},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return resp.json()['id']
        print(f'    ⚠️ アイキャッチ画像のアップロードに失敗 status={resp.status_code}: {resp.text[:200]}')
    except Exception as e:
        print(f'    ⚠️ アイキャッチ画像の取得/アップロードエラー: {e}')
    return None


def _find_existing_content_by_slug(slug: str, post_type: str):
    """同じslugのpost/pageが既にあれば、そのidを返す（無ければNone）。
    毎回新規記事を作らず、同じページを更新し続けるための重複防止。"""
    try:
        resp = requests.get(
            f'{WP_URL}/wp-json/wp/v2/{post_type}',
            params={'slug': slug, 'status': 'draft,pending,publish', 'context': 'edit'},
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
        )
        if resp.status_code == 200:
            results = resp.json()
            if isinstance(results, list) and results:
                return results[0]['id']
    except Exception as e:
        print(f'    ⚠️ 既存{("ページ" if post_type == "pages" else "記事")}の検索エラー: {e}')
    return None


def post_or_update_roundup(slug: str, title: str, body: str, excerpt: str,
                            featured_image_url: str, category_names: list, tag_names: list,
                            post_type: str = 'posts') -> bool:
    """post_type='posts' なら通常のブログ投稿（一覧・カテゴリーページに表示される）。
    post_type='pages' なら固定ページ（ブログの一覧には表示されず、独立したURLになる）。
    固定ページはカテゴリー・タグを持たないため、その場合はタクソノミー付与をスキップする。"""
    payload = {
        'title':   title,
        'slug':    slug,
        'excerpt': excerpt,
        'content': body,
        'status':  WP_POST_STATUS,
    }
    if post_type == 'posts':
        category_ids = [cid for cid in (
            _get_or_create_term('categories', name, _category_cache) for name in category_names
        ) if cid]
        tag_ids = [tid for tid in (
            _get_or_create_term('tags', name, _tag_cache) for name in tag_names
        ) if tid]
        payload['categories'] = category_ids
        payload['tags'] = tag_ids

    media_id = _upload_featured_image(featured_image_url, slug)
    # media_id が取れなかった場合も featured_media: 0 を明示することで、
    # 既存記事に前回設定されたアイキャッチが残ってしまわないようにする。
    payload['featured_media'] = media_id if media_id else 0

    kind_label = 'ページ' if post_type == 'pages' else '記事'
    existing_id = _find_existing_content_by_slug(slug, post_type)
    if existing_id:
        endpoint = f'{WP_URL}/wp-json/wp/v2/{post_type}/{existing_id}'
        verb = f'{kind_label}更新'
    else:
        endpoint = f'{WP_URL}/wp-json/wp/v2/{post_type}'
        verb = f'{kind_label}新規作成'

    try:
        resp = requests.post(endpoint, data=json.dumps(payload).encode('utf-8'),
                              auth=_wp_auth(), headers=_JSON_HEADERS, timeout=20)
        if resp.status_code in (200, 201):
            result = resp.json()
            if not isinstance(result, dict) or 'id' not in result:
                print(f'    ❌ {verb}失敗：想定外のレスポンス: {resp.text[:300]}')
                return False
            print(f"    ✅ {verb}成功（status={result.get('status')}）: {title}")
            print(f"       {result.get('link', '')}")
            return True
        print(f'    ❌ {verb}失敗 status={resp.status_code}: {resp.text[:300]}')
        return False
    except Exception as e:
        print(f'    ❌ {verb}エラー: {e}')
        return False


# ================================================================
# 🚀 メイン実行
# ================================================================

def main():
    print(f'\n🔎 rank順でTOP{TOP_N}を収集中...')
    products = fetch_ranked_products(TOP_N)
    print(f'📦 {len(products)}件 集まりました。')

    if not products:
        print('⚠️ 対象の作品が見つかりませんでした。ジャンル指定や期間の設定を見直してください。')
        sys.exit(1)

    if ROUNDUP_MODE == 'ranking':
        title = build_ranking_title()
        # 週単位で蓄積するアーカイブにするため、スラッグに週の開始日を含める。
        # 同じ週内に再実行すれば同じ記事を上書き更新、週が変われば新しい記事が増える。
        if RANKING_PERIOD == 'week':
            slug = f'ranking-{CONTENT_TYPE}-{_week_start_date.strftime("%Y%m%d")}'
        else:
            slug = f'ranking-{CONTENT_TYPE}-{NOW_JST.year}{NOW_JST.month:02d}'
        category_names = [CONTENT_LABEL, 'ランキング']
        post_type = 'posts'
    else:
        title = build_genre_title()
        slug = f'genre-roundup-{CONTENT_TYPE}-{DMM_ARTICLE_ID}'
        category_names = [CONTENT_LABEL, 'ジャンルまとめ', GENRE_LABEL]
        post_type = 'posts'

    body = build_roundup_body(products)
    excerpt = build_excerpt(products)
    # アイキャッチ画像は1位の作品のサムネイルを使う。
    # 「ランキング」カテゴリー一覧ページでは他の記事と同じ形式でカード表示され、
    # 個別記事ページ側ではCSS（body.single/.page 用のルール）で非表示にしている。
    featured_image_url = products[0]['package_image']

    # 個々の作品ジャンルも、回遊性のためタグとして付与する（重複は自動で除外される）
    tag_names = []
    for p in products:
        for g in p.get('genres', [])[:2]:
            if g not in tag_names:
                tag_names.append(g)

    print(f'\n📝 投稿中: {title}')
    ok = post_or_update_roundup(slug, title, body, excerpt, featured_image_url,
                                 category_names, tag_names, post_type=post_type)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
