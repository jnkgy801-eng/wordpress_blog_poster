# -*- coding: utf-8 -*-
"""
🛡️ 年少者連想ワード フィルター

タイトル・ジャンルタグ・サークル名などに、未成年を想起させる語が
含まれる作品を自動的に除外するための安全装置です。

このフィルターは「投稿前に必ず通す」ことを前提にしています。
キーワードを緩める（削除する）場合は、除外対象がなぜ危険なのかを
理解した上で、慎重に判断してください。単純に検知漏れを増やす方向の
調整はおすすめしません。追加・強化（表記ゆれの追加など）を推奨します。
"""

import unicodedata

# ================================================================
# 除外キーワード一覧
# カテゴリ別に管理。表記ゆれ（全角/半角・大文字/小文字）にできるだけ
# 対応するため、正規化した上でマッチングする。
# ================================================================

EXCLUDE_KEYWORDS = [
    # 直接的な年齢表現
    "実の娘 中学", "小学生", "小学校", "小卒", "幼児", "児童",
    "中学生", "中学校", "JC", "女子中学生",
    "JK", "女子高生", "高校生", "女子校生",
    "未成年", "18歳未満", "未就学児",
    # スラング・隠語（年少者を想起させる表現）
    "ロリ", "ショタ", "幼女", "幼児体型",
    # 学校設定と組み合わさりやすい表現
    "教え子 中学", "教え子 小学", "生徒 中学", "生徒 小学",
    "姪 小学", "姪 中学", "娘 小学", "娘 中学",
]

# 注意: 「制服」単体・「学園」単体はコスプレ・大人設定の作品にも
# 頻出するため単独では入れていないが、このジャンルは見逃し（過小検知）の
# リスクが過検知のリスクより大きいため、上記のように「学校種別を示す語」
# は単独でも除外対象に含めている。
#
# ⚠️ このリストは出発点に過ぎません。運用前に必ず以下を行ってください。
# - DMM/FANZA側の年齢確認・出演者年齢証明の運用ルールを確認する
# - 実際にAPIから取得される作品タイトル・ジャンル・パッケージ表記を
#   一定期間サンプル調査し、見落としがちな表記ゆれを追加する
# - 判断に迷ってキーワードを削除・弱体化する場合は、除外対象がなぜ
#   危険なのかを理解した上で慎重に行う（追加・強化を優先する）


def _normalize(text: str) -> str:
    """全角/半角・大文字/小文字を統一して比較しやすくする。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return text.lower()


_NORMALIZED_KEYWORDS = [_normalize(k) for k in EXCLUDE_KEYWORDS]


def find_matched_keywords(*fields) -> list:
    """
    渡された文字列群（タイトル・ジャンル・サークル名など）を走査し、
    マッチした除外キーワードのリストを返す。空リストなら該当なし。
    """
    matched = []
    for field in fields:
        if not field:
            continue
        if isinstance(field, (list, tuple)):
            joined = " ".join(str(f) for f in field)
        else:
            joined = str(field)
        norm = _normalize(joined)
        for raw_kw, norm_kw in zip(EXCLUDE_KEYWORDS, _NORMALIZED_KEYWORDS):
            if norm_kw in norm and raw_kw not in matched:
                matched.append(raw_kw)
    return matched


def is_safe(product: dict) -> tuple:
    """
    product（parse_productが返す辞書）を受け取り、
    (安全か: bool, マッチしたキーワード: list) を返す。
    """
    matched = find_matched_keywords(
        product.get("title", ""),
        product.get("genres", []),
        product.get("maker", ""),
    )
    return (len(matched) == 0, matched)
