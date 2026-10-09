# -*- coding: utf-8 -*-
"""
FANZA同人 ランキング上位 → WordPress 自動投稿（1日N作品・本文は最小構成）

仕様:
  - ランキングは DMM アフィリエイトAPI の sort=rank（人気順）で取得する
  - 上位から順に見て、投稿済みなら1つ下の順位へ進み、規定数に達するまで繰り返す
  - 投稿済み判定は「履歴ファイル」と「WordPress上の同slug（下書き含む）」の二重チェック
  - 本文は サンプル画像・価格・アフィリエイトリンク のみ（タグはWPのタグとして付与）
  - 1日の投稿数は POSTS_PER_DAY（JST基準）で制限する。再実行しても超過しない

既存の scripts/wordpress_cid_poster.py には依存しない
（あちらは import 時に環境変数を検証して sys.exit するため、モジュールとして再利用できない）。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_VERSION = "2026-10-10-auto-ranking-01"
JST = dt.timezone(dt.timedelta(hours=9))

DMM_API_BASE = "https://api.dmm.com/affiliate/v3"
DMM_SERVICE = "doujin"
DMM_FLOOR = "digital_doujin"
DMM_MAX_HITS = 100  # ItemList API の hits 上限

LINK_REL = "sponsored nofollow noopener"
MAX_SAMPLE_IMAGES = 8
MAX_GENRE_TAGS = 6
# WP障害時に最大 RANK_SCAN_LIMIT 回リクエストし続けないための打ち切り回数
MAX_CONSECUTIVE_ERRORS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
VALID_POST_STATUSES = ("draft", "pending", "publish")


class ConfigError(Exception):
    """設定・履歴ファイルの不備（実行を続けると誤投稿につながるため停止する）。"""


class DmmApiError(Exception):
    """DMM APIの取得失敗。"""


class WordPressError(Exception):
    """WordPress REST APIの失敗。"""


# ================================================================
# 設定
# ================================================================

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} は整数で指定してください: {raw!r}") from e
    if value < minimum:
        raise ConfigError(f"{name} は {minimum} 以上で指定してください: {value}")
    return value


@dataclass(frozen=True)
class Config:
    dmm_api_id: str
    dmm_affiliate_id: str
    wp_url: str
    wp_username: str
    wp_app_password: str
    wp_post_status: str
    posts_per_day: int
    rank_scan_limit: int
    category_label: str
    history_file: Path
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        required = ("DMM_API_ID", "DMM_AFFILIATE_ID", "WP_URL", "WP_USERNAME", "WP_APP_PASSWORD")
        missing = [k for k in required if not os.environ.get(k, "").strip()]
        if missing:
            raise ConfigError(f"環境変数が未設定です: {', '.join(missing)}")

        status = os.environ.get("WP_POST_STATUS", "publish").strip().lower()
        # 無人運用では「不明な値は draft に丸める」より、気づけるよう停止する方が安全
        if status not in VALID_POST_STATUSES:
            raise ConfigError(f"WP_POST_STATUS が不正です: {status!r}（{'/'.join(VALID_POST_STATUSES)}）")

        return cls(
            dmm_api_id=os.environ["DMM_API_ID"].strip(),
            dmm_affiliate_id=os.environ["DMM_AFFILIATE_ID"].strip(),
            wp_url=os.environ["WP_URL"].strip().rstrip("/"),
            wp_username=os.environ["WP_USERNAME"].strip(),
            wp_app_password=os.environ["WP_APP_PASSWORD"].strip(),
            wp_post_status=status,
            posts_per_day=_env_int("POSTS_PER_DAY", 3),
            rank_scan_limit=_env_int("RANK_SCAN_LIMIT", 100),
            category_label=os.environ.get("CATEGORY_LABEL", "").strip() or "同人",
            history_file=Path(os.environ.get("HISTORY_FILE", "").strip() or "outputs/ranking_posted_history.json"),
            dry_run=os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes"),
        )


# ================================================================
# 投稿履歴（既存の outputs/ranking_posted_history.json と同じ runs 形式）
# ================================================================

class PostHistory:
    def __init__(self, path: Path, runs: list[dict[str, Any]]) -> None:
        self._path = path
        self._runs = runs
        self._ids: set[str] = {
            str(cid)
            for run in runs
            if run.get("posted")
            for cid in run.get("content_ids", [])
        }

    @classmethod
    def load(cls, path: Path) -> "PostHistory":
        if not path.exists():
            return cls(path, [])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # 壊れた履歴を「空」として扱うと過去作品を再投稿しかねないので止める
            raise ConfigError(f"履歴ファイルを読み込めません: {path} ({e})") from e
        runs = data.get("runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise ConfigError(f"履歴ファイルの形式が不正です（'runs' がありません）: {path}")
        return cls(path, runs)

    def __contains__(self, content_id: str) -> bool:
        return content_id in self._ids

    def count_on(self, date: str) -> int:
        return sum(
            len(run.get("content_ids", []))
            for run in self._runs
            if run.get("posted") and run.get("date") == date
        )

    def record(self, content_id: str, date: str, status: str) -> None:
        """1作品投稿するたびに保存する（途中でジョブが落ちても記録が残るように）。"""
        for run in reversed(self._runs):
            if run.get("posted") and run.get("date") == date and run.get("status") == status:
                run.setdefault("content_ids", []).append(content_id)
                break
        else:
            self._runs.append(
                {"date": date, "posted": True, "status": status, "content_ids": [content_id]}
            )
        self._ids.add(content_id)
        self._save()

    def _save(self) -> None:
        tmp_path: Optional[str] = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"runs": self._runs}, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self._path)  # 書き込み途中で壊れた履歴を残さない
        except OSError as e:
            # 投稿自体は完了済み。次回はWP側のslugチェックが重複を防ぐので続行する
            print(f"⚠️ 履歴の保存に失敗しました（WP側の重複チェックで補完されます）: {e}")
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)


# ================================================================
# 商品データ
# ================================================================

@dataclass(frozen=True)
class Product:
    content_id: str
    title: str
    affiliate_url: str
    price: str
    genres: tuple[str, ...]
    maker: str
    series: str
    package_image: str
    sample_images: tuple[str, ...]


_TITLE_PREFIX_RE = re.compile(r"^【[^】]{1,20}】\s*")


def _build_affiliate_url(raw_url: str, affiliate_id: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlsplit(raw_url)
    query = "&".join(kv for kv in parsed.query.split("&") if kv and not kv.startswith("utm_"))
    clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    return (
        f"https://al.dmm.co.jp/?lurl={urllib.parse.quote(clean, safe='')}"
        f"&af_id={affiliate_id}&ch=api&ch_id=link"
    )


def _resolve_affiliate_url(item: dict[str, Any], affiliate_id: str) -> str:
    raw = item.get("affiliateURL", "")
    if raw and ("al.dmm.co.jp" in raw or "al.fanza.co.jp" in raw):
        return raw
    return _build_affiliate_url(raw or item.get("URL", ""), affiliate_id)


def _parse_price(prices: dict[str, Any]) -> str:
    value = prices.get("price") or prices.get("list_price") or ""
    # "880~1000" のような表記でも先頭の数値だけを採る（数字を連結して桁が壊れるのを防ぐ）
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return ""
    return f"¥{int(match.group().replace(',', '')):,}"


def _parse_sample_images(item: dict[str, Any]) -> tuple[str, ...]:
    block = item.get("sampleImageURL") or {}
    for key in ("sample_l", "sample_s"):
        images = (block.get(key) or {}).get("image") or []
        if isinstance(images, str):
            images = [images]
        urls = tuple(u for u in images if u)
        if urls:
            return urls
    return ()


def parse_product(item: dict[str, Any], affiliate_id: str) -> Product:
    info = item.get("iteminfo") or {}
    image = item.get("imageURL") or {}
    return Product(
        content_id=str(item.get("content_id") or item.get("product_id") or ""),
        title=_TITLE_PREFIX_RE.sub("", item.get("title", ""), count=1).strip(),
        affiliate_url=_resolve_affiliate_url(item, affiliate_id),
        price=_parse_price(item.get("prices") or {}),
        genres=tuple(g.get("name", "") for g in (info.get("genre") or []) if g.get("name")),
        maker=((info.get("maker") or [{}])[0]).get("name", ""),
        series=((info.get("series") or [{}])[0]).get("name", ""),
        package_image=image.get("large") or image.get("small") or "",
        sample_images=_parse_sample_images(item),
    )


def collect_tag_names(product: Product, category_label: str) -> list[str]:
    """ジャンル（上位N件）＋サークル＋シリーズ。薄いアーカイブの量産を避けるためジャンルには上限を設ける。"""
    names = list(product.genres[:MAX_GENRE_TAGS])
    for extra in (product.maker, product.series):
        if extra and extra not in names:
            names.append(extra)
    return [n for n in names if n != category_label]


def make_slug(content_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-]+", "-", content_id).strip("-").lower()


# ================================================================
# 記事本文（サンプル画像・価格・アフィリエイトリンクのみ）
# ================================================================

def _esc(text: str) -> str:
    return html.escape(text, quote=True)  # 属性値にも使うため quote=True


def build_body(product: Product) -> str:
    url = _esc(product.affiliate_url)

    # 広告表記はステマ規制で必要なため、本文を最小化しても1行だけ残す
    disclosure = (
        '<p style="font-size:12px;color:#888;margin:0 0 10px;text-align:center;">'
        "※本記事にはアフィリエイト広告（PR）が含まれます。</p>"
    )

    price_html = ""
    if product.price:
        price_html = (
            '<div style="text-align:center;margin:0 0 14px;">'
            '<span style="display:inline-block;background:#e0507a;color:#fff;padding:6px 18px;'
            f'border-radius:999px;font-size:15px;font-weight:bold;">💰 {_esc(product.price)}</span></div>'
        )

    gallery_html = ""
    images = product.sample_images[:MAX_SAMPLE_IMAGES]
    if images:
        # class名は手動投稿（wordpress_cid_poster.py）と揃え、テーマ側CSSをそのまま効かせる
        cells = "".join(
            f'<a href="{url}" target="_blank" rel="{LINK_REL}" class="ona-sample-cell">'
            f'<img src="{_esc(img)}" alt="{_esc(product.title)} サンプル画像{i}" '
            'loading="lazy" class="ona-sample-img"></a>'
            for i, img in enumerate(images, start=1)
        )
        gallery_html = (
            '<div class="ona-sample-gallery">'
            '<h3 class="ona-sample-gallery-title" style="margin:0 0 8px;font-size:15px;">サンプル画像</h3>'
            f'<div class="ona-sample-grid">{cells}</div></div>'
        )

    cta_html = (
        '<div style="text-align:center;margin-top:18px;">'
        f'<a href="{url}" target="_blank" rel="{LINK_REL}" '
        'style="display:inline-block;padding:14px 36px;background:linear-gradient(135deg,#ff6f91,#e0507a);'
        'color:#fff;text-decoration:none;border-radius:999px;font-size:16px;font-weight:bold;">'
        "▶ 作品ページを見る</a></div>"
    )

    return (
        disclosure
        + '<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #eee;border-radius:16px;">'
        + price_html
        + gallery_html
        + cta_html
        + "</div>"
    )


# ================================================================
# HTTP / DMM API
# ================================================================

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    # GETのみリトライ。POSTを再送すると二重投稿になり得るため対象外にする
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_ranking(
    session: requests.Session, cfg: Config, limit: int, now: dt.datetime
) -> list[dict[str, Any]]:
    """人気順(sort=rank)で上位 limit 件を取得する。"""
    items: list[dict[str, Any]] = []
    offset = 1
    while len(items) < limit:
        hits = min(DMM_MAX_HITS, limit - len(items))
        params = {
            "api_id": cfg.dmm_api_id,
            "affiliate_id": cfg.dmm_affiliate_id,
            "site": "FANZA",
            "service": DMM_SERVICE,
            "floor": DMM_FLOOR,
            "hits": hits,
            "offset": offset,
            "sort": "rank",
            # 未来日付（予約）の作品は販売実績がないため除外する
            "lte_date": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "output": "json",
        }
        try:
            resp = session.get(f"{DMM_API_BASE}/ItemList", params=params, timeout=15)
            resp.raise_for_status()
            result = (resp.json() or {}).get("result") or {}
        except (requests.RequestException, ValueError) as e:
            if items:
                print(f"⚠️ ランキングの追加取得に失敗（取得済み{len(items)}件で続行）: {e}")
                break
            raise DmmApiError(f"ランキング取得に失敗しました: {e}") from e

        status = result.get("status")
        if status not in (None, 200, "200"):
            raise DmmApiError(f"DMM APIがエラーを返しました status={status} message={result.get('message')}")

        page = result.get("items") or []
        if isinstance(page, dict):
            page = page.get("item", [])
        page = [p for p in page if isinstance(p, dict)]
        if not page:
            break
        items.extend(page)
        offset += hits
    return items[:limit]


# ================================================================
# WordPress REST API
# ================================================================

class WordPressClient:
    def __init__(self, session: requests.Session, cfg: Config) -> None:
        self._session = session
        self._base = f"{cfg.wp_url}/wp-json/wp/v2"
        self._auth = (cfg.wp_username, cfg.wp_app_password)  # DMM側へ認証情報を送らないよう呼び出しごとに渡す
        self._term_cache: dict[tuple[str, str], int] = {}

    def slug_exists(self, slug: str) -> bool:
        """同slugの投稿が（下書き・非公開含め）存在するか。確認できない場合は例外にする。"""
        try:
            resp = self._session.get(
                f"{self._base}/posts",
                params={"slug": slug, "status": "any", "per_page": 1, "_fields": "id"},
                auth=self._auth,
                timeout=15,
            )
            if resp.status_code != 200:
                raise WordPressError(f"重複チェック失敗 status={resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            # 手動版と違い、確認できなければ投稿しない（無人運用で二重投稿を出さないため）
            raise WordPressError(f"重複チェックでエラー: {e}") from e
        return isinstance(data, list) and len(data) > 0

    def get_or_create_term(self, taxonomy: str, name: str) -> Optional[int]:
        """タグ/カテゴリーのIDを返す。失敗しても投稿は止めない（Noneを返す）。"""
        key = (taxonomy, name)
        if key in self._term_cache:
            return self._term_cache[key]
        endpoint = f"{self._base}/{taxonomy}"
        try:
            resp = self._session.get(
                endpoint, params={"search": name, "per_page": 100}, auth=self._auth, timeout=15
            )
            if resp.status_code == 200:
                for term in resp.json():
                    # WPは "&" 等をHTMLエンティティで返すため unescape して比較する
                    if isinstance(term, dict) and html.unescape(term.get("name", "")) == name:
                        self._term_cache[key] = term["id"]
                        return term["id"]

            resp = self._session.post(endpoint, json={"name": name}, auth=self._auth, timeout=15)
            body = resp.json()
            if resp.status_code in (200, 201) and isinstance(body, dict) and "id" in body:
                self._term_cache[key] = body["id"]
                return body["id"]
            if isinstance(body, dict) and body.get("code") == "term_exists":
                term_id = (body.get("data") or {}).get("term_id")
                if term_id:
                    self._term_cache[key] = term_id
                    return term_id
            print(f"    ⚠️ {taxonomy}「{name}」の作成に失敗 status={resp.status_code}: {resp.text[:150]}")
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"    ⚠️ {taxonomy}「{name}」の取得/作成エラー: {e}")
        return None

    def upload_featured_image(self, image_url: str, content_id: str, alt: str) -> Optional[int]:
        """アイキャッチ（パッケージ画像）。本文には載せず、テーマの一覧表示用にのみ使う。"""
        if not image_url:
            return None
        try:
            img = self._session.get(image_url, timeout=20)
            img.raise_for_status()
            content_type = img.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            ext = mimetypes.guess_extension(content_type) or ".jpg"
            safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", content_id) or "item"
            resp = self._session.post(
                f"{self._base}/media",
                data=img.content,
                auth=self._auth,
                headers={
                    "Content-Type": content_type,
                    "Content-Disposition": f'attachment; filename="featured-{safe_id}{ext}"',
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                print(f"    ⚠️ アイキャッチのアップロード失敗 status={resp.status_code}: {resp.text[:150]}")
                return None
            media_id = resp.json()["id"]
            self._session.post(
                f"{self._base}/media/{media_id}",
                json={"alt_text": alt, "title": alt},
                auth=self._auth,
                timeout=15,
            )
            return media_id
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"    ⚠️ アイキャッチ処理エラー: {e}")
            return None

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._session.post(f"{self._base}/posts", json=payload, auth=self._auth, timeout=30)
            if resp.status_code not in (200, 201):
                raise WordPressError(f"投稿失敗 status={resp.status_code}: {resp.text[:300]}")
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise WordPressError(f"投稿エラー: {e}") from e
        if not isinstance(result, dict) or "id" not in result:
            raise WordPressError(f"投稿データが返りませんでした: {str(result)[:300]}")
        return result


def publish_product(wp: WordPressClient, cfg: Config, product: Product, slug: str) -> str:
    category_id = wp.get_or_create_term("categories", cfg.category_label)
    tag_ids = [
        tid
        for name in collect_tag_names(product, cfg.category_label)
        if (tid := wp.get_or_create_term("tags", name)) is not None
    ]
    media_id = wp.upload_featured_image(
        product.package_image, product.content_id, f"{product.title} パッケージ画像"
    )

    payload: dict[str, Any] = {
        "title": product.title,
        "slug": slug,
        "excerpt": product.title[:90],
        "content": build_body(product),
        "status": cfg.wp_post_status,
        "categories": [category_id] if category_id else [],
        "tags": tag_ids,
        # サイト側スニペットが参照する可能性があるため、手動版と同じメタを維持する
        "meta": {
            "_onavi_script_version": SCRIPT_VERSION,
            "_onavi_affiliate_url": product.affiliate_url,
        },
    }
    if media_id:
        payload["featured_media"] = media_id

    result = wp.create_post(payload)
    return str(result.get("link", ""))


# ================================================================
# メイン
# ================================================================

def run(cfg: Config) -> int:
    now = dt.datetime.now(JST)
    today = now.date().isoformat()
    history = PostHistory.load(cfg.history_file)

    done_today = history.count_on(today)
    # dry-run は選定結果を確認する目的なので、本日の投稿済み数に関わらず上限分を表示する
    quota = cfg.posts_per_day if cfg.dry_run else cfg.posts_per_day - done_today

    print(f"🏷️ version={SCRIPT_VERSION} / status={cfg.wp_post_status} / dry_run={cfg.dry_run}")
    print(f"📚 履歴 {len(history._ids)}件 / 本日({today})投稿済み {done_today}件 / 今回の投稿枠 {max(quota, 0)}件")
    if quota <= 0:
        print("✅ 本日の投稿上限に達しているため、何もせず終了します。")
        return 0

    session = build_session()
    ranking = fetch_ranking(session, cfg, cfg.rank_scan_limit, now)
    if not ranking:
        raise DmmApiError("ランキングが0件でした（API条件を確認してください）")
    print(f"🏆 ランキング上位 {len(ranking)}件を取得しました。")

    wp = WordPressClient(session, cfg)
    posted = 0
    consecutive_errors = 0

    for rank, item in enumerate(ranking, start=1):
        if posted >= quota:
            break
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"🛑 WordPress側のエラーが{MAX_CONSECUTIVE_ERRORS}回連続したため中断します。")
            break

        product = parse_product(item, cfg.dmm_affiliate_id)
        if not product.content_id or not product.title:
            continue
        label = f"#{rank} {product.content_id}"

        if product.content_id in history:
            print(f"⏭️ {label}: 履歴にあるため次の順位へ")
            continue

        slug = make_slug(product.content_id)
        try:
            if wp.slug_exists(slug):
                consecutive_errors = 0
                print(f"⏭️ {label}: WordPress上に既存のため次の順位へ")
                continue
            consecutive_errors = 0

            if cfg.dry_run:
                print(f"🧪 [dry-run] {label} {product.price or '価格不明'} "
                      f"サンプル{len(product.sample_images)}枚 tags={collect_tag_names(product, cfg.category_label)}")
                posted += 1
                continue

            if not product.sample_images:
                print(f"    ⚠️ {label}: サンプル画像がありません（このまま投稿します）")
            link = publish_product(wp, cfg, product, slug)
        except WordPressError as e:
            consecutive_errors += 1
            print(f"❌ {label}: {e}")
            continue

        history.record(product.content_id, today, cfg.wp_post_status)
        posted += 1
        print(f"✅ {label}: {cfg.wp_post_status}として投稿 {product.title[:40]} → {link}")

    print(f"\n📊 結果: {posted}/{quota}件")
    if posted < quota:
        print("⚠️ 規定数に届きませんでした（走査範囲内の作品が投稿済み、またはエラー）。")
        return 1
    return 0


def main() -> int:
    try:
        return run(Config.from_env())
    except (ConfigError, DmmApiError) as e:
        print(f"❌ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
