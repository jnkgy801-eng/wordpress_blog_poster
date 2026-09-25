# -*- coding: utf-8 -*-
"""
📝 品番（content_id）＋作品概要だけ入力 → WordPress 記事自動生成・下書き投稿ツール

「作品概要」以外の情報（作品名・ジャンル・価格・パッケージ画像・サンプル画像・
アフィリエイトリンク等）は、入力された品番をキーにDMM APIから自動取得する。
そのうえで、閲覧者の興味を引く構成（ジャンルバッジ・価格バッジ・レビュー星評価・
自動生成ポイント一覧・サンプル画像ギャラリー・CTAボタン）で記事を組み立て、
WordPress REST APIへ下書き（draft）として投稿する。

【人間が入力するのは実質2項目だけ】
  ・WORK_CONTENT_ID（品番。例: "d_123456"）
  ・WORK_OVERVIEW（作品概要。自分の言葉での紹介文。1〜2文でも可）

【DMM APIから自動取得する項目】
  作品名・ジャンル・サークル/メーカー名・価格・パッケージ画像・サンプル画像・
  サンプル動画・レビュー評価・シリーズ名・アフィリエイトリンク 等
  （wordpress_blog_poster.py の parse_product() と同じロジックを使用）

【「興味を引く」ための自動生成部分】
  ・ポイント一覧: ジャンル・レビュー・サークル名から自動で複数パターンの
    キャッチーな文言を生成する（wordpress_blog_poster.py のテンプレート
    ロジックを流用。AI（Gemini）は使わないため追加コスト・APIキー不要）
  ・レビュー星評価・ジャンルバッジ・価格バッジ・サンプル画像ギャラリーを
    自動でレイアウトし、視覚的に注目を引く構成にする

【自動版（wordpress_blog_poster.py）との違い】
  ・複数作品を自動収集する機能は無く、指定した品番1件のみを処理する
  ・age_safety_filter（年少者連想ワードの自動除外）は使わない
    → 品番を指定する時点で人間が対象作品を選んでいる前提のため
  ・AI（Gemini）による本文生成は行わない（テンプレート生成のみ）
--------------------------------------------------------------
"""

import os
import re
import sys
import json
import datetime
import urllib.parse
from xml.sax.saxutils import escape

import requests

JST = datetime.timezone(datetime.timedelta(hours=9))

# ================================================================
# 📌 スクリプトバージョン（デプロイ確認用）
# ================================================================
SCRIPT_VERSION = '2026-09-25-cid-01'

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

WP_URL          = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME     = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '')

WP_POST_STATUS = os.environ.get('WP_POST_STATUS', 'draft').lower()

# --- 人間が入力する項目 -------------------------------------------------
WORK_CONTENT_ID = os.environ.get('WORK_CONTENT_ID', '').strip()   # 品番（必須）
WORK_OVERVIEW   = os.environ.get('WORK_OVERVIEW', '').strip()     # 作品概要（必須）
WORK_CAUTION    = os.environ.get('WORK_CAUTION', '').strip()      # 気になる点・注意点（任意）
                                                                    # 「公式が謳っていないデメリットやリスクも
                                                                    # 書くと信頼性が上がる」という商品レビュー記事の
                                                                    # セオリーに対応した任意項目。入力が無ければ
                                                                    # セクション自体を出さない（嘘の欠点は作らない）。

# コンテンツ種別（品番の検索対象floorを決める）。doujin（同人）/ av（動画）
CONTENT_TYPE = os.environ.get('CONTENT_TYPE', 'doujin').strip().lower()
_CONTENT_TYPE_TARGETS = {
    'doujin': {'service': 'doujin', 'floor': 'digital_doujin', 'label': '同人'},
    'av':     {'service': 'digital', 'floor': 'videoa', 'label': '動画'},
}
if CONTENT_TYPE not in _CONTENT_TYPE_TARGETS:
    print(f'⚠️ CONTENT_TYPE="{CONTENT_TYPE}" は不明な値です。doujin にフォールバックします。')
    CONTENT_TYPE = 'doujin'
SERVICE            = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['service']
FLOOR              = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['floor']
DEFAULT_CATEGORY   = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['label']
WORK_CATEGORY_LABEL = os.environ.get('WORK_CATEGORY_LABEL', DEFAULT_CATEGORY).strip()

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)

if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)

if WP_POST_STATUS not in ('draft', 'pending', 'publish'):
    print(f'⚠️ WP_POST_STATUS="{WP_POST_STATUS}" は不明な値です。draft にフォールバックします。')
    WP_POST_STATUS = 'draft'

if not WORK_CONTENT_ID:
    print('❌ 環境変数 WORK_CONTENT_ID（品番）が設定されていません。')
    sys.exit(1)

if not WORK_OVERVIEW:
    print('❌ 環境変数 WORK_OVERVIEW（作品概要）が設定されていません。')
    sys.exit(1)

print('✅ 認証情報を読み込みました。')
print(f'🏷️ スクリプトバージョン: {SCRIPT_VERSION}')
print(f'📌 品番: {WORK_CONTENT_ID}（{DEFAULT_CATEGORY}／service={SERVICE}, floor={FLOOR}）')
if WP_POST_STATUS == 'publish':
    print('🚨 投稿ステータス: publish（本公開）が指定されています。')
else:
    print(f'📌 投稿ステータス: {WP_POST_STATUS}（公開は必ず手動で行ってください）')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'


# ================================================================
# 🔧 DMM API：品番から作品情報を取得
# ================================================================

def fetch_product_by_cid(cid: str):
    """指定した品番（content_id）の商品情報をDMM APIから1件取得する。"""
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
        'cid':          cid,
        'output':       'json',
    }
    try:
        resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        if not items:
            print(f'❌ 品番「{cid}」に一致する作品がDMM APIから見つかりませんでした。'
                  f'品番の入力ミス、またはCONTENT_TYPE（doujin/av）の指定違いの可能性があります。')
            return None
        return items[0]
    except Exception as e:
        print(f'❌ DMM APIエラー（cid={cid}）: {e}')
        return None


_TITLE_PREFIX_RE = re.compile(r'^【[^】]{1,20}】\s*')


def _strip_redundant_title_prefix(title: str) -> str:
    if not title:
        return title
    return _TITLE_PREFIX_RE.sub('', title, count=1).strip()


def _build_affiliate_url(raw_url: str) -> str:
    if not raw_url:
        return ''
    parsed = urllib.parse.urlsplit(raw_url)
    clean_query = '&'.join(
        kv for kv in parsed.query.split('&')
        if kv and not kv.startswith('utm_')
    )
    clean_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, clean_query, '')
    )
    encoded = urllib.parse.quote(clean_url, safe='')
    return f'https://al.dmm.co.jp/?lurl={encoded}&af_id={DMM_AFFILIATE_ID}&ch=api&ch_id=link'


def _resolve_affiliate_url(item: dict) -> str:
    raw_affiliate = item.get('affiliateURL', '')
    if raw_affiliate and ('al.dmm.co.jp' in raw_affiliate or 'al.fanza.co.jp' in raw_affiliate):
        return raw_affiliate
    fallback_source = raw_affiliate or item.get('URL', '')
    return _build_affiliate_url(fallback_source)


def parse_product(item: dict) -> dict:
    content_id    = item.get('content_id', '') or item.get('product_id', '')
    title         = _strip_redundant_title_prefix(item.get('title', ''))
    affiliate_url = _resolve_affiliate_url(item)
    prices        = item.get('prices', {})
    price_str, price_num = '', None
    if prices:
        price_val = prices.get('price') or prices.get('list_price') or ''
        if price_val:
            digits = ''.join(c for c in str(price_val) if c.isdigit())
            if digits:
                price_num = int(digits)
                price_str = f'¥{price_num:,}'

    iteminfo = item.get('iteminfo', {}) or {}
    genres   = [g.get('name', '') for g in (iteminfo.get('genre') or [])]
    maker    = ((iteminfo.get('maker') or [{}])[0]).get('name', '')
    actors   = [a.get('name', '') for a in (iteminfo.get('actress') or []) if a.get('name')][:3]
    series   = ((iteminfo.get('series') or [{}])[0]).get('name', '')

    sample_movie_url = ''
    movie_block = item.get('sampleMovieURL', {}) or {}
    for key in ('size_720_480', 'size_644_414', 'size_560_360', 'size_476_306'):
        if movie_block.get(key):
            sample_movie_url = movie_block[key]
            break

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg, review_count = 0.0, 0
    review_avg   = round(review_avg, 2) if review_avg else None
    review_count = review_count if review_count else None

    img = item.get('imageURL', {}) or {}
    package_image = img.get('large') or img.get('small') or ''

    sample_images = []
    sample_url_block = item.get('sampleImageURL', {}) or {}
    for key in ('sample_l', 'sample_s'):
        block = sample_url_block.get(key) or {}
        images = block.get('image') or []
        if isinstance(images, str):
            images = [images]
        if images:
            sample_images = [u for u in images if u]
            break

    return {
        'content_id':    content_id,
        'title':         title,
        'affiliate_url': affiliate_url,
        'price':         price_str,
        'price_num':     price_num,
        'genres':        genres,
        'maker':         maker,
        'actors':        actors,
        'series':        series,
        'review_avg':    review_avg,
        'review_count':  review_count,
        'package_image': package_image,
        'sample_images': sample_images,
        'sample_movie_url': sample_movie_url,
    }


# ================================================================
# 📝 「興味を引く」ポイント一覧の自動生成（テンプレート方式・AI不要）
# ================================================================

_GENRE_POINT_TEMPLATES = [
    '{g}好きなら、うっかり夜更かし確定の内容です',
    '{g}成分が気になる方は、もう指がカートに伸びているはず',
    '{g}のツボを心得た一作。油断してると即決してしまいます',
    '{g}好きにこっそり教えたい、隠れた掘り出し物です',
]

_OVERVIEW_CLOSERS = [
    '気づいたら作品ページを開いている……そんな自分に気づいても、責めないであげてください。',
    '買う理由を探すより、買わない理由を探す方が難しい一作です。',
    '迷っている時間があるなら、その時間でもう読み終わっているかもしれません。',
]


def _stable_pick(items: list, key: str):
    """content_idベースの安定ハッシュでリストから1つ選ぶ（毎回同じ結果になる）。"""
    if not items:
        return None
    idx = sum(ord(c) for c in key) % len(items) if key else 0
    return items[idx]


def _build_auto_points(product: dict) -> list:
    """ジャンル・レビュー・サークル名から、閲覧者の興味を引くポイント一覧を自動生成する。"""
    points = []
    for i, g in enumerate((product.get('genres') or [])[:4]):
        tmpl = _GENRE_POINT_TEMPLATES[i % len(_GENRE_POINT_TEMPLATES)]
        points.append(tmpl.format(g=g))
    if product.get('review_avg') and product.get('review_count'):
        points.append(f"レビュー平均{product['review_avg']}点（{product['review_count']}件）と、みんなも太鼓判")
    if product.get('maker'):
        points.append(f"制作は{product['maker']}。安定した仕上がりも安心材料のひとつです")
    if product.get('series'):
        points.append(f"「{product['series']}」シリーズの1作。過去作のファンにもおすすめです")
    if not points:
        points = ['作品ページを開いた時点で、もう半分ハマっています']
    return points[:6]


def _build_recommend_for(product: dict) -> list:
    """
    「こんな人におすすめ」を自動生成する。
    ジャンルタグをそのまま「〇〇が好きな人」に変換するだけのシンプルな方式。
    レビュー参考記事の「どんな人にオススメか」を明記すべき、という指摘に対応。
    """
    recommends = []
    for g in (product.get('genres') or [])[:4]:
        recommends.append(f'{g}が好きな人')
    if product.get('review_avg') and (product.get('review_count') or 0) >= 30:
        recommends.append('レビュー件数が多い、実績のある作品を選びたい人')
    if product.get('series'):
        recommends.append(f'「{product["series"]}」シリーズが気になっている人')
    return recommends[:5]


def _build_closing_line(product: dict) -> str:
    key = product.get('content_id') or product.get('title') or ''
    return _stable_pick(_OVERVIEW_CLOSERS, key) or _OVERVIEW_CLOSERS[0]


def _build_focus_keyphrase(product: dict, max_words: int = 2, max_chars: int = 12) -> str:
    words = []
    for g in (product.get('genres') or []):
        if len(words) >= max_words:
            break
        g = (g or '').strip()
        if g and len(g) > 1 and g not in words:
            words.append(g)
    if not words and product.get('maker'):
        words.append(product['maker'].strip())
    while len(words) > 1 and len(' '.join(words)) > max_chars:
        words.pop()
    return ' '.join(words[:max_words])


# ================================================================
# 📝 HTMLパーツ生成
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


def _genre_badges_html(genres: list) -> str:
    if not genres:
        return ''
    badges = []
    for i, g in enumerate(genres[:5]):
        color = _BADGE_COLORS[i % len(_BADGE_COLORS)]
        badges.append(
            f'<span style="display:inline-block;background:{color};color:#fff;'
            'padding:4px 12px;border-radius:999px;font-size:12px;font-weight:bold;'
            f'margin:2px 4px 2px 0;">{escape(g)}</span>'
        )
    return f'<div style="margin:8px 0;">{"".join(badges)}</div>'


def _star_rating_html(avg, count) -> str:
    if not avg or not count:
        return ''
    filled = max(0, min(5, round(avg)))
    stars = '★' * filled + '☆' * (5 - filled)
    return (
        '<div style="margin:6px 0;">'
        f'<span style="color:#f5a623;font-size:18px;letter-spacing:1px;">{stars}</span> '
        f'<span style="color:#888;font-size:13px;">{avg}点（{count}件のレビュー）</span>'
        '</div>'
    )


def _points_list_html(points: list, heading: str = '✓ ここがポイント') -> str:
    if not points:
        return ''
    items = ''.join(
        f'<li style="margin:6px 0;line-height:1.6;">{escape(pt)}</li>'
        for pt in points
    )
    return (
        '<div class="ona-points-box">'
        f'<h2 class="ona-points-title" style="margin:0 0 8px;font-size:16px;">{escape(heading)}</h2>'
        f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
        '</div>'
    )


def _recommend_for_html(recommends: list) -> str:
    """「こんな人におすすめ」セクション。タグ風のチップで軽く見せる。"""
    if not recommends:
        return ''
    chips = ''.join(
        '<span style="display:inline-block;background:#eef6ff;color:#2b6cb0;'
        'border:1px solid #bcdcff;padding:4px 12px;border-radius:999px;'
        f'font-size:13px;margin:2px 4px 2px 0;">👤 {escape(r)}</span>'
        for r in recommends
    )
    return (
        '<h2 style="margin:0 0 8px;font-size:17px;">🙋 こんな人におすすめ</h2>'
        f'<div>{chips}</div>'
    )


def _caution_html(caution_text: str) -> str:
    """
    「気になる点・注意点」セクション。手動入力(WORK_CAUTION)がある場合のみ表示する。
    レビュー記事の信頼性向上（公式が言わないデメリットも書く）に対応した任意項目。
    """
    if not caution_text:
        return ''
    body = _paragraphs_to_html(caution_text)
    return (
        '<h2 style="margin:0 0 8px;font-size:17px;">⚠️ ここは事前に知っておきたいポイント</h2>'
        f'<div>{body}</div>'
    )


def _sample_gallery_html(affiliate_url: str, sample_images: list, title: str,
                          heading: str = '作品サンプル') -> str:
    imgs = [u for u in (sample_images or []) if u][:8]
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


def _sample_video_html(sample_movie_url: str) -> str:
    if not sample_movie_url:
        return ''
    return (
        '<div class="ona-sample-video" style="margin:14px 0;">'
        '<h3 style="margin:0 0 8px;font-size:15px;">サンプル動画</h3>'
        '<div style="position:relative;width:100%;max-width:560px;aspect-ratio:560/360;">'
        f'<iframe src="{escape(sample_movie_url)}" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;border-radius:8px;" '
        'allow="autoplay; fullscreen" allowfullscreen loading="lazy"></iframe>'
        '</div>'
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


def _build_seo_title(product: dict, keyphrase: str = '', max_len: int = 32) -> str:
    title = (product.get('title') or '').strip()
    if not keyphrase:
        return title[:max_len]
    if keyphrase in title:
        return title[:max_len]
    remaining = max_len - len(keyphrase) - 1
    if remaining <= 0:
        return keyphrase[:max_len]
    return f'{keyphrase} {title[:remaining].rstrip()}'


# ================================================================
# 📝 記事の組み立て
# ================================================================

def _hero_html(product: dict) -> str:
    """
    カード最上部：パッケージ画像＋価格＋星評価＋CTAをまとめた
    「ファーストビューで即決できる」エリア。
    ここだけ見れば「どんな作品で、いくらで、評判はどうか」が分かるようにする。
    """
    img_html = ''
    if product.get('package_image'):
        img_html = (
            f'<img src="{escape(product["package_image"])}" alt="{escape(product["title"])}" '
            'style="width:100%;display:block;border-radius:12px 12px 0 0;" loading="lazy">'
        )

    quick_badges = []
    if product.get('price'):
        quick_badges.append(
            '<span style="display:inline-block;background:#e0507a;color:#fff;'
            'padding:5px 14px;border-radius:999px;font-size:14px;font-weight:bold;'
            f'margin:0 6px 6px 0;">💰 {escape(product["price"])}</span>'
        )
    if product.get('review_avg') and product.get('review_count'):
        quick_badges.append(
            '<span style="display:inline-block;background:#fff3d6;color:#a67c00;'
            'padding:5px 14px;border-radius:999px;font-size:14px;font-weight:bold;'
            f'margin:0 6px 6px 0;">⭐ {product["review_avg"]}（{product["review_count"]}件）</span>'
        )
    quick_badges_html = ''.join(quick_badges)

    hero_cta = (
        '<div style="text-align:center;margin-top:10px;">'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        'style="display:inline-block;padding:12px 32px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:15px;font-weight:bold;'
        'box-shadow:0 4px 12px rgba(224,80,122,0.35);">'
        '🔥 今すぐ作品ページをチェック</a></div>'
    )

    return (
        '<div style="margin:-20px -20px 0;">'
        f'{img_html}'
        '<div style="padding:14px 20px 4px;text-align:center;">'
        f'{quick_badges_html}'
        f'{hero_cta}'
        '</div></div>'
    )


def _section(inner_html: str, is_first: bool = False) -> str:
    """
    セクションを区切り線付きのブロックで包む。
    見出し単位で視覚的に区切ることで、情報のまとまりを把握しやすくする。
    """
    if not inner_html:
        return ''
    if is_first:
        style = 'margin-top:18px;'
    else:
        style = 'margin-top:18px;padding-top:18px;border-top:1px dashed #e5c6d3;'
    return f'<div style="{style}">{inner_html}</div>'


def build_article(product: dict) -> dict:
    focus_keyphrase = _build_focus_keyphrase(product)
    genre_str = '、'.join(product.get('genres', [])[:3]) or '注目'

    # ---- 概要セクション: 冒頭文にフォーカスキーフレーズと作品名を必ず含める ----
    # （Yoastの「冒頭のキーフレーズ」チェック対策。GSC上のクエリ一致率向上にも寄与）
    lead_sentence = (
        f'今回紹介するのは「{focus_keyphrase}」が魅力の{DEFAULT_CATEGORY}作品'
        f'『{product["title"]}』です。'
    )
    overview_text = f'{lead_sentence}\n\n{WORK_OVERVIEW}\n\n{_build_closing_line(product)}'
    overview_html = _paragraphs_to_html(overview_text)

    points = _build_auto_points(product)
    points_html = _points_list_html(points, heading='✓ ここがポイント')
    genre_badges_html = _genre_badges_html(product.get('genres', []))
    gallery_html = _sample_gallery_html(
        product.get('affiliate_url', ''), product.get('sample_images', []), product.get('title', ''),
        heading='📸 作品サンプル',
    )
    video_html = _sample_video_html(product.get('sample_movie_url', ''))

    meta_line_html = ''
    if product.get('maker'):
        meta_line_html = (
            '<div style="color:#666;font-size:13px;margin:4px 0 10px;">'
            f'🏷️ サークル: {escape(product["maker"])}</div>'
        )

    # ---- H2見出しでセクションを分割する ----
    # 1つの<h2>に情報を詰め込みすぎず、検索クエリと一致しやすい見出しを
    # 複数用意することで、記事の網羅性・構造の分かりやすさを高める。
    # 絵文字を付けることでスキャン（流し読み）時の視線誘導をしやすくする。
    overview_section_html = (
        f'<h2 style="margin:0 0 8px;font-size:17px;">📖 『{escape(product["title"])}』はどんな作品？</h2>'
        f'<div>{overview_html}</div>'
        f'{meta_line_html}'
    )

    genre_section_html = ''
    if product.get('genres'):
        genre_section_html = (
            '<h2 style="margin:0 0 8px;font-size:17px;">🎯 ジャンル・見どころ</h2>'
            f'{genre_badges_html}'
        )

    price_section_html = ''
    if product.get('price'):
        price_section_html = (
            '<h2 style="margin:0 0 8px;font-size:17px;">💰 価格・購入方法</h2>'
            '<div style="display:inline-block;background:#fff0f5;color:#e0507a;'
            'border:1px solid #ffc2d6;border-radius:8px;padding:6px 14px;'
            f'font-size:15px;font-weight:bold;margin:6px 0;">価格 {escape(product["price"])}</div>'
            '<p style="font-size:13px;color:#666;margin-top:6px;">'
            '作品ページから購入手続きに進めます（ダウンロード形式）。</p>'
        )

    points_section_html = points_html  # 見出しは_points_list_html内で既に付与済み

    recommend_section_html = _recommend_for_html(_build_recommend_for(product))
    caution_section_html = _caution_html(WORK_CAUTION)

    cta_html = (
        '<div style="text-align:center;">'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        'style="display:inline-block;padding:14px 36px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:16px;font-weight:bold;'
        'box-shadow:0 4px 12px rgba(224,80,122,0.35);">'
        f'▶「{escape(product["title"][:20])}」の作品ページを見る</a></div>'
    )

    footer_html = ''
    footer_parts = []
    if WP_URL:
        footer_parts.append(
            f'<p style="font-size:13px;margin:14px 0 0;text-align:center;">'
            f'<a href="{escape(WP_URL)}/category/{escape(WORK_CATEGORY_LABEL)}/">'
            f'他の{escape(WORK_CATEGORY_LABEL)}作品もチェックする →</a></p>'
        )
        # レビュー記事から比較・ランキング的なページへの内部リンクは
        # SEO・回遊率の両面で有効なため、先頭ジャンルのタグアーカイブにもリンクする。
        first_genre = (product.get('genres') or [None])[0]
        if first_genre:
            footer_parts.append(
                f'<p style="font-size:13px;margin:4px 0 0;text-align:center;">'
                f'<a href="{escape(WP_URL)}/tag/{escape(urllib.parse.quote(first_genre))}/">'
                f'「{escape(first_genre)}」の他の作品も見る →</a></p>'
            )
    footer_parts.append(
        '<p style="color:#999;font-size:12px;line-height:1.6;margin:10px 0 0;text-align:center;">'
        '※成人向けコンテンツを含みます。18歳未満の方はご利用いただけません。</p>'
    )
    footer_html = ''.join(footer_parts)

    # ---- ヒーローエリア（画像＋価格＋星評価＋CTA）は必ず最初に置く ----
    hero_html = _hero_html(product)

    # レビューが多い作品は星評価をヒーローエリアに含めているため、
    # 本文側では星評価バッジを重複させず、その分ジャンルを少し前に出す。
    section_blocks = [
        _section(overview_section_html, is_first=True),
        _section(genre_section_html),
        _section(price_section_html),
        _section(points_section_html),
        _section(recommend_section_html),
        _section(caution_section_html),
        _section(gallery_html),
        _section(video_html),
        _section(cta_html),
        footer_html,
    ]

    card_inner = hero_html + '\n'.join(filter(None, section_blocks))

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    seo_title = _build_seo_title(product, keyphrase=focus_keyphrase, max_len=20)
    excerpt = _make_description_excerpt(WORK_OVERVIEW, product['title'], max_len=55)

    tag_source = list(product.get('genres') or [])
    actor_source = list(product.get('actors') or []) if CONTENT_TYPE == 'av' else []

    return {
        'title':             product['title'],
        'slug':              _make_slug(product.get('content_id', ''), product['title']),
        'excerpt':           excerpt,
        'body':              body_html,
        'tags':              tag_source,
        'actors':            actor_source,
        'category_label':    WORK_CATEGORY_LABEL,
        'featured_image_url': product.get('package_image', ''),
        'content_id':        product.get('content_id', ''),
        'focus_keyphrase':   focus_keyphrase,
        'seo_title':         seo_title,
        'affiliate_url':     product.get('affiliate_url', ''),
    }


# ================================================================
# 🔐 WordPress REST API 投稿
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

    category_label = article.get('category_label') or DEFAULT_CATEGORY
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
                print(f"    ❌ 投稿失敗：WordPressから投稿データが返りませんでした: {resp.text[:300]}")
                return False
            actual_status = result.get('status')
            if actual_status != WP_POST_STATUS:
                print(f"    ⚠️ 指定したステータス（{WP_POST_STATUS}）と異なる値が返りました"
                      f"（status={actual_status}）。念のため内容をご確認ください: {result.get('link', '')}")
            print(f"    ✅ {actual_status}として投稿成功: {article['title'][:40]}")
            print(f"    🔗 link: {result.get('link', '')}")
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
    print(f'\n🔎 品番「{WORK_CONTENT_ID}」の作品情報をDMM APIから取得します...')
    raw_item = fetch_product_by_cid(WORK_CONTENT_ID)
    if not raw_item:
        sys.exit(1)

    product = parse_product(raw_item)
    print(f'✅ 取得成功: {product["title"][:50]}')
    print(f'   ジャンル: {"、".join(product["genres"][:5]) or "不明"} / 価格: {product.get("price") or "不明"}')

    print(f'\n📝 記事生成中...')
    article = build_article(product)
    ok = post_draft_to_wordpress(article)
    if ok:
        print(f'\n✅ 完了！WordPressに{WP_POST_STATUS}として投稿しました。')
        print('   ※ 公開前に必ず内容をご確認ください。')
    else:
        print('\n❌ 投稿に失敗しました。ログを確認してください。')
        sys.exit(1)


if __name__ == '__main__':
    main()
