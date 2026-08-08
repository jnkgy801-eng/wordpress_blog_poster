# -*- coding: utf-8 -*-
"""
🔁 既存記事の本文を「新テンプレート」で再生成し、WordPressを更新するツール
--------------------------------------------------------------
wordpress_blog_poster.py で過去にテンプレート生成された記事
（AI生成が使えず _get_article_body_template にフォールバックしたもの）は、
文章の構文がほぼ共通で「クロール済み-インデックス未登録」の一因になっている。

このスクリプトは:
  1. WordPressから既存の投稿一覧を取得する
  2. 各投稿のslug（= DMMのcontent_id）から、DMM APIで商品情報を再取得する
  3. 新しい（バリエーション豊富な）テンプレートで本文を再生成する
  4. タイトル・スラッグ・カテゴリー・タグ・アイキャッチ画像は変更せず、
     本文（content）・抜粋（excerpt）・Yoastメタのみを PUT で更新する

事前準備:
  - wordpress_blog_poster.py と同じ環境変数（DMM_API_ID / DMM_AFFILIATE_ID /
    WP_URL / WP_USERNAME / WP_APP_PASSWORD / CONTENT_TYPE）を設定しておくこと
  - CONTENT_TYPE は対象記事の種類（doujin / av）に合わせて実行時に指定する
    （同人とAVが混在している場合は、2回に分けて実行する）

実行例:
  # 同人記事を対象に、まずは3件だけ試す（本番反映前に必ずドライランで確認）
  DRY_RUN=true MAX_UPDATE=3 CONTENT_TYPE=doujin python wordpress_retemplate_existing_posts.py

  # 問題なければ本番反映
  DRY_RUN=false MAX_UPDATE=50 CONTENT_TYPE=doujin python wordpress_retemplate_existing_posts.py
"""

import os
import re
import sys
import json
import time
import hashlib
import datetime
import urllib.parse

import requests
from pathlib import Path
from xml.sax.saxutils import escape

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

WP_URL           = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME      = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD  = os.environ.get('WP_APP_PASSWORD', '')

CONTENT_TYPE = os.environ.get('CONTENT_TYPE', 'doujin').strip().lower()
_CONTENT_TYPE_TARGETS = {
    'doujin': {'service': 'doujin', 'floor': 'digital_doujin', 'label': 'FANZA同人'},
    'av':     {'service': 'digital', 'floor': 'videoa', 'label': 'FANZA動画'},
}
if CONTENT_TYPE not in _CONTENT_TYPE_TARGETS:
    print(f'⚠️ CONTENT_TYPE="{CONTENT_TYPE}" は不明な値です。doujin にフォールバックします。')
    CONTENT_TYPE = 'doujin'
SERVICE       = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['service']
FLOOR         = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['floor']
CONTENT_LABEL = _CONTENT_TYPE_TARGETS[CONTENT_TYPE]['label']

# true の場合、WordPressへの実際の更新は行わず、生成結果をログ出力するだけ
DRY_RUN = os.environ.get('DRY_RUN', 'true').strip().lower() not in ('false', '0', 'no')

# 1回の実行で更新する記事数の上限（安全のため小さめに設定）
MAX_UPDATE = int(os.environ.get('MAX_UPDATE', '20'))

# 更新対象を絞り込むための、投稿の取得ステータス（カンマ区切り）
WP_STATUSES = os.environ.get('WP_STATUSES', 'publish,draft,pending,future')

# 更新済み履歴ファイル（同じ記事を何度も再生成しないようにする）
RETEMPLATE_HISTORY_FILE = Path(os.environ.get('RETEMPLATE_HISTORY_FILE', 'outputs/retemplate_history.json'))

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)
if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)

print(f'📌 対象コンテンツ種別: {CONTENT_LABEL}（service={SERVICE}, floor={FLOOR}）')
print(f'📌 DRY_RUN={DRY_RUN} / MAX_UPDATE={MAX_UPDATE} / 対象ステータス={WP_STATUSES}')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'

_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}


def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


# ================================================================
# 🗂️ 更新済み履歴管理（重複再生成防止）
# ================================================================

def load_history() -> set:
    if not RETEMPLATE_HISTORY_FILE.exists():
        return set()
    try:
        with open(RETEMPLATE_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('updated', []))
    except Exception as e:
        print(f'⚠️ 更新履歴の読み込みに失敗しました（新規履歴として扱います）: {e}')
        return set()


def save_history(history: set) -> None:
    RETEMPLATE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(RETEMPLATE_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({'updated': sorted(history)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 更新履歴の保存に失敗しました: {e}')


# ================================================================
# 🔧 DMM API：content_id（=WordPressのslug）から商品情報を1件取得する
# ================================================================

def fetch_dmm_product_by_cid(cid: str) -> dict:
    """cid（content_id）を指定してDMM APIから単一商品を取得する。"""
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
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
        if not items:
            return None
        return items[0]
    except Exception as e:
        print(f'    ❌ DMM APIエラー（cid={cid}）: {e}')
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

    genres = [g.get('name', '') for g in (item.get('iteminfo', {}).get('genre') or [])]
    maker  = ((item.get('iteminfo', {}).get('maker') or [{}])[0]).get('name', '')
    actors = [a.get('name', '') for a in (item.get('iteminfo', {}).get('actress') or []) if a.get('name')][:3]

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
        'review_avg':    review_avg,
        'review_count':  review_count,
        'package_image': package_image,
        'sample_images': sample_images,
        'date':          item.get('date', ''),
    }


# ================================================================
# 📝 新テンプレート（バリエーション豊富版）
# ================================================================

def _pick(seed_key: str, salt: str, options: list):
    """content_id等をシードに、決定論的にoptionsから1つ選ぶ。
    同じ商品・同じsaltなら常に同じ結果になるため、再実行しても文面がブレない。"""
    h = hashlib.md5(f'{seed_key}:{salt}'.encode('utf-8')).hexdigest()
    idx = int(h, 16) % len(options)
    return options[idx]


_OPENERS = [
    '{genre_str}系の{work_kind}です。',
    '今回紹介するのは、{genre_str}要素が光る{work_kind}。',
    '{genre_str}好きなら見逃せない{work_kind}が登場しました。',
    '{work_kind}の中でも、{genre_str}のテイストが強めの一本です。',
    '注目したいのは、{genre_str}を軸にした{work_kind}であること。',
]

_KEYPHRASE_OPENERS = [
    '「{kw}」に注目の{work_kind}。{genre_str}系の内容です。',
    '「{kw}」が気になる方に向けた{work_kind}で、{genre_str}要素も充実しています。',
    '「{kw}」というキーワードがしっくりくる、{genre_str}系の{work_kind}です。',
]

_ACTOR_LINES = [
    ' 出演しているのは{actor_str}。この時点でもう見る理由は十分揃っています。',
    ' キャストは{actor_str}。名前を見ただけで期待値が上がる方も多いはずです。',
    ' {actor_str}が出演というだけで、チェックする価値は十分にあります。',
]

_MAKER_LINES = [
    ' ちなみに、手がけるのは{maker}。',
    ' 制作を担当しているのは{maker}です。',
    ' {maker}が手がけている点も、選ぶ際のひとつの安心材料です。',
]

_PRICE_LINES = [
    ' さらに価格は{price}なので、この内容ならコスパも十分満足できるはずです。',
    ' 価格は{price}。内容を考えると、むしろ手頃に感じるかもしれません。',
    ' 気になる価格は{price}。ここは実際に見てから判断してみてください。',
    ' {price}という価格設定も、検討材料として押さえておきたいところです。',
]

_GENRE_APPEAL_LINES = [
    ' つまり、{genre_str}といったジャンルが好きな方はもちろん、普段あまりこの手のジャンルを見ない方にも新鮮に映る一本というわけです。',
    ' {genre_str}が好きな方には刺さる内容ですが、初めて触れる方にも入りやすい構成になっています。',
    ' 特に{genre_str}のファンには外せない要素が詰まっている一方、初見の方でも十分楽しめる内容です。',
]

_SAMPLE_LINES = [
    ' サンプル画像だけでも見応え十分なので、まずは雰囲気だけでもチェックしてみると早いかもしれません。',
    ' サンプル画像を見るだけでも、作品の雰囲気は十分に伝わってくるはずです。',
    ' 詳細に入る前に、まずはサンプル画像で世界観を確認してみるのもおすすめです。',
]

_REVIEW_LINES = [
    ' 実際、レビューでも平均{avg}点（{count}件）と評価は上々で、すでに多くの人がその魅力に気づいている一本です。',
    ' レビューは平均{avg}点（{count}件）。数字が物語る通り、評判は決して悪くありません。',
    ' 平均{avg}点（{count}件のレビュー）という数字も、内容の裏付けになっています。',
]

_OVERVIEW_CLOSERS = [
    '気づいたら作品ページを開いている……そんな自分に気づいても、責めないであげてください。',
    '買う理由を探すより、買わない理由を探す方が難しい一作です。',
    '迷っている時間があるなら、その時間でもう読み終わっているかもしれません。',
    '一度サンプルを見てしまったら、あとは時間の問題かもしれません。',
    '気になった時点で、もう答えは出ているのではないでしょうか。',
    '迷う理由よりも、選ぶ理由の方が多い一本です。',
    '後回しにするほど、機会を逃す可能性も高くなります。',
]

_POINT_TEMPLATES_POOL = [
    '{g}好きなら、うっかり夜更かし確定の内容です',
    '{g}成分が気になる方は、もう指がカートに伸びているはず',
    '{g}のツボを心得た一作。油断してると即決してしまいます',
    '{g}好きにこっそり教えたい、隠れた掘り出し物です',
    '{g}が好きなら、まず外れることはない一本です',
    '{g}要素の描き方に、細かなこだわりが感じられます',
    '{g}を求めている方には、期待以上の内容かもしれません',
    '{g}好きの間で話題になりそうな仕上がりです',
]


def _get_article_body_template(product: dict, focus_keyphrase: str = '') -> dict:
    seed = product.get('content_id') or product.get('title') or ''
    genre_str = '、'.join(product['genres'][:5]) if product['genres'] else '不明'
    work_kind = '同人作品' if CONTENT_TYPE == 'doujin' else 'AV作品'

    if focus_keyphrase:
        opener_tmpl = _pick(seed, 'opener_kw', _KEYPHRASE_OPENERS)
        overview = opener_tmpl.format(kw=focus_keyphrase, genre_str=genre_str, work_kind=work_kind)
    else:
        opener_tmpl = _pick(seed, 'opener', _OPENERS)
        overview = opener_tmpl.format(genre_str=genre_str, work_kind=work_kind)

    blocks = []

    if product.get('actors') and CONTENT_TYPE == 'av':
        actor_str = '、'.join(product['actors'])
        blocks.append(_pick(seed, 'actor', _ACTOR_LINES).format(actor_str=actor_str))

    if product.get('maker'):
        blocks.append(_pick(seed, 'maker', _MAKER_LINES).format(maker=product['maker']))

    if product.get('price'):
        blocks.append(_pick(seed, 'price', _PRICE_LINES).format(price=product['price']))

    if product.get('genres'):
        blocks.append(_pick(seed, 'genre_appeal', _GENRE_APPEAL_LINES).format(genre_str=genre_str))

    if product.get('sample_images'):
        blocks.append(_pick(seed, 'sample', _SAMPLE_LINES))

    if product.get('review_avg') and product.get('review_count'):
        blocks.append(_pick(seed, 'review', _REVIEW_LINES).format(
            avg=product['review_avg'], count=product['review_count']
        ))

    if blocks:
        h = hashlib.md5(f'{seed}:order'.encode('utf-8')).hexdigest()
        rotate = int(h, 16) % len(blocks)
        blocks = blocks[rotate:] + blocks[:rotate]

    overview += ''.join(blocks)

    closer = _pick(seed, 'closer', _OVERVIEW_CLOSERS)
    overview += f"\n\nそのため、{closer}"

    points = []
    genres_for_points = (product.get('genres') or [])[:4]
    for i, g in enumerate(genres_for_points):
        tmpl = _pick(f'{seed}:{g}:{i}', 'point', _POINT_TEMPLATES_POOL)
        points.append(tmpl.format(g=g))

    if product.get('review_avg') and product.get('review_count'):
        points.append(f"また、レビュー平均{product['review_avg']}点（{product['review_count']}件）と、みんなも太鼓判")
    if product.get('maker'):
        points.append(f"制作は{product['maker']}。安定した仕上がりも安心材料のひとつです")
    if not points:
        points = ['作品ページを開いた時点で、もう半分ハマっています']

    if focus_keyphrase:
        keyphrase_point = f"なお、「{focus_keyphrase}」が気になった方は、ぜひ作品ページもチェックしてみてください"
        if len(points) >= 6:
            points[5] = keyphrase_point
        else:
            points.append(keyphrase_point)

    return {'overview': overview, 'points': points[:6]}


# ================================================================
# 🧱 記事HTML組み立て（元スクリプトの build_article と同一構造）
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


def _points_list_html(points: list) -> str:
    if not points:
        return ''
    items = ''.join(
        f'<li style="margin:6px 0;line-height:1.6;">{escape(pt)}</li>'
        for pt in points
    )
    return (
        '<div class="ona-points-box">'
        '<div class="ona-points-title">✓ ここがポイント</div>'
        f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
        '</div>'
    )


def _sample_gallery_html(affiliate_url: str, sample_images: list, title: str, focus_keyphrase: str = '') -> str:
    imgs = [u for u in (sample_images or []) if u][:8]
    if not imgs:
        return ''
    keyphrase_alt = f'{focus_keyphrase} {title} サンプル画像' if focus_keyphrase else f'{title} サンプル画像'
    cells = []
    for i, url in enumerate(imgs):
        alt_text = keyphrase_alt if i == 0 else f'サンプル画像{i + 1}'
        cells.append(
            f'<a href="{escape(affiliate_url)}" target="_blank" rel="nofollow" class="ona-sample-cell">'
            f'<img src="{escape(url)}" alt="{escape(alt_text)}" loading="lazy" class="ona-sample-img"></a>'
        )
    return (
        '<div class="ona-sample-gallery">'
        '<div class="ona-sample-gallery-title">作品サンプル</div>'
        '<div class="ona-sample-grid">' + ''.join(cells) + '</div>'
        '</div>'
    )


def _make_excerpt(title: str, max_len: int = 90) -> str:
    plain = re.sub(r'\s+', ' ', title or '').strip()
    if len(plain) > max_len:
        plain = plain[:max_len - 1].rstrip() + '…'
    return plain


_FOCUS_KEYPHRASE_EXCLUDE = {
    'ハイビジョン', '4K', '4k', 'ＨＤ', 'HD',
    '独占配信', '独占', '専売', '単体', '通販', 'DL版',
    '4時間以上作品', 'デジタル限定', '成人向け', '男性向け',
}


def _build_focus_keyphrase(product: dict, max_words: int = 2, max_chars: int = 12) -> str:
    words = []
    if CONTENT_TYPE == 'av' and product.get('actors'):
        for a in product['actors']:
            a = (a or '').strip()
            if a and a not in words:
                words.append(a)
            if len(words) >= max_words:
                break
    for g in (product.get('genres') or []):
        if len(words) >= max_words:
            break
        g = (g or '').strip()
        if g and len(g) > 1 and g not in words and g not in _FOCUS_KEYPHRASE_EXCLUDE:
            words.append(g)
    if not words and product.get('maker'):
        maker = product['maker'].strip()
        if maker:
            words.append(maker)
    while len(words) > 1 and len(' '.join(words)) > max_chars:
        words.pop()
    return ' '.join(words[:max_words])


def _build_seo_title(product: dict, keyphrase: str = '', max_len: int = 32) -> str:
    title = (product.get('title') or '').strip()
    if not keyphrase:
        return title[:max_len]
    remaining = max_len - len(keyphrase) - 1
    if remaining <= 0:
        return keyphrase[:max_len]
    return f'{keyphrase} {title[:remaining].rstrip()}'


def build_updated_body(product: dict) -> dict:
    """新テンプレートで、既存記事の更新用データ（本文HTML・抜粋・メタ等）を作る。
    タイトル・スラッグ・カテゴリー・タグ・アイキャッチ画像は変更しないため、ここには含めない。"""
    focus_keyphrase = _build_focus_keyphrase(product)
    body_content = _get_article_body_template(product, focus_keyphrase)
    seo_title = _build_seo_title(product, keyphrase=focus_keyphrase, max_len=20)

    _base_excerpt = _make_excerpt(product['title'], max_len=55)
    if focus_keyphrase and focus_keyphrase not in _base_excerpt:
        excerpt = _make_excerpt(f'{focus_keyphrase}｜{product["title"]}', max_len=55)
    else:
        excerpt = _base_excerpt

    overview_html = _paragraphs_to_html(body_content['overview'])
    points_html = _points_list_html(body_content['points'])
    genre_badges_html = _genre_badges_html(product.get('genres', []))
    star_html = _star_rating_html(product.get('review_avg'), product.get('review_count'))
    gallery_html = _sample_gallery_html(
        product.get('affiliate_url', ''), product.get('sample_images', []), product.get('title', ''),
        focus_keyphrase
    )

    meta_line_parts = []
    if product.get('maker'):
        meta_line_parts.append(f'サークル: {escape(product["maker"])}')
    meta_line_html = ''
    if meta_line_parts:
        meta_line_html = (
            '<div style="color:#666;font-size:13px;margin:4px 0 10px;">'
            + ' ／ '.join(meta_line_parts) + '</div>'
        )

    price_badge_html = ''
    if product.get('price'):
        price_badge_html = (
            '<div style="display:inline-block;background:#fff0f5;color:#e0507a;'
            'border:1px solid #ffc2d6;border-radius:8px;padding:6px 14px;'
            f'font-size:15px;font-weight:bold;margin:10px 0;">価格 {escape(product["price"])}</div>'
        )

    overview_section_html = (
        '<div style="margin-top:14px;">'
        f'{overview_html}</div>'
    )

    cta_html = (
        f'<div style="text-align:center;margin:20px 0 8px;">'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
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
        cat_label = '同人' if CONTENT_TYPE == 'doujin' else '動画'
        internal_link_html = (
            f'<p style="font-size:13px;margin-top:14px;">'
            f'<a href="{escape(WP_URL)}/category/{cat_label}/">'
            f'他の{cat_label}作品もチェックする →</a></p>'
        )

    card_inner = '\n'.join(
        part for part in [
            overview_section_html,
            meta_line_html,
            genre_badges_html,
            star_html,
            price_badge_html,
            points_html,
            gallery_html,
            cta_html,
            internal_link_html,
            disclaimer_html,
        ] if part
    )

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    return {
        'body':            body_html,
        'excerpt':         excerpt,
        'focus_keyphrase': focus_keyphrase,
        'seo_title':       seo_title,
    }


# ================================================================
# 🔐 WordPress REST API：投稿一覧の取得・更新
# ================================================================

def fetch_wp_posts_page(page: int, per_page: int = 50):
    endpoint = f'{WP_URL}/wp-json/wp/v2/posts'
    params = {
        'page':     page,
        'per_page': per_page,
        'status':   WP_STATUSES,
        'context':  'edit',  # 認証済みで status=draft 等も取得するために必要
        '_fields':  'id,slug,title,status,link',
    }
    resp = requests.get(endpoint, params=params, auth=_wp_auth(), headers=_JSON_HEADERS, timeout=20)
    if resp.status_code != 200:
        print(f'    ❌ 投稿一覧の取得に失敗 status={resp.status_code}: {resp.text[:300]}')
        return [], False
    try:
        posts = resp.json()
    except ValueError:
        return [], False
    total_pages_header = resp.headers.get('X-WP-TotalPages')
    has_more = True
    if total_pages_header is not None:
        try:
            has_more = page < int(total_pages_header)
        except ValueError:
            has_more = bool(posts)
    return posts, has_more


def update_wp_post(post_id: int, update_data: dict) -> bool:
    endpoint = f'{WP_URL}/wp-json/wp/v2/posts/{post_id}'
    payload = {
        'content': update_data['body'],
        'excerpt': update_data['excerpt'],
    }
    meta = {}
    if update_data.get('focus_keyphrase'):
        meta['_yoast_wpseo_focuskw'] = update_data['focus_keyphrase']
    if update_data.get('seo_title'):
        meta['_yoast_wpseo_title'] = update_data['seo_title']
    if update_data.get('excerpt'):
        meta['_yoast_wpseo_metadesc'] = update_data['excerpt']
    if meta:
        payload['meta'] = meta

    try:
        resp = requests.post(
            endpoint, data=json.dumps(payload).encode('utf-8'),
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=20,
        )
        if resp.status_code in (200, 201):
            try:
                result = resp.json()
            except ValueError:
                result = None
            if not isinstance(result, dict) or 'id' not in result:
                print(f'    ❌ 更新失敗：WordPressから投稿データが返りませんでした: {resp.text[:300]}')
                return False
            print(f'    ✅ 更新成功: post_id={post_id}')
            return True
        else:
            print(f'    ❌ 更新失敗 status={resp.status_code}: {resp.text[:300]}')
            return False
    except Exception as e:
        print(f'    ❌ 更新エラー: {e}')
        return False


# ================================================================
# 🚀 メイン実行
# ================================================================

def main():
    history = load_history()
    print(f'📚 更新済み履歴: {len(history)}件')

    updated = 0
    skipped_no_product = 0
    skipped_history = 0
    page = 1

    while updated < MAX_UPDATE:
        posts, has_more = fetch_wp_posts_page(page)
        if not posts:
            break

        for post in posts:
            if updated >= MAX_UPDATE:
                break

            post_id = post.get('id')
            slug = post.get('slug', '')
            title_rendered = (post.get('title') or {}).get('rendered', '')

            history_key = f'{slug}'
            if history_key in history:
                skipped_history += 1
                continue

            print(f'\n🔍 処理中: post_id={post_id} slug={slug} title={title_rendered[:30]}')

            # DMMのcontent_idはslugと同一（_make_slugで content_id をそのままslug化しているため）
            item = fetch_dmm_product_by_cid(slug)
            if not item:
                print('    ⚠️ DMM APIで該当商品が見つかりませんでした（配信終了・cid不一致の可能性）。スキップします。')
                skipped_no_product += 1
                continue

            product = parse_product(item)
            update_data = build_updated_body(product)

            if DRY_RUN:
                print('    📝 [DRY_RUN] 生成した本文（overview部分抜粋）:')
                preview = re.sub('<[^>]+>', '', update_data['body'])[:200]
                print(f'       {preview}...')
                print(f'       excerpt: {update_data["excerpt"]}')
            else:
                ok = update_wp_post(post_id, update_data)
                if ok:
                    updated += 1
                    history.add(history_key)
                    save_history(history)
                # サーバー負荷・レート制限対策のウェイト
                time.sleep(1.0)
                continue

            # DRY_RUNでも件数カウントし、確認したい件数で止められるようにする
            updated += 1

        if not has_more:
            break
        page += 1

    print(f'\n✅ 完了！{updated}件を{"確認（DRY_RUN）" if DRY_RUN else "更新"}しました。')
    print(f'   スキップ（履歴済み）: {skipped_history}件 / スキップ（DMM側で商品なし）: {skipped_no_product}件')
    if DRY_RUN:
        print('   ※ DRY_RUN=true のため、実際のWordPress更新は行っていません。')
        print('   ※ 内容に問題なければ、DRY_RUN=false にして再実行してください。')


if __name__ == '__main__':
    main()
