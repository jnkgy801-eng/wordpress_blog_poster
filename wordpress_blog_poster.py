# -*- coding: utf-8 -*-
"""
📝 FANZA同人 → WordPress 記事自動生成・下書き投稿ツール

DMMアフィリエイトAPI(v3)で同人作品情報を取得し、作品ごとに
記事（1作品=1記事）を生成して、WordPress REST APIへ
「下書き（draft）」として投稿します。
--------------------------------------------------------------
"""

import os
import re
import sys
import json
import datetime
import requests
from pathlib import Path
from xml.sax.saxutils import escape

from age_safety_filter import is_safe, find_matched_keywords

# ================================================================
# ⚙️ 設定（環境変数から読み込み）
# ================================================================

DMM_API_ID       = os.environ.get('DMM_API_ID', '')
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '')

WP_URL           = os.environ.get('WP_URL', '').rstrip('/')      # 例: https://example.com
WP_USERNAME      = os.environ.get('WP_USERNAME', '')             # WordPressのログインユーザー名
WP_APP_PASSWORD  = os.environ.get('WP_APP_PASSWORD', '')         # アプリケーションパスワード（通常のログインパスワードとは別物）

# 投稿ステータス。draft（下書き）/ pending（承認待ち）/ publish（本公開）から選択。
# publishを選ぶと人間の目視確認なしにそのままサイトに公開されるため、
# 記事内容・画像・年齢確認フィルターの精度に十分自信がある場合のみ使用してください。
WP_POST_STATUS   = os.environ.get('WP_POST_STATUS', 'draft').lower()

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
    sys.exit(1)

if not WP_URL or not WP_USERNAME or not WP_APP_PASSWORD:
    print('❌ 環境変数 WP_URL / WP_USERNAME / WP_APP_PASSWORD が設定されていません。')
    sys.exit(1)

if WP_POST_STATUS not in ('draft', 'pending', 'publish'):
    print(f'⚠️ WP_POST_STATUS="{WP_POST_STATUS}" は不明な値です。'
          f' draft / pending / publish のいずれかを指定してください。draft にフォールバックします。')
    WP_POST_STATUS = 'draft'

print('✅ 認証情報を読み込みました。')
if WP_POST_STATUS == 'publish':
    print('🚨 投稿ステータス: publish（本公開）が指定されています。'
          '生成された記事は目視確認なしにそのままサイトへ公開されます。')
else:
    print(f'📌 投稿ステータス: {WP_POST_STATUS}（公開は必ず手動で行ってください）')

DMM_API_BASE = 'https://api.dmm.com/affiliate/v3'

# コンテンツ種別（同人 / AV）を選択。CONTENT_TYPE環境変数で切り替え可能。
#   doujin : FANZA同人（デフォルト）
#   av     : FANZA動画（アダルトビデオ）
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
print(f'📌 コンテンツ種別: {CONTENT_LABEL}（service={SERVICE}, floor={FLOOR}）')

DMM_SORT_MODE = os.environ.get('DMM_SORT_MODE', 'rank').lower()
SORT_TARGETS = {'date': '-date', 'rank': '-rank'}
SORT_KEY = SORT_TARGETS.get(DMM_SORT_MODE, '-rank')

# 価格フィルタ（円）。未設定なら制限なし。price_numが取得できない商品は対象外にはしない。
def _parse_price_env(name: str):
    raw = os.environ.get(name, '').strip()
    if raw.isdigit():
        return int(raw)
    return None

PRICE_MIN = _parse_price_env('PRICE_MIN')
PRICE_MAX = _parse_price_env('PRICE_MAX')
if PRICE_MIN is not None or PRICE_MAX is not None:
    print(f'📌 価格フィルタ: {PRICE_MIN if PRICE_MIN is not None else "指定なし"}円 〜 '
          f'{PRICE_MAX if PRICE_MAX is not None else "指定なし"}円')

# AVでVRコンテンツを除外するかどうか。デフォルトは除外（True）。
# EXCLUDE_VR=false を指定すれば無効化できる（doujinの場合は元々関係なし）。
EXCLUDE_VR = os.environ.get('EXCLUDE_VR', 'true').strip().lower() not in ('false', '0', 'no')
if CONTENT_TYPE == 'av':
    print(f'📌 VR作品の除外: {"する" if EXCLUDE_VR else "しない"}')


def _is_vr_product(product: dict) -> bool:
    """ジャンル名・タイトルにVRを示すキーワードが含まれる作品かどうかを判定する。"""
    vr_keywords = ('VR', 'ＶＲ')
    for genre in product.get('genres', []) or []:
        if any(kw in genre for kw in vr_keywords):
            return True
    title = product.get('title', '') or ''
    if any(kw in title for kw in vr_keywords):
        return True
    return False


_raw_start = os.environ.get('POST_START_INDEX', '')
if _raw_start.strip().isdigit():
    START_OFFSET = int(_raw_start.strip())
else:
    START_OFFSET = 1
print(f'📌 取得開始番号: {START_OFFSET}（{SORT_KEY}順）')

FETCH_COUNT = int(os.environ.get('FETCH_COUNT', '100'))
MAX_ARTICLES = int(os.environ.get('MAX_ARTICLES', '5'))  # 1回の実行で投稿する記事数の上限
MAX_FETCH_PAGES = int(os.environ.get('MAX_FETCH_PAGES', '10'))

# 投稿済み作品の重複防止用履歴ファイル
POSTED_HISTORY_FILE = Path(os.environ.get('POSTED_HISTORY_FILE', 'outputs/posted_history.json'))

# ================================================================
# 🗂️ 投稿履歴管理（重複投稿防止）
# ================================================================

def load_posted_history() -> set:
    if not POSTED_HISTORY_FILE.exists():
        return set()
    try:
        with open(POSTED_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('posted', []))
    except Exception as e:
        print(f'⚠️ 投稿履歴の読み込みに失敗しました（新規履歴として扱います）: {e}')
        return set()


def save_posted_history(history: set) -> None:
    POSTED_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(POSTED_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({'posted': sorted(history)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ 投稿履歴の保存に失敗しました: {e}')


def product_history_key(product: dict) -> str:
    return product.get('content_id') or f"title:{product.get('title', '')}"


# ================================================================
# 🔧 DMM API 関連（既存スクリプトと同じロジックを踏襲）
# ================================================================

def fetch_dmm_products(offset: int):
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
        'hits':         FETCH_COUNT,
        'offset':       offset,
        'sort':         SORT_KEY,
        'output':       'json',
    }
    try:
        resp = requests.get(f'{DMM_API_BASE}/ItemList', params=params, timeout=15)
        data = resp.json()
        items = data.get('result', {}).get('items', [])
        if isinstance(items, dict):
            items = items.get('item', [])
        print(f'✅ DMM APIから {len(items)} 件取得しました（offset={offset}）。')
        return items
    except Exception as e:
        print(f'❌ DMM APIエラー（offset={offset}）: {e}')
        return []


_TITLE_PREFIX_RE = re.compile(r'^【[^】]{1,20}】\s*')


def _strip_redundant_title_prefix(title: str) -> str:
    """DMMの商品タイトルは【ハイビジョン・独占配信・巨乳】のような接頭辞の直後に、
    同じキーワードを読点区切りでそのまま繰り返す形式が多い。
    一覧表示で全作品のタイトルが同じ書き出しに見えてしまう原因になるため、
    この冒頭の【...】タグ部分だけを取り除く（ジャンル情報はバッジ/カテゴリーで別途表示されるため
    情報は失われない）。"""
    if not title:
        return title
    return _TITLE_PREFIX_RE.sub('', title, count=1).strip()


def parse_product(item):
    content_id    = item.get('content_id', '') or item.get('product_id', '')
    title         = _strip_redundant_title_prefix(item.get('title', ''))
    affiliate_url = item.get('affiliateURL', '') or item.get('URL', '')
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

    review_info = item.get('review', {}) or {}
    try:
        review_avg   = float(review_info.get('average', 0) or 0)
        review_count = int(review_info.get('count', 0) or 0)
    except (ValueError, TypeError):
        review_avg, review_count = 0.0, 0
    review_avg   = round(review_avg, 2) if review_avg else None
    review_count = review_count if review_count else None

    package_image = ''
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
        'review_avg':    review_avg,
        'review_count':  review_count,
        'package_image': package_image,
        'sample_images': sample_images,
    }


# ================================================================
# 📝 記事本文生成（元スクリプトと同じロジック）
# ================================================================

def get_article_body_ai(product: dict) -> dict:
    if ANTHROPIC_API_KEY:
        try:
            return _get_article_body_from_api(product)
        except Exception as e:
            print(f'    ⚠️ AI記事生成エラー（テンプレート使用）: {e}')
    return _get_article_body_template(product)


def _get_article_body_from_api(product: dict) -> dict:
    genre_str = '・'.join(product['genres'][:5]) if product['genres'] else '不明'
    review_str = (
        f"平均{product['review_avg']}点（{product['review_count']}件のレビュー）"
        if product.get('review_avg') and product.get('review_count')
        else '不明'
    )
    prompt = (
        f"{CONTENT_LABEL}（成人向け）作品を紹介するブログ記事の本文材料を作成してください。\n"
        "読み手がクスッと笑いながら読み進め、最後には『これは買うしかない』と\n"
        "思ってしまうような、ユーモアたっぷりで購買意欲を刺激する文章を書いてください。\n\n"
        f"作品名: {product['title']}\n"
        f"ジャンル: {genre_str}\n"
        f"{'サークル' if CONTENT_TYPE == 'doujin' else 'メーカー/レーベル'}: {product.get('maker') or '不明'}\n"
        f"価格: {product.get('price') or '不明'}\n"
        f"レビュー: {review_str}\n\n"
        "条件:\n"
        "- 文体はユーモラスで軽快に。読者にニヤッとしてもらえるような比喩・ツッコミ・\n"
        "  軽い自虐やボケを交えてよい（下品・侮辱的にはしない）\n"
        "- 『買わない理由が見当たらない』『気づいたらカートに入れている』のような、\n"
        "  読者の背中を押す一言をOVERVIEWの締めに入れる\n"
        "- ただし『業界No.1』『絶対』『必ず満足』など、検証不可能な断定・優良誤認の\n"
        "  おそれがある表現（景品表示法に抵触しうる表現）は使わない\n"
        "- 未成年を想起させる表現は一切使わない（成人向け作品であることを前提にする）\n"
        "- OVERVIEWは作品タイトルを本文中で繰り返さない（タイトルは見出しに既に表示されている）。\n"
        "  代わりに、ジャンルから読み取れる「どんな魅力があるか」「どういうシチュエーション・\n"
        "  関係性の話か」を、ユーモアを交えつつ自然な文章として書く\n"
        "- POINTSに価格やレビュー点数など数値情報は含めない（別欄に表示済みのため）\n"
        "- POINTSは『○○要素が中心のストーリー』のような機械的な言い回しを使わない。\n"
        "  各ジャンル・特徴が読者にとってどう楽しめる要素なのかを、ユーモアを効かせつつ\n"
        "  項目ごとに違う言い回しで、思わず読みたくなる一文にする\n"
        "- 出力は必ず次のプレーンテキスト形式のみ。前置きや説明・Markdown記法は禁止。\n\n"
        "===OVERVIEW===\n"
        "(150〜250文字程度で作品の魅力・シチュエーションを1〜2段落、ユーモアを交えて。\n"
        "段落間は空行で区切る)\n"
        "===POINTS===\n"
        "(「ここがポイント」として3〜4個、1行1項目、先頭に「- 」を付ける)\n"
    )
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 600,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=15,
    )
    data = resp.json()
    text = data.get('content', [{}])[0].get('text', '').strip()
    if not text or '===OVERVIEW===' not in text or '===POINTS===' not in text:
        raise ValueError('unexpected AI response format')

    overview_part = text.split('===OVERVIEW===', 1)[1].split('===POINTS===', 1)[0].strip()
    points_part = text.split('===POINTS===', 1)[1].strip()
    points = [
        line.strip().lstrip('-').strip()
        for line in points_part.splitlines()
        if line.strip().lstrip('-').strip()
    ]
    if not overview_part or not points:
        raise ValueError('empty overview or points')
    return {'overview': overview_part, 'points': points[:4]}


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


def _get_article_body_template(product: dict) -> dict:
    genre_str = '、'.join(product['genres'][:5]) if product['genres'] else '不明'
    work_kind = '同人作品' if CONTENT_TYPE == 'doujin' else 'AV作品'
    overview = f"{genre_str}系の{work_kind}です。"
    if product.get('maker'):
        overview += f" 手がけるのは{product['maker']}。"
    closer = _OVERVIEW_CLOSERS_PICK(product)
    overview += f"\n\n{closer}"

    points = []
    for i, g in enumerate((product.get('genres') or [])[:3]):
        tmpl = _GENRE_POINT_TEMPLATES[i % len(_GENRE_POINT_TEMPLATES)]
        points.append(tmpl.format(g=g))
    if product.get('review_avg') and product.get('review_count'):
        points.append(f"レビュー平均{product['review_avg']}点（{product['review_count']}件）と、みんなも太鼓判")
    if not points:
        points = ['作品ページを開いた時点で、もう半分ハマっています']

    return {'overview': overview, 'points': points[:4]}


def _OVERVIEW_CLOSERS_PICK(product: dict) -> str:
    key = product.get('content_id') or product.get('title') or ''
    idx = sum(ord(c) for c in key) % len(_OVERVIEW_CLOSERS) if key else 0
    return _OVERVIEW_CLOSERS[idx]


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


def _sample_gallery_html(affiliate_url: str, sample_images: list, title: str) -> str:
    imgs = [u for u in (sample_images or []) if u][:8]
    if not imgs:
        return ''
    cells = []
    for url in imgs:
        cells.append(
            f'<a href="{escape(affiliate_url)}" target="_blank" rel="nofollow" class="ona-sample-cell">'
            f'<img src="{escape(url)}" alt="{escape(title)} サンプル画像" loading="lazy" class="ona-sample-img"></a>'
        )
    return (
        '<div class="ona-sample-gallery">'
        '<div class="ona-sample-gallery-title">作品サンプル</div>'
        '<div class="ona-sample-grid">' + ''.join(cells) + '</div>'
        '</div>'
    )


def _make_slug(content_id: str, title: str) -> str:
    """パーマリンクを英数字のみの短いスラッグにする。
    日本語タイトルがそのままURLエンコードされて長く読みにくくなるのを防ぐため、
    content_id（DMM側の商品IDで元々英数字）を優先的に使う。"""
    base = (content_id or '').strip()
    base = re.sub(r'[^A-Za-z0-9\-]+', '-', base).strip('-').lower()
    if base:
        return base
    # content_idが取得できない場合のフォールバック（タイトルのハッシュ的な短縮）
    fallback = re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-').lower()
    return fallback[:60] or 'item'


def _make_excerpt(title: str, max_len: int = 90) -> str:
    """アーカイブページ等で画像の下に表示される抜粋文（プレーンテキスト、HTMLタグなし）。
    以前はジャンル接頭辞＋概要文を独自に組み立てていたが、
    それぞれが同じジャンル名を繰り返してしまい一覧が全部同じ書き出しに見える原因になっていたため、
    シンプルに作品タイトルをそのまま抜粋として使う。"""
    plain = re.sub(r'\s+', ' ', title or '').strip()
    if len(plain) > max_len:
        plain = plain[:max_len - 1].rstrip() + '…'
    return plain


def build_article(product: dict) -> dict:
    body_content = get_article_body_ai(product)
    excerpt = _make_excerpt(product['title'])
    overview_html = _paragraphs_to_html(body_content['overview'])
    points_html = _points_list_html(body_content['points'])
    genre_badges_html = _genre_badges_html(product.get('genres', []))
    star_html = _star_rating_html(product.get('review_avg'), product.get('review_count'))
    gallery_html = _sample_gallery_html(
        product.get('affiliate_url', ''), product.get('sample_images', []), product.get('title', '')
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
        '<div style="font-weight:bold;color:#555;margin-bottom:6px;font-size:14px;">作品概要</div>'
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

    card_inner = '\n'.join(
        part for part in [
            f'<h3 style="margin:0 0 6px;font-size:20px;line-height:1.4;">{escape(product["title"])}</h3>',
            meta_line_html,
            genre_badges_html,
            star_html,
            price_badge_html,
            overview_section_html,
            points_html,
            gallery_html,
            cta_html,
            disclaimer_html,
        ] if part
    )

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    genre_categories = product['genres'][:5] if product['genres'] else []

    return {
        'title':             product['title'],
        'slug':              _make_slug(product.get('content_id', ''), product['title']),
        'excerpt':           excerpt,
        'body':              body_html,
        'categories':        genre_categories + [CONTENT_LABEL, 'PR'],
        'genre_categories':  genre_categories,
        'featured_image_url': product.get('package_image', ''),
        'content_id':        product.get('content_id', ''),
    }


# ================================================================
# 🔐 WordPress REST API 投稿（アプリケーションパスワード認証・draft固定）
# ================================================================

_category_cache = {}   # name -> id
_tag_cache = {}        # name -> id


def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
    'User-Agent': 'wordpress-blog-poster/1.0 (+https://otona-navi example)',
}


def _get_or_create_term(taxonomy: str, name: str, cache: dict):
    """WordPressのカテゴリー/タグを名前で検索し、無ければ作成してIDを返す。"""
    if not name:
        return None
    if name in cache:
        return cache[name]

    endpoint = f'{WP_URL}/wp-json/wp/v2/{taxonomy}'
    try:
        # まず既存を検索
        resp = requests.get(
            endpoint, params={'search': name, 'per_page': 100},
            auth=_wp_auth(), headers=_JSON_HEADERS, timeout=15,
        )
        if resp.status_code == 200:
            try:
                results = resp.json()
            except ValueError:
                results = None
            # 想定外のレスポンス形状（文字列やdictなど）はスキップして作成処理へ進む。
            # search語に完全一致するタームだけを既存として扱う（前方一致等の誤爆を避けるため）。
            if isinstance(results, list):
                for term in results:
                    if isinstance(term, dict) and term.get('name') == name:
                        cache[name] = term['id']
                        return term['id']

        # 無ければ新規作成
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

        # WordPressは同名タームが既に存在する場合、
        # status 400 + code:"term_exists" + data.term_id を返す仕様。
        # 検索でヒットしなかった（例: 全角/半角違いなど）場合はここで既存IDを拾う。
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
        # HTTPヘッダーはASCII(latin-1)のみ許容されるため、日本語タイトルではなく
        # content_id（英数字）ベースのファイル名にする
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

    # カテゴリーは種別ラベル（FANZA同人 / FANZA動画）に加えて、上位ジャンル（最大2件）もカテゴリーとして
    # 登録する（ジャンル別に記事一覧を辿れるようにし、サイト内回遊性を上げるため）。
    # ジャンル名は従来どおりタグにも登録し、細かい検索性も維持する。
    category_ids = []
    base_category_id = _get_or_create_term('categories', CONTENT_LABEL, _category_cache)
    if base_category_id:
        category_ids.append(base_category_id)
    for genre_name in article.get('genre_categories', [])[:2]:
        gid = _get_or_create_term('categories', genre_name, _category_cache)
        if gid and gid not in category_ids:
            category_ids.append(gid)

    tag_ids = []
    for genre_name in article['categories']:
        if genre_name in (CONTENT_LABEL, 'PR'):
            continue
        tid = _get_or_create_term('tags', genre_name, _tag_cache)
        if tid:
            tag_ids.append(tid)
    pr_tag_id = _get_or_create_term('tags', 'PR', _tag_cache)
    if pr_tag_id:
        tag_ids.append(pr_tag_id)

    payload = {
        'title':      article['title'],
        'slug':       article.get('slug') or '',
        'excerpt':    article.get('excerpt') or '',
        'content':    article['body'],
        'status':     WP_POST_STATUS,   # 'draft' / 'pending' / 'publish'（WP_POST_STATUSの設定に従う）
        'categories': category_ids,
        'tags':       tag_ids,
    }

    # アイキャッチ画像（featured_media）を設定する。
    # 本文側からは同じ画像を削除したので、重複表示にはならない。
    media_id = _upload_featured_image(article.get('featured_image_url', ''), article.get('content_id', ''))
    if media_id:
        payload['featured_media'] = media_id

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
            # レンタルサーバーのbot対策（Imunify360等）にブロックされた場合、
            # HTTPステータスは200/201でも本文が {"message": "..."} のような
            # エラーメッセージだけのことがある。実際に投稿されたことを保証するため、
            # 本物のWordPress投稿レスポンス（'id'キーを持つdict）かどうかを確認する。
            if not isinstance(result, dict) or 'id' not in result:
                print(f"    ❌ 投稿失敗：WordPressから投稿データが返りませんでした"
                      f"（サーバー側のbot対策等でブロックされた可能性があります）: {resp.text[:300]}")
                return False
            actual_status = result.get('status')
            # WordPressから返ってきたステータスが、こちらが指定したWP_POST_STATUSと
            # 一致しているかどうかだけを確認する（想定外の値が返った場合のみ警告）。
            if actual_status != WP_POST_STATUS:
                print(f"    ⚠️ 指定したステータス（{WP_POST_STATUS}）と異なる値が返りました"
                      f"（status={actual_status}）。念のため内容をご確認ください: {result.get('link', '')}")
            print(f"    ✅ {actual_status}として投稿成功: {article['title'][:40]}")
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
    posted_history = load_posted_history()
    print(f'📚 投稿済み履歴: {len(posted_history)}件')

    safe_products = []
    seen_in_run = set()
    all_skipped = []
    offset = START_OFFSET

    for page in range(1, MAX_FETCH_PAGES + 1):
        if len(safe_products) >= MAX_ARTICLES:
            break

        print(f'\n🔎 [{page}/{MAX_FETCH_PAGES}ページ目] {SORT_KEY}順で取得中 '
              f'(offset={offset}, hits={FETCH_COUNT})...')
        raw_items = fetch_dmm_products(offset)
        if not raw_items:
            print('  ⚠️ これ以上取得できませんでした。検索を打ち切ります。')
            break

        for item in raw_items:
            if len(safe_products) >= MAX_ARTICLES:
                break

            product = parse_product(item)
            key = product_history_key(product)

            if key in posted_history or key in seen_in_run:
                continue

            ok, matched = is_safe(product)
            if not ok:
                all_skipped.append((product, matched))
                continue

            price_num = product.get('price_num')
            if PRICE_MIN is not None and (price_num is None or price_num < PRICE_MIN):
                continue
            if PRICE_MAX is not None and (price_num is None or price_num > PRICE_MAX):
                continue

            if CONTENT_TYPE == 'av' and EXCLUDE_VR and _is_vr_product(product):
                continue

            seen_in_run.add(key)
            safe_products.append(product)

        offset += FETCH_COUNT

    print(f'\n📊 検索結果: {len(safe_products)}/{MAX_ARTICLES}件 集まりました '
          f'（安全フィルター除外 {len(all_skipped)}件）')

    if all_skipped:
        Path('outputs').mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        skip_path = Path('outputs') / f'skipped_{ts}.txt'
        with open(skip_path, 'w', encoding='utf-8') as f:
            f.write('# 年少者連想ワードにより除外された作品一覧\n\n')
            for p, matched in all_skipped:
                f.write(f"- {p['title']}\n  マッチ語: {', '.join(matched)}\n")
        print(f'📄 除外ログ: {skip_path}')

    if not safe_products:
        print('⚠️ 投稿対象の作品がありませんでした（フィルター/重複除外で全件除外、または取得0件）。')
        sys.exit(0)

    if len(safe_products) < MAX_ARTICLES:
        print(f'⚠️ {MAX_FETCH_PAGES}ページ検索しましたが{MAX_ARTICLES}件に届きませんでした。'
              f'集まった{len(safe_products)}件のみ投稿します。')

    posted = 0
    for p in safe_products:
        print(f"\n📝 記事生成中: {p['title'][:40]}")
        article = build_article(p)
        if post_draft_to_wordpress(article):
            posted += 1
            posted_history.add(product_history_key(p))
            save_posted_history(posted_history)

    print(f'\n✅ 完了！{posted}/{len(safe_products)} 件をWordPressに{WP_POST_STATUS}として投稿しました。')
    print(f'   📚 累計投稿履歴: {len(posted_history)}件（{POSTED_HISTORY_FILE}）')
    print('   ※ 公開前に必ず内容をご確認ください。')


if __name__ == '__main__':
    main()
