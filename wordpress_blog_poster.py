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
import urllib.parse

# ================================================================
# 📌 スクリプトバージョン（デプロイ確認用）
#    「投稿によって結果が違う」といった不整合が起きた際、
#    どのバージョンのコードで生成された投稿かを確認できるようにする。
#    コードを修正するたびに、この日付/番号を更新すること。
# ================================================================
SCRIPT_VERSION = '2026-08-02-01'
import json
import datetime
JST = datetime.timezone(datetime.timedelta(hours=9))
import requests
from pathlib import Path
from xml.sax.saxutils import escape

from age_safety_filter import is_safe

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

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL   = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash')

# SEOタイトル／メタディスクリプションの冒頭にフォーカスキーフレーズ（出演者名・
# ジャンル名等）を挿入するかどうか。true（デフォルト）なら従来どおり挿入する。
# false にすると、SEOタイトルは作品タイトルそのまま、メタディスクリプションは
# 作品の説明文（AI生成OVERVIEWの冒頭）のみになり、キーフレーズは一切含まれない。
SEO_INCLUDE_KEYPHRASE = os.environ.get('SEO_INCLUDE_KEYPHRASE', 'true').strip().lower() not in ('false', '0', 'no')

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
print(f'🏷️ スクリプトバージョン: {SCRIPT_VERSION}')
print(f'📌 コンテンツ種別: {CONTENT_LABEL}（service={SERVICE}, floor={FLOOR}）')

RANK_FETCH_LIMIT = int(os.environ.get('RANK_FETCH_LIMIT', '500'))
DATE_WINDOW_DAYS = int(os.environ.get('DATE_WINDOW_DAYS', '14'))

# DMM APIへ渡すsortパラメータ。rank（人気順）/ date（新着順）から選択。
DMM_SORT_MODE = os.environ.get('DMM_SORT_MODE', 'rank').strip().lower()
if DMM_SORT_MODE not in ('rank', 'date'):
    print(f'⚠️ DMM_SORT_MODE="{DMM_SORT_MODE}" は不明な値です。rank にフォールバックします。')
    DMM_SORT_MODE = 'rank'
_SORT_LABEL = {'rank': '人気順', 'date': '新着順'}[DMM_SORT_MODE]

print(f'📌 {_SORT_LABEL}（sort={DMM_SORT_MODE}）で上位{RANK_FETCH_LIMIT}件を取得し、'
      f'発売日/配信日が実行日時より過去（今日から過去{DATE_WINDOW_DAYS}日以内）の作品のみ投稿対象にします。')

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


FETCH_COUNT = int(os.environ.get('FETCH_COUNT', '100'))
MAX_ARTICLES = int(os.environ.get('MAX_ARTICLES', '5'))  # 1回の実行で投稿する記事数の上限

# 投稿済み作品の重複防止用履歴ファイル
POSTED_HISTORY_FILE = Path(os.environ.get('POSTED_HISTORY_FILE', 'outputs/posted_history.json'))

# ================================================================
# 🗂️ 投稿履歴管理（重複投稿防止）
# ================================================================

def load_posted_history() -> dict:
    """投稿済みID集合（'posted'）に加えて、直近記事の書き出し冒頭リスト
    （'recent_openings'）も一緒に読み込む。後者はAIへの自己模倣防止プロンプトに使う。"""
    if not POSTED_HISTORY_FILE.exists():
        return {'posted': set(), 'recent_openings': []}
    try:
        with open(POSTED_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            'posted': set(data.get('posted', [])),
            'recent_openings': list(data.get('recent_openings', [])),
        }
    except Exception as e:
        print(f'⚠️ 投稿履歴の読み込みに失敗しました（新規履歴として扱います）: {e}')
        return {'posted': set(), 'recent_openings': []}


def save_posted_history(posted: set, recent_openings: list = None) -> None:
    POSTED_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(POSTED_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(
                {
                    'posted': sorted(posted),
                    'recent_openings': (recent_openings or [])[-_RECENT_OPENINGS_KEEP:],
                },
                f, ensure_ascii=False, indent=2,
            )
    except Exception as e:
        print(f'⚠️ 投稿履歴の保存に失敗しました: {e}')


def product_history_key(product: dict) -> str:
    return product.get('content_id') or f"title:{product.get('title', '')}"


# 直近何件分の「書き出し冒頭」を履歴として保持し、AIへの
# 自己模倣防止プロンプトに使うか。多すぎるとプロンプトが長くなるため8件程度に留める。
_RECENT_OPENINGS_KEEP = 8


def _stable_variant_index(key: str, salt: str, mod: int) -> int:
    """content_id等のキーと軸ごとのsaltから、実行のたびに変わらない安定した
    バリエーション番号（0〜mod-1）を算出する。
    同じ作品は常に同じ番号になるが、salt（'structure'/'heading'/'appeal'等）を
    変えることで、構成順・見出し・訴求パターンといった各軸を互いに独立して
    ばらつかせることができる（全軸が常に同じ組み合わせになるのを防ぐ）。"""
    if not key or mod <= 1:
        return 0
    combined = f'{key}::{salt}'
    return sum(ord(c) for c in combined) % mod


# ================================================================
# 🔧 DMM API 関連（既存スクリプトと同じロジックを踏襲）
# ================================================================

def fetch_dmm_products(offset: int, sort: str = None, hits: int = None,
                        gte_date: str = None, lte_date: str = None):
    params = {
        'api_id':       DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site':         'FANZA',
        'service':      SERVICE,
        'floor':        FLOOR,
        'hits':         hits or FETCH_COUNT,
        'offset':       offset,
        'sort':         sort or 'rank',
        'output':       'json',
    }
    # 発売日/配信日の範囲をDMM API側で絞り込む（未来の予約作品を最初から除外するため）。
    # 形式は 'YYYY-MM-DDTHH:MM:SS'。
    if gte_date:
        params['gte_date'] = gte_date
    if lte_date:
        params['lte_date'] = lte_date
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


def fetch_rank_items(limit: int, sort: str = None,
                      gte_date: str = None, lte_date: str = None) -> list:
    """指定sort順（rank=人気順 / date=新着順）・指定日付範囲内の上位limit件の
    生アイテム（dict）をリストで返す。"""
    sort = sort or DMM_SORT_MODE
    items_out = []
    offset = 1
    while len(items_out) < limit:
        items = fetch_dmm_products(offset, sort=sort, gte_date=gte_date, lte_date=lte_date)
        if not items:
            break
        items_out.extend(items)
        offset += FETCH_COUNT
    return items_out[:limit]


def _strip_redundant_title_prefix(title: str) -> str:
    """DMMの商品タイトルは【ハイビジョン・独占配信・巨乳】のような接頭辞の直後に、
    同じキーワードを読点区切りでそのまま繰り返す形式が多い。
    一覧表示で全作品のタイトルが同じ書き出しに見えてしまう原因になるため、
    この冒頭の【...】タグ部分だけを取り除く（ジャンル情報はバッジ/カテゴリーで別途表示されるため
    情報は失われない）。"""
    if not title:
        return title
    return _TITLE_PREFIX_RE.sub('', title, count=1).strip()


def _build_affiliate_url(raw_url: str) -> str:
    """DMM APIのレスポンスに 'affiliateURL' が含まれない商品（同人カテゴリ等で
    まれに発生）向けに、計測付きの正しいアフィリエイトリンク（al.dmm.co.jp形式）を
    自前で組み立てる。
    ここを経由しないと、'URL' フィールド（utm_*パラメータ付きだが
    アフィリエイト計測は乗っていない生リンク）がそのまま使われてしまい、
    クリックされても報酬が一切発生しない、という不具合になる。
    """
    if not raw_url:
        return ''
    # utm_* パラメータ（アフィリエイト計測とは無関係）は取り除いてからlurlに使う
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
    """DMM APIが返す 'affiliateURL' を、そのまま信用せずに検証する。

    同人カテゴリ等: 'affiliateURL' が空で返ってくることがある。
    AV(video.dmm.co.jp)等: 'affiliateURL' が空ではないが、中身が
        utm_*パラメータだけの計測なしURL（al.dmm.co.jpを経由していない）
        のまま返ってくることがある。
    どちらの場合も、クリックされてもアフィリエイト報酬が発生しない生リンクに
    なってしまうため、'al.dmm.co.jp' を経由した正しい形式でなければ、
    自前で組み立て直す。
    """
    raw_affiliate = item.get('affiliateURL', '')
    # DMM APIは商品によって、正しい計測付きリンクを 'al.dmm.co.jp' ではなく
    # 'al.fanza.co.jp' というドメインで返してくることがある。
    # ここを見落とすと、すでに正しいリンクをもう一度 al.dmm.co.jp で
    # 二重に包んでしまい（lurlの中にlurlが入れ子になる）、404の原因になる。
    if raw_affiliate and ('al.dmm.co.jp' in raw_affiliate or 'al.fanza.co.jp' in raw_affiliate):
        return raw_affiliate
    # affiliateURLが空、または計測なしの生リンクだった場合は、
    # 手元にあるURL（affiliateURLかURLフィールド）から正しい形式を組み立て直す
    fallback_source = raw_affiliate or item.get('URL', '')
    return _build_affiliate_url(fallback_source)


def parse_product(item):
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
    # ジャンルは[:5]で切り捨てず全件保持する（プロンプト側で作品の差別化に使うため）
    genres   = [g.get('name', '') for g in (iteminfo.get('genre') or [])]
    maker    = ((iteminfo.get('maker') or [{}])[0]).get('name', '')
    actors   = [a.get('name', '') for a in (iteminfo.get('actress') or []) if a.get('name')][:3]

    # 作品固有の差別化情報（シリーズ・レーベル・監督）。
    # 存在しない作品も多いため、無ければ空文字にフォールバックする。
    series   = ((iteminfo.get('series') or [{}])[0]).get('name', '')
    label    = ((iteminfo.get('label') or [{}])[0]).get('name', '')
    director = ((iteminfo.get('director') or [{}])[0]).get('name', '')

    # サンプル動画URL（記事に埋め込み用）。取得できるサイズを優先度順に探す。
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
        'review_avg':    review_avg,
        'review_count':  review_count,
        'package_image': package_image,
        'sample_images': sample_images,
        'sample_movie_url': sample_movie_url,
        'series':        series,
        'label':         label,
        'director':      director,
        'date':          item.get('date', ''),
    }


# ================================================================
# 📝 記事本文生成（元スクリプトと同じロジック）
# ================================================================

def get_article_body_ai(product: dict, focus_keyphrase: str = '', appeal_pattern: dict = None,
                         length_variant: dict = None, recent_openings: list = None) -> dict:
    if GEMINI_API_KEY:
        try:
            return _get_article_body_from_api(
                product, focus_keyphrase,
                appeal_pattern=appeal_pattern, length_variant=length_variant,
                recent_openings=recent_openings,
            )
        except Exception as e:
            print(f'    ⚠️ AI記事生成エラー（テンプレート使用）: {e}')
    else:
        # GEMINI_API_KEY未設定に気づかないままテンプレート運用が続く事故を防ぐため、
        # 実行のたびに明示的に警告を出す。
        print('    ⚠️ GEMINI_API_KEY未設定のため、テンプレート文章を使用します。')
    return _get_article_body_template(product, focus_keyphrase, appeal_pattern=appeal_pattern)


def _get_article_body_from_api(product: dict, focus_keyphrase: str = '', appeal_pattern: dict = None,
                                length_variant: dict = None, recent_openings: list = None) -> dict:
    appeal_pattern = appeal_pattern or _APPEAL_PATTERNS[0]
    length_variant = length_variant or _LENGTH_VARIANTS[1]
    # ジャンルは全件使う（切り捨てない）。作品ごとの差別化に使う情報のため。
    genre_str = '・'.join(product['genres']) if product['genres'] else '不明'
    review_str = (
        f"平均{product['review_avg']}点（{product['review_count']}件のレビュー）"
        if product.get('review_avg') and product.get('review_count')
        else '不明'
    )

    # 作品固有の差別化情報（シリーズ・レーベル・監督）。無ければその旨を明記し、
    # AIが実在しない情報を捏造しないようにする。
    unique_info_lines = []
    if product.get('series'):
        unique_info_lines.append(f"シリーズ: {product['series']}")
    if product.get('label'):
        unique_info_lines.append(f"レーベル: {product['label']}")
    if product.get('director'):
        unique_info_lines.append(f"監督: {product['director']}")
    unique_info_str = '\n'.join(unique_info_lines) if unique_info_lines else '（シリーズ・レーベル情報なし）'

    keyphrase_instruction = ''
    if focus_keyphrase:
        keyphrase_instruction = (
            f"- 「{focus_keyphrase}」というキーフレーズを、OVERVIEWの最初の一文に必ず含め、\n"
            "  かつ本文（OVERVIEW+POINTS）全体でもう1回以上、自然な形で登場させる\n"
        )

    # 訴求パターン（利用シーン/ジャンル比較/作者・レーベル/読者タイプ）を作品ごとに
    # 固定でローテーションさせ、記事間で切り口が単調に揃わないようにする。
    appeal_instruction = appeal_pattern['instruction']

    # 直近の記事の書き出しパターンをAIに伝え、同じ言い回し・構文の繰り返しを避けさせる
    # （内容の使い回し防止ではなく、あくまで「書き出しの型」の重複防止が目的）。
    avoid_repetition_instruction = ''
    if recent_openings:
        sample = recent_openings[-5:]
        bullet_lines = '\n'.join(f'  - 「{op}…」' for op in sample)
        avoid_repetition_instruction = (
            "- 直近の記事は次のような書き出しで始まっています。同じ言い回し・"
            "同じ文構造の書き出しは避けてください（内容自体の使い回し防止ではなく、"
            "書き出しパターンの単調な重複を避ける目的です）:\n"
            f"{bullet_lines}\n"
        )

    prompt = (
        f"あなたは{CONTENT_LABEL}（成人向け）ジャンルを専門とする、読者の意思決定を助ける\n"
        "レビューライターです。以下の作品について、読者が『自分に合う作品かどうか』を\n"
        "具体的に判断できるような紹介記事の本文材料を作成してください。\n"
        "ゴールは読者を煽って購入させることではなく、正直で具体的な情報を提供し、\n"
        "結果として『自分に合っている』と納得した読者が自然に購入を検討できる状態を\n"
        "作ることです。\n\n"
        f"作品名: {product['title']}\n"
        f"ジャンル: {genre_str}\n"
        f"{'サークル' if CONTENT_TYPE == 'doujin' else 'メーカー/レーベル'}: {product.get('maker') or '不明'}\n"
        f"{unique_info_str}\n"
        f"価格: {product.get('price') or '不明'}\n"
        f"レビュー: {review_str}\n\n"
        "本文の書き方:\n"
        "1. この作品ならではの特徴（シリーズものならその中での位置づけ・過去作との違い、\n"
        "   レーベル/監督が分かる場合はその作風の傾向）を、憶測ではなく分かる範囲で明記する。\n"
        "   情報がない場合は無理に触れなくてよい（存在しない情報を作らない）\n"
        "2. ジャンルの組み合わせから、どんな状況・関係性を描いた作品かを具体的に描写する\n"
        "3. 『こんな人には特に向いている』『逆にこういう好みの人には物足りないかもしれない』\n"
        "   という判断材料を両方入れる（誇大な断定は避ける）\n"
        "4. 価格情報の円額そのものはOVERVIEW本文では言及しない（別欄表示のため）\n\n"
        "条件:\n"
        f"{keyphrase_instruction}"
        f"{appeal_instruction}"
        f"{avoid_repetition_instruction}"
        "- 文体は親しみやすく具体的に。読者の判断を助けることを最優先する\n"
        "- 抽象的な誉め言葉（『魅力的』『必見』等）だけで終わらせず、具体的な描写を入れる\n"
        "- 『業界No.1』『絶対』『必ず満足』『今だけ』『期間限定』など、検証不可能な\n"
        "  断定・優良誤認・実際にはない限定性を匂わせる表現（景品表示法に抵触しうる\n"
        "  表現）は使わない\n"
        "- 未成年を想起させる表現は一切使わない（成人向け作品であることを前提にする）\n"
        "- OVERVIEWは作品タイトルを本文中で繰り返さない（タイトルは見出しに既に表示されている）\n"
        "- POINTSに価格やレビュー点数など数値情報は含めない（別欄に表示済みのため）\n"
        "- POINTSは『○○要素が中心のストーリー』のような機械的な言い回しを避け、\n"
        "  各項目で違う切り口・違う言い回しにする\n"
        "- 出力は必ず次のプレーンテキスト形式のみ。前置きや説明・Markdown記法は禁止。\n\n"
        "===OVERVIEW===\n"
        f"({length_variant['paragraphs']}で、上記の書き方に沿って段落間は空行で区切る)\n"
        "===POINTS===\n"
        f"(「ここがポイント」として{length_variant['points_count']}、1行1項目、先頭に「- 」を付ける。各項目は60〜100文字程度で)\n"
    )

    # Gemini API呼び出し（課金設定をしない限り無料枠で利用可能）
    # 成人向け作品の紹介文であるため、性的表現に関する安全フィルタの閾値を
    # 緩めておく（デフォルトのままだと、正当な業務利用でも出力がブロックされ
    # 空文字やフォーマット崩れの応答になりやすい）。
    resp = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent',
        params={'key': GEMINI_API_KEY},
        headers={'content-type': 'application/json'},
        json={
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                # 800〜1200字の本文＋POINTSを日本語で出力するには1600では不足しがちなため増量。
                'maxOutputTokens': 4096,
                'temperature': 0.9,
                # 内部思考（thinking）にトークンを消費させず、全予算を実際の出力に充てる。
                # thinkingBudget未対応のモデルの場合はこのフィールドは無視される想定。
                'thinkingConfig': {'thinkingBudget': 0},
            },
            'safetySettings': [
                {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_HARASSMENT',        'threshold': 'BLOCK_ONLY_HIGH'},
                {'category': 'HARM_CATEGORY_HATE_SPEECH',       'threshold': 'BLOCK_ONLY_HIGH'},
                {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_ONLY_HIGH'},
            ],
        },
        timeout=20,
    )
    data = resp.json()
    try:
        parts = data['candidates'][0]['content']['parts']
        # 応答が複数partsに分かれるケースに備え、全partsのtextを連結する
        # （parts[0]だけを見ると、モデルによっては本文の一部しか拾えないことがある）。
        text = ''.join(p.get('text', '') for p in parts).strip()
        if not text:
            raise KeyError('no text in parts')
    except (KeyError, IndexError, TypeError):
        # candidatesが無い/空の場合、多くは安全フィルタによるブロックが原因。
        # promptFeedback（ブロック理由）を含めてログに出し、原因を特定しやすくする。
        prompt_feedback = data.get('promptFeedback', {})
        raise ValueError(f'unexpected Gemini API response: promptFeedback={prompt_feedback} / raw={data}')

    if not text or '===OVERVIEW===' not in text or '===POINTS===' not in text:
        # フォーマット崩れの原因調査用に、実際に返ってきたテキストの先頭部分と
        # finishReason（MAX_TOKENS/SAFETY等）をログに出す。
        finish_reason = ''
        try:
            finish_reason = data['candidates'][0].get('finishReason', '')
        except (KeyError, IndexError, TypeError):
            pass
        preview = text[:300] if text else '(空文字)'
        raise ValueError(
            f'unexpected AI response format / finishReason={finish_reason} / text_preview={preview!r}'
        )

    overview_part = text.split('===OVERVIEW===', 1)[1].split('===POINTS===', 1)[0].strip()
    points_part = text.split('===POINTS===', 1)[1].strip()
    points = [
        line.strip().lstrip('-').strip()
        for line in points_part.splitlines()
        if line.strip().lstrip('-').strip()
    ]
    if not overview_part or not points:
        raise ValueError('empty overview or points')
    return {'overview': overview_part, 'points': points[:length_variant['points_max']]}


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


def _get_article_body_template(product: dict, focus_keyphrase: str = '', appeal_pattern: dict = None) -> dict:
    # AI生成失敗時のフォールバックだが、テンプレート運用が続いた場合でも
    # 「作者・レーベル訴求」など訴求パターンに応じて一言添える文を変え、
    # 完全に同一の文面が量産されるのを多少なりとも軽減する。
    appeal_name = (appeal_pattern or {}).get('name', '')
    genre_str = '、'.join(product['genres'][:5]) if product['genres'] else '不明'
    work_kind = '同人作品' if CONTENT_TYPE == 'doujin' else 'AV作品'

    # 冒頭の一文にフォーカスキーフレーズをそのまま含める
    # （Yoastの「冒頭のキーフレーズ」チェック対策）。
    if focus_keyphrase:
        overview = f"「{focus_keyphrase}」に注目の{work_kind}。{genre_str}系の内容です。"
    else:
        overview = f"{genre_str}系の{work_kind}です。"
    if product.get('actors') and CONTENT_TYPE == 'av':
        actor_str = '、'.join(product['actors'])
        overview += f" 出演しているのは{actor_str}。この時点でもう見る理由は十分揃っています。"
    if product.get('maker'):
        if appeal_name == '作者・レーベル訴求':
            overview += f" 手がけているのは{product['maker']}で、作風の傾向を知っている方なら安心して選べる一本です。"
        else:
            overview += f" ちなみに、手がけるのは{product['maker']}。"
    if product.get('price'):
        overview += f" さらに価格は{product['price']}なので、この内容ならコスパも十分満足できるはずです。"
    if product.get('genres'):
        overview += (
            f" つまり、{genre_str}といったジャンルが好きな方はもちろん、"
            "普段あまりこの手のジャンルを見ない方にも新鮮に映る一本というわけです。"
        )
    if product.get('sample_images'):
        overview += (
            f" サンプル画像だけでも見応え十分なので、まずは雰囲気だけでもチェックしてみると"
            "早いかもしれません。"
        )
    if product.get('review_avg') and product.get('review_count'):
        overview += (
            f" 実際、レビューでも平均{product['review_avg']}点（{product['review_count']}件）と評価は上々で、"
            "すでに多くの人がその魅力に気づいている一本です。"
        )
    closer = _OVERVIEW_CLOSERS_PICK(product)
    overview += f"\n\nそのため、{closer}"

    points = []
    for i, g in enumerate((product.get('genres') or [])[:4]):
        tmpl = _GENRE_POINT_TEMPLATES[i % len(_GENRE_POINT_TEMPLATES)]
        points.append(tmpl.format(g=g))
    if product.get('review_avg') and product.get('review_count'):
        points.append(f"また、レビュー平均{product['review_avg']}点（{product['review_count']}件）と、みんなも太鼓判")
    if product.get('maker'):
        points.append(f"制作は{product['maker']}。安定した仕上がりも安心材料のひとつです")
    if not points:
        points = ['作品ページを開いた時点で、もう半分ハマっています']

    # 本文中でもう一度フォーカスキーフレーズに触れる
    # （Yoastの「キーフレーズ分布（最低2回）」チェック対策）。
    # 6件上限で切り捨てられないよう、既に6件ある場合は末尾と差し替える。
    if focus_keyphrase:
        keyphrase_point = f"なお、「{focus_keyphrase}」が気になった方は、ぜひ作品ページもチェックしてみてください"
        if len(points) >= 6:
            points[5] = keyphrase_point
        else:
            points.append(keyphrase_point)

    return {'overview': overview, 'points': points[:6]}


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


def _sample_gallery_html(affiliate_url: str, sample_images: list, title: str, focus_keyphrase: str = '',
                          heading: str = '作品サンプル') -> str:
    imgs = [u for u in (sample_images or []) if u][:8]
    if not imgs:
        return ''
    # altテキストにフォーカスキーフレーズ（や、それと重なりがちな作品タイトル）を
    # 含めるのは最初の1枚だけにする。
    # 2枚目以降はタイトルも繰り返さず「サンプル画像2」のような汎用文言にすることで、
    # Yoastの「キーフレーズ（や同義語）の過剰使用」警告を避ける。
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
        f'<h3 class="ona-sample-gallery-title" style="margin:0 0 8px;font-size:15px;">{escape(heading)}</h3>'
        '<div class="ona-sample-grid">' + ''.join(cells) + '</div>'
        '</div>'
    )


def _sample_video_html(sample_movie_url: str) -> str:
    """サンプル動画の埋め込み。DMM APIからサンプル動画URLが取得できた作品のみ表示される。
    静止画ギャラリーだけのページより滞在時間が伸びやすく、ページの情報価値も上がる。"""
    if not sample_movie_url:
        return ''
    return (
        '<div class="ona-sample-video" style="margin:14px 0;">'
        '<h3 style="margin:0 0 8px;font-size:15px;">サンプル動画</h3>'
        f'<video controls preload="none" style="width:100%;max-width:560px;border-radius:8px;" '
        f'src="{escape(sample_movie_url)}"></video>'
        '</div>'
    )


# ================================================================
# 🎛️ 記事バリエーション設定（同一テンプレート感の軽減用）
# ================================================================
# 全記事が「同じ見出し・同じセクション順」になると、文章表現を変えても
# サイト全体としては機械生成のパターンとして見えやすい。
# content_idベースで安定的に（＝再実行しても同じ作品は同じ結果になるように）
# 構成順・見出し文言・AIへの訴求指示をばらけさせる。

# セクション構成順のバリエーション（デフォルトのハッシュ選択用）。
# 各要素名はbuild_article内のsections辞書のキーと対応する。
_SECTION_ORDER_VARIANTS = [
    ['overview', 'meta', 'badges', 'star', 'price', 'points', 'gallery', 'video', 'cta', 'internal_link', 'disclaimer'],
    ['overview', 'star', 'price', 'points', 'badges', 'meta', 'gallery', 'video', 'cta', 'internal_link', 'disclaimer'],
    ['overview', 'points', 'badges', 'meta', 'star', 'price', 'gallery', 'video', 'cta', 'internal_link', 'disclaimer'],
]

# シリーズ物: 「前作との違い」の判断material（meta欄）を早めに見せた方が
# 読者の判断に資するため、構成順を固定でこちらにする。
_SERIES_FORWARD_ORDER = [
    'overview', 'meta', 'points', 'badges', 'star', 'price', 'gallery', 'video', 'cta', 'internal_link', 'disclaimer'
]
# レビューが一定数以上ある作品: 「みんなの評価」を早めに見せて説得材料にする。
_REVIEW_FORWARD_ORDER = [
    'overview', 'star', 'meta', 'price', 'points', 'badges', 'gallery', 'video', 'cta', 'internal_link', 'disclaimer'
]
_REVIEW_FORWARD_THRESHOLD = 50  # このレビュー件数以上ならレビュー優先構成にする

_OVERVIEW_HEADING_VARIANTS = ['作品の魅力', 'この作品はどんな内容？', '注目ポイントを先取り']
_POINTS_HEADING_VARIANTS = ['✓ ここがポイント', '✓ チェックしておきたい特徴', '✓ 押さえておきたい魅力']
_GALLERY_HEADING_VARIANTS = ['作品サンプル', 'サンプルをチェック', '気になる場面をプレビュー']

# Gemini記事生成時にどの切り口を軸にするかのバリエーション。
_APPEAL_PATTERNS = [
    {
        'name': '利用シーン訴求',
        'instruction': (
            "- どんな時間帯・気分・シチュエーションで読者がこの作品を楽しみたくなるか、"
            "具体的な利用シーンを軸に描写してください\n"
        ),
    },
    {
        'name': 'ジャンル比較訴求',
        'instruction': (
            "- 似た系統の作品と比べたときに、このジャンルの組み合わせ・配分が"
            "どう違って見えるかを軸に描写してください\n"
        ),
    },
    {
        'name': '作者・レーベル訴求',
        'instruction': (
            "- 制作元（サークル/メーカー/レーベル/監督）の作風の傾向や過去作との関係を"
            "軸に描写してください（情報が乏しい場合はこの軸に固執しなくてよい）\n"
        ),
    },
    {
        'name': '読者タイプ訴求',
        'instruction': (
            "- どんな好み・経験を持つ読者に強く刺さるか／逆にどんな読者には物足りないかを"
            "軸に描写してください\n"
        ),
    },
]

# 段落数・文字数・POINTS数のバリエーション（全記事が同じ分量にならないように）。
_LENGTH_VARIANTS = [
    {'paragraphs': '3〜4段落、700〜950文字程度', 'points_count': '3〜4個', 'points_max': 4},
    {'paragraphs': '4〜5段落、900〜1150文字程度', 'points_count': '4〜5個', 'points_max': 5},
    {'paragraphs': '4〜6段落、1000〜1300文字程度', 'points_count': '5〜6個', 'points_max': 6},
]


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


def _make_description_excerpt(overview_text: str, fallback_title: str, max_len: int = 90) -> str:
    """メタディスクリプション用に、AIが生成した本文（OVERVIEW）の冒頭から
    プレーンテキストの説明文を作る。改行や段落区切りは単一スペースにまとめる。
    OVERVIEWが空の場合（テンプレート運用時など）は、作品タイトルにフォールバックする。"""
    plain = re.sub(r'\s+', ' ', (overview_text or '')).strip()
    if not plain:
        return _make_excerpt(fallback_title, max_len=max_len)
    if len(plain) > max_len:
        plain = plain[:max_len - 1].rstrip() + '…'
    return plain


_FOCUS_KEYPHRASE_EXCLUDE = {
    'ハイビジョン', '4K', '4k', 'ＨＤ', 'HD',
    '独占配信', '独占', '専売', '単体', '通販', 'DL版',
    '4時間以上作品', 'デジタル限定', '成人向け', '男性向け',
}


def _build_focus_keyphrase(product: dict, max_words: int = 2, max_chars: int = 12) -> str:
    """Yoast SEOの「フォーカスキーフレーズ」用に、出演者名・ジャンル名から
    最大max_words個の単語を選んでスペース区切りの文字列にする。
    Yoastは12文字前後を推奨しているため、文字数がmax_charsを超える場合は
    単語数をさらに削って収める。
    1文字だけの意味のないジャンル名（例:「P」）や、「ハイビジョン」「4K」など
    画質・属性系のキーワードは除外する。
    出演者名（avのみ）を優先的に含め、残り枠をジャンルで埋める。
    出演者名が無い場合（doujin等）はジャンルのみ、それも無ければサークル/メーカー名を使う。"""
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

    # 文字数がmax_charsを超える場合、収まるまで末尾の単語を削る
    while len(words) > 1 and len(' '.join(words)) > max_chars:
        words.pop()

    return ' '.join(words[:max_words])


def _build_seo_title(product: dict, keyphrase: str = '', max_len: int = 32) -> str:
    """検索結果に表示されるSEOタイトルを生成する。
    キーフレーズが既にタイトル内に含まれる場合は重複させない。"""
    title = (product.get('title') or '').strip()
    if not keyphrase:
        return title[:max_len]

    # キーフレーズが既にタイトルの先頭付近に含まれていれば、
    # 単純にタイトルをそのまま使う（重複防止）
    if keyphrase in title:
        return title[:max_len]

    remaining = max_len - len(keyphrase) - 1
    if remaining <= 0:
        return keyphrase[:max_len]
    return f'{keyphrase} {title[:remaining].rstrip()}'


def build_article(product: dict, recent_openings: list = None) -> dict:
    focus_keyphrase = _build_focus_keyphrase(product)

    # 構成順・見出し・訴求パターン・分量は、content_id（無ければタイトル）を
    # キーにした安定ハッシュで軸ごとに独立して選ぶ。これにより同じ作品は
    # 再実行しても同じ組み合わせになる一方、記事間ではバラける。
    variant_key = product.get('content_id') or product.get('title') or ''
    heading_idx = _stable_variant_index(variant_key, 'heading', len(_OVERVIEW_HEADING_VARIANTS))
    appeal_idx = _stable_variant_index(variant_key, 'appeal', len(_APPEAL_PATTERNS))
    length_idx = _stable_variant_index(variant_key, 'length', len(_LENGTH_VARIANTS))
    structure_idx = _stable_variant_index(variant_key, 'structure', len(_SECTION_ORDER_VARIANTS))

    appeal_pattern = _APPEAL_PATTERNS[appeal_idx]
    length_variant = _LENGTH_VARIANTS[length_idx]

    # 構成順は、作品の情報の有無（シリーズ物か／レビューが多いか）を優先的に見て決める。
    # 該当しない場合のみ、ハッシュによるローテーションにフォールバックする。
    if product.get('series'):
        section_order = _SERIES_FORWARD_ORDER
    elif (product.get('review_avg') and (product.get('review_count') or 0) >= _REVIEW_FORWARD_THRESHOLD):
        section_order = _REVIEW_FORWARD_ORDER
    else:
        section_order = _SECTION_ORDER_VARIANTS[structure_idx]

    body_content = get_article_body_ai(
        product, focus_keyphrase,
        appeal_pattern=appeal_pattern, length_variant=length_variant,
        recent_openings=recent_openings,
    )
    # SEO_INCLUDE_KEYPHRASE=false の場合、SEOタイトル生成にキーフレーズを渡さない
    # （_build_seo_titleはkeyphrase未指定だとタイトルそのままを返す）。
    seo_title = _build_seo_title(
        product,
        keyphrase=(focus_keyphrase if SEO_INCLUDE_KEYPHRASE else ''),
        max_len=20,
    )

    # メタディスクリプションは、作品の内容が伝わるようAI生成OVERVIEW（本文の概要部分）
    # の冒頭を使う。文字数は、Yoastが日本語（全角）を長めにカウントする傾向があるため、
    # 「80文字を超えています」という警告が出ないよう余裕を持たせて55文字までに抑える。
    overview_for_meta = body_content.get('overview', '')
    if SEO_INCLUDE_KEYPHRASE and focus_keyphrase and focus_keyphrase not in overview_for_meta:
        # キーフレーズがOVERVIEW冒頭に含まれていない場合のみ、Yoastのキーフレーズ
        # チェック対策として先頭に付与する（作品説明そのものは維持する）。
        excerpt = _make_description_excerpt(
            f'{focus_keyphrase}｜{overview_for_meta}', product['title'], max_len=55,
        )
    else:
        excerpt = _make_description_excerpt(overview_for_meta, product['title'], max_len=55)
    overview_html = _paragraphs_to_html(body_content['overview'])
    points_html = _points_list_html(
        body_content['points'], heading=_POINTS_HEADING_VARIANTS[heading_idx]
    )
    genre_badges_html = _genre_badges_html(product.get('genres', []))
    star_html = _star_rating_html(product.get('review_avg'), product.get('review_count'))
    gallery_html = _sample_gallery_html(
        product.get('affiliate_url', ''), product.get('sample_images', []), product.get('title', ''),
        focus_keyphrase, heading=_GALLERY_HEADING_VARIANTS[heading_idx]
    )
    video_html = _sample_video_html(product.get('sample_movie_url', ''))

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
        f'<h2 style="margin:14px 0 8px;font-size:17px;">{escape(_OVERVIEW_HEADING_VARIANTS[heading_idx])}</h2>'
        '<div>'
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

    # セクションを辞書として持ち、section_order（作品ごとに固定で決まる並び順）に
    # 従って組み立てる。全記事が同じ順番にならないようにするための仕組み。
    sections = {
        'overview':      overview_section_html,
        'meta':          meta_line_html,
        'badges':        genre_badges_html,
        'star':          star_html,
        'price':         price_badge_html,
        'points':        points_html,
        'gallery':       gallery_html,
        'video':         video_html,
        'cta':           cta_html,
        'internal_link': internal_link_html,
        'disclaimer':    disclaimer_html,
    }
    card_inner = '\n'.join(
        sections[name] for name in section_order if sections.get(name)
    )

    body_html = (
        '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;'
        'border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{card_inner}</div>'
    )

    # タグはジャンルのみ（フィルタなしでそのまま使用、検索性重視）。
    # 出演者名は専用タクソノミー（onavi_actress）に登録するため、タグには含めない。
    tag_source = list(product['genres']) if product['genres'] else []
    actor_source = list(product['actors']) if (CONTENT_TYPE == 'av' and product.get('actors')) else []

    # カテゴリーは「同人」または「動画」の1つだけを使う（ジャンルはカテゴリーに含めない、PRは付与しない）。
    display_category_label = '同人' if CONTENT_TYPE == 'doujin' else '動画'

    return {
        'title':             product['title'],
        'slug':              _make_slug(product.get('content_id', ''), product['title']),
        'excerpt':           excerpt,
        'body':              body_html,
        'tags':              tag_source,
        'actors':            actor_source,
        'category_label':    display_category_label,
        'featured_image_url': product.get('package_image', ''),
        'content_id':        product.get('content_id', ''),
        'focus_keyphrase':   focus_keyphrase,
        'seo_title':         seo_title,
        'affiliate_url':     product.get('affiliate_url', ''),
        # WordPressには送らない。次回実行時の自己模倣防止プロンプト用に、
        # 生成されたOVERVIEWの冒頭を投稿履歴へ記録するためだけに使う。
        'overview_text':     body_content.get('overview', ''),
    }


# ================================================================
# 🔐 WordPress REST API 投稿（アプリケーションパスワード認証・draft固定）
# ================================================================

_category_cache = {}   # name -> id
_tag_cache = {}        # name -> id
_actress_cache = {}    # name -> id（onavi_actress タクソノミー用）


def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)


_JSON_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Accept': 'application/json',
    # レンタルサーバーのbot対策（Imunify360等）は、見慣れない独自User-Agentを
    # 弾くことがあるため、一般的なブラウザのUser-Agentを名乗る。
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

    # カテゴリーは種別ラベル（doujin:「同人」／av:「動画」）の1つだけを登録する。
    # ジャンル・出演者はタグ側に登録する（build_articleで組み立て済みのarticle['tags']を使用）。
    category_label = article.get('category_label') or CONTENT_LABEL
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

    # 出演者（avのみ）は専用タクソノミー「onavi_actress」に登録する。
    # ※ WordPress側で register_taxonomy() により rest_base='onavi_actress' として
    #   REST APIに公開されている必要がある（未対応の場合はこの項目は無視される）。
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
        'status':     WP_POST_STATUS,   # 'draft' / 'pending' / 'publish'（WP_POST_STATUSの設定に従う）
        'categories': category_ids,
        'tags':       tag_ids,
    }
    if actress_ids:
        payload['onavi_actress'] = actress_ids

    # Yoast SEOの「フォーカスキーフレーズ」「SEOタイトル」「メタディスクリプション」を
    # 投稿と同時に設定する。
    # ※ WordPress側で '_yoast_wpseo_focuskw' / '_yoast_wpseo_title' / '_yoast_wpseo_metadesc'
    #   メタフィールドが register_post_meta() 等でREST APIに公開されている必要がある。
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
    # デプロイ確認用: この投稿がどのバージョンのスクリプトで生成されたかを記録する
    meta['_onavi_script_version'] = SCRIPT_VERSION

    # 記事一覧のアイキャッチ画像から直接アフィリエイトリンクへ飛ばせるように、
    # アフィリエイトURLをカスタムフィールドとしても保存しておく。
    # ※ WordPress側で '_onavi_affiliate_url' を register_post_meta() 等で
    #   REST APIに公開しておく必要がある（下記functions.php例を参照）。
    if article.get('affiliate_url'):
        meta['_onavi_affiliate_url'] = article['affiliate_url']

    if meta:
        payload['meta'] = meta

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

        # --- 診断ログ（原因切り分け用） -------------------------------
        # ・resp.history に何か入っていれば、途中でリダイレクトが発生している
        #   （POSTがGETに化ける典型パターン）。
        # ・resp.url がendpointと異なれば、リダイレクト先URLが分かる。
        # ・Cache関連ヘッダーがあれば、プロキシ/CDN層のキャッシュが疑われる。
        if resp.history:
            redirect_chain = ' -> '.join(r.url for r in resp.history) + f' -> {resp.url}'
            print(f"    🔎 リダイレクトが発生しています: {redirect_chain}")
        if resp.url != endpoint:
            print(f"    🔎 最終アクセスURLがendpointと異なります: endpoint={endpoint} / 実際={resp.url}")
        cache_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() in (
                'x-cache', 'x-cache-status', 'age', 'cf-cache-status',
                'x-litespeed-cache', 'x-proxy-cache', 'server', 'via',
            )
        }
        if cache_headers:
            print(f"    🔎 キャッシュ/プロキシ関連ヘッダー: {cache_headers}")
        print(f"    🔎 HTTPステータス: {resp.status_code}")
        # ---------------------------------------------------------------

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

def _parse_dmm_date(date_str):
    """DMMの date フィールド（例: 'YYYY-MM-DD HH:MM:SS'）をdatetimeに変換する。
    パースできない・空の場合はNoneを返す。"""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def main():
    history = load_posted_history()
    posted_history = history['posted']
    recent_openings = history['recent_openings']
    print(f'📚 投稿済み履歴: {len(posted_history)}件')

    now_jst = datetime.datetime.now(JST)
    today = now_jst.date()
    window_start = today - datetime.timedelta(days=DATE_WINDOW_DAYS)
    # 比較用に、実行時刻（JST）をtzなしdatetimeにしておく（DMMのdateフィールドはtz情報を持たないため）
    now_naive = now_jst.replace(tzinfo=None)
    # DMM API側で発売日/配信日の範囲を絞り込み、未来の予約作品を最初から取得対象外にする
    gte_date_str = datetime.datetime.combine(window_start, datetime.time.min).strftime('%Y-%m-%dT%H:%M:%S')
    lte_date_str = now_naive.strftime('%Y-%m-%dT%H:%M:%S')
    print(f'\n🏆 {_SORT_LABEL}（sort={DMM_SORT_MODE}）で上位{RANK_FETCH_LIMIT}件を取得します...'
          f'（DMM APIへ日付範囲指定: {gte_date_str} 〜 {lte_date_str}）')
    raw_items = fetch_rank_items(RANK_FETCH_LIMIT, sort=DMM_SORT_MODE,
                                  gte_date=gte_date_str, lte_date=lte_date_str)
    print(f'🏆 {len(raw_items)}件を取得しました。'
          f'（対象期間: {window_start} 〜 実行日時（{now_naive}）より過去 の発売日/配信日のみ投稿対象）')

    safe_products = []
    seen_in_run = set()
    all_skipped = []
    out_of_window_skipped = []
    dup_skipped = 0

    for item in raw_items:
        if len(safe_products) >= MAX_ARTICLES:
            break

        product = parse_product(item)
        key = product_history_key(product)

        # 【重複防止】過去に投稿済みの作品、および今回の実行内で既に選ばれた作品はスキップする
        if key in posted_history or key in seen_in_run:
            dup_skipped += 1
            continue

        ok, matched = is_safe(product)
        if not ok:
            all_skipped.append((product, matched))
            continue

        # 発売日/配信日が「実行日時（now）より過去」かつ「今日から過去DATE_WINDOW_DAYS日以内」の
        # 作品のみを対象にする。
        # ・実行日時より未来（＝当日中でもまだ配信/発売されていない予約作品）は自動的に対象外になる
        # ・window_startより古い作品も対象外になる
        product_date = _parse_dmm_date(product.get('date', ''))
        if (
            product_date is None
            or product_date > now_naive
            or product_date.date() < window_start
        ):
            out_of_window_skipped.append(product)
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

    print(f'\n📊 検索結果: {len(safe_products)}/{MAX_ARTICLES}件 集まりました '
          f'（安全フィルター除外 {len(all_skipped)}件・対象期間外のため除外 {len(out_of_window_skipped)}件・'
          f'投稿済み重複のため除外 {dup_skipped}件）')

    if out_of_window_skipped:
        print(f'   📅 対象期間（{window_start}〜{today}）外のためスキップ（例）:')
        for p in out_of_window_skipped[:10]:
            print(f"      - {p['title'][:40]}（発売日/配信日: {p.get('date')}）")
        if len(out_of_window_skipped) > 10:
            print(f'      …他{len(out_of_window_skipped) - 10}件')

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
        print('⚠️ 投稿対象の作品がありませんでした（フィルター/重複除外で全件除外、または対象期間内に該当作品なし）。')
        sys.exit(0)

    if len(safe_products) < MAX_ARTICLES:
        print(f'⚠️ {_SORT_LABEL}上位{RANK_FETCH_LIMIT}件・対象期間{DATE_WINDOW_DAYS}日以内の中では'
              f'{MAX_ARTICLES}件に届きませんでした。集まった{len(safe_products)}件のみ投稿します。')

    posted = 0
    for p in safe_products:
        print(f"\n📝 記事生成中: {p['title'][:40]}（発売日/配信日: {p.get('date') or '不明'}）")
        article = build_article(p, recent_openings=recent_openings)
        if post_draft_to_wordpress(article):
            posted += 1
            posted_history.add(product_history_key(p))
            # 今回生成した書き出し冒頭を履歴に追加し、次回以降のAI生成で
            # 同じような書き出しパターンが連続しないようにする。
            opening = (article.get('overview_text') or '')[:40]
            if opening:
                recent_openings.append(opening)
            save_posted_history(posted_history, recent_openings)

    print(f'\n✅ 完了！{posted}/{len(safe_products)} 件をWordPressに{WP_POST_STATUS}として投稿しました。')
    print(f'   📚 累計投稿履歴: {len(posted_history)}件（{POSTED_HISTORY_FILE}）')
    print('   ※ 公開前に必ず内容をご確認ください。')


if __name__ == '__main__':
    main()
