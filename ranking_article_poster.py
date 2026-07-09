# -*- coding: utf-8 -*-
"""
🏆 FANZA同人/動画 → WordPress ランキングまとめ記事 自動生成・投稿ツール

wordpress_blog_poster.py が「1作品=1記事」なのに対し、こちらは
DMM APIの人気順(rank)データを使って「TOP N ランキング記事」を
1本にまとめて生成し、WordPressへ下書き投稿します。

紹介文は実際に取得した作品データ(ジャンル・価格・レビュー点数)のみを
根拠に生成し、視聴済みであるかのような一人称の体験談は生成しません。
--------------------------------------------------------------
"""

import os
import sys
import json
import datetime
from pathlib import Path
from xml.sax.saxutils import escape

# 既存スクリプトの関数・設定をそのまま再利用する
from wordpress_blog_poster import (
    DMM_API_ID, DMM_AFFILIATE_ID, WP_POST_STATUS, CONTENT_TYPE, CONTENT_LABEL,
    fetch_dmm_products, parse_product, product_history_key,
    _genre_badges_html, _star_rating_html, _make_excerpt,
    post_draft_to_wordpress, _get_article_body_template,
)
from age_safety_filter import is_safe

RANKING_TOP_N = int(os.environ.get('RANKING_TOP_N', '5'))
RANKING_FETCH_PAGES = int(os.environ.get('RANKING_FETCH_PAGES', '5'))
RANKING_HISTORY_FILE = Path(os.environ.get('RANKING_HISTORY_FILE', 'outputs/ranking_posted_history.json'))

_RANK_COLORS = {1: '#e0507a', 2: '#c9a227', 3: '#8d8d8d'}
_RANK_COLOR_DEFAULT = '#4b93ff'


def _period_label() -> str:
    """記事タイトル用の期間ラベル（例: 2026年7月第2週）を今日の日付から作る。"""
    today = datetime.date.today()
    week_of_month = (today.day - 1) // 7 + 1
    return f'{today.year}年{today.month}月第{week_of_month}週'


def load_ranking_history() -> dict:
    if not RANKING_HISTORY_FILE.exists():
        return {'runs': []}
    try:
        with open(RANKING_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'⚠️ ランキング履歴の読み込みに失敗しました（新規として扱います）: {e}')
        return {'runs': []}


def save_ranking_history(history: dict) -> None:
    RANKING_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(RANKING_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'⚠️ ランキング履歴の保存に失敗しました: {e}')


def fetch_ranking_candidates(top_n: int) -> list:
    """rank順（人気順）でDMM APIから取得し、安全フィルターを通過した上位N件を返す。"""
    candidates = []
    seen = set()
    offset = 1
    for page in range(1, RANKING_FETCH_PAGES + 1):
        if len(candidates) >= top_n:
            break
        raw_items = fetch_dmm_products(offset)
        if not raw_items:
            break
        for item in raw_items:
            if len(candidates) >= top_n:
                break
            product = parse_product(item)
            key = product_history_key(product)
            if key in seen:
                continue
            ok, _matched = is_safe(product)
            if not ok:
                continue
            if not product.get('package_image'):
                continue
            seen.add(key)
            candidates.append(product)
        offset += 100
    return candidates


def _rank_badge_html(rank: int) -> str:
    color = _RANK_COLORS.get(rank, _RANK_COLOR_DEFAULT)
    return (
        f'<div style="position:absolute;top:-10px;left:-10px;width:40px;height:40px;'
        f'border-radius:50%;background:{color};color:#fff;display:flex;'
        'align-items:center;justify-content:center;font-weight:bold;font-size:16px;'
        'box-shadow:0 2px 6px rgba(0,0,0,0.25);">'
        f'{rank}</div>'
    )


def _ranking_item_html(rank: int, product: dict) -> str:
    body = _get_article_body_template(product)  # 実データ（ジャンル・レビュー）に基づく短文のみ。体験談は含まない
    point = body['points'][0] if body['points'] else ''

    genre_badges = _genre_badges_html(product.get('genres', []))
    star_html = _star_rating_html(product.get('review_avg'), product.get('review_count'))

    price_html = ''
    if product.get('price'):
        price_html = (
            '<div style="display:inline-block;background:#fff0f5;color:#e0507a;'
            'border:1px solid #ffc2d6;border-radius:8px;padding:4px 12px;'
            f'font-size:13px;font-weight:bold;margin:6px 0;">価格 {escape(product["price"])}</div>'
        )

    return (
        '<div style="position:relative;display:flex;gap:16px;align-items:flex-start;'
        'padding:16px;margin:0 0 18px;border:1px solid #eee;border-radius:14px;'
        'box-shadow:0 1px 8px rgba(0,0,0,0.05);">'
        f'{_rank_badge_html(rank)}'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        'style="flex:0 0 140px;">'
        f'<img src="{escape(product["package_image"])}" alt="{escape(product["title"])}" '
        'loading="lazy" style="width:100%;height:auto;border-radius:8px;display:block;">'
        '</a>'
        '<div style="flex:1;min-width:0;">'
        f'<h3 style="margin:0 0 6px;font-size:16px;line-height:1.4;">'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        f'style="color:#222;text-decoration:none;">{escape(product["title"])}</a></h3>'
        f'{genre_badges}{star_html}{price_html}'
        f'<p style="margin:6px 0 10px;color:#555;font-size:13px;line-height:1.6;">{escape(point)}</p>'
        f'<a href="{escape(product["affiliate_url"])}" target="_blank" rel="nofollow" '
        'style="display:inline-block;padding:8px 20px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:13px;font-weight:bold;">'
        '▶ 詳細を見る</a>'
        '</div></div>'
    )


def build_ranking_article(products: list) -> dict:
    period = _period_label()
    title = f'【{period}】FANZA{CONTENT_LABEL}人気ランキングTOP{len(products)}'

    intro_html = (
        '<p style="line-height:1.8;color:#333;margin:0 0 16px;">'
        f'DMM/FANZAの実際のランキングデータをもとに、今売れている{CONTENT_LABEL}の人気作品'
        f'TOP{len(products)}をまとめました。気になる作品があれば、画像またはリンクから'
        '詳細ページをチェックしてみてください。</p>'
    )

    items_html = ''.join(
        _ranking_item_html(i + 1, p) for i, p in enumerate(products)
    )

    disclaimer_html = (
        '<p style="color:#999;font-size:12px;line-height:1.6;margin-top:16px;">'
        '※本ランキングはDMM/FANZAアフィリエイトAPIの人気順データに基づき自動生成しています。'
        '※成人向けコンテンツを含みます。18歳未満の方はご利用いただけません。'
        '※本記事にはアフィリエイトリンク（広告）を含みます。</p>'
    )

    body_html = (
        '<div style="max-width:640px;margin:0 auto;padding:20px;font-family:'
        '-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',sans-serif;">'
        f'{intro_html}{items_html}{disclaimer_html}</div>'
    )

    excerpt = _make_excerpt(
        f'{period}のFANZA{CONTENT_LABEL}人気ランキングTOP{len(products)}をまとめました。', []
    )

    # カテゴリー: 種別ラベル + 上位ジャンル（重複除く・最大3件）+ PR
    genre_categories = []
    for p in products:
        for g in p.get('genres', []):
            if g not in genre_categories:
                genre_categories.append(g)
        if len(genre_categories) >= 3:
            break

    slug = f'ranking-{datetime.date.today().isoformat()}'

    return {
        'title': title,
        'slug': slug,
        'excerpt': excerpt,
        'body': body_html,
        'categories': genre_categories[:3] + [CONTENT_LABEL, 'PR'],
        'genre_categories': genre_categories[:3],
        'featured_image_url': products[0].get('package_image', '') if products else '',
        'content_id': slug,
    }


def main():
    if not DMM_API_ID or not DMM_AFFILIATE_ID:
        print('❌ 環境変数 DMM_API_ID / DMM_AFFILIATE_ID が設定されていません。')
        sys.exit(1)

    print(f'📌 ランキング記事生成: 上位{RANKING_TOP_N}件（{CONTENT_LABEL}）')
    products = fetch_ranking_candidates(RANKING_TOP_N)

    if len(products) < 2:
        print('⚠️ 安全フィルター通過後の候補が少なすぎるため、記事生成を中止しました。')
        sys.exit(0)

    if len(products) < RANKING_TOP_N:
        print(f'⚠️ 候補が{len(products)}件しか集まりませんでした。'
              f'{len(products)}件でランキング記事を作成します。')

    article = build_ranking_article(products)
    ok = post_draft_to_wordpress(article)

    history = load_ranking_history()
    history.setdefault('runs', []).append({
        'date': datetime.date.today().isoformat(),
        'posted': ok,
        'status': WP_POST_STATUS,
        'content_ids': [p.get('content_id') for p in products],
    })
    save_ranking_history(history)

    if ok:
        print(f'✅ ランキング記事を{WP_POST_STATUS}として投稿しました: {article["title"]}')
        print('   ※ 公開前に必ず内容をご確認ください。')
    else:
        print('❌ ランキング記事の投稿に失敗しました。')
        sys.exit(1)


if __name__ == '__main__':
    main()
