# -*- coding: utf-8 -*-
import json
from wordpress_blog_poster import fetch_dmm_products  # ファイル名は実際のものに合わせてください

items = fetch_dmm_products(offset=1, hits=1)
if items:
    print(json.dumps(items[0].get('iteminfo', {}), ensure_ascii=False, indent=2))
else:
    print('取得できませんでした（環境変数やCONTENT_TYPEの設定を確認してください）')
