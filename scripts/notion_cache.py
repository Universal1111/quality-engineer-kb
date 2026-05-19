#!/usr/bin/env python3
"""
notion_cache.py — Notion 数据库内容缓存到本地 .md
==================================================
功能：将 Notion 关键数据库的页面内容下载为本地 .md 文件，
      使 search_my_files.py 能搜索 Notion 知识。

支持的数据库：
  - ⚡ Battery Knowledge (BATTERY_KB_DB_ID)
  - 求职投递追踪 (NOTION_DB_ID from env)

用法：
  python3 notion_cache.py          # 同步所有数据库
  python3 notion_cache.py --list   # 只列出，不下载
"""
import os, sys, json, time
from pathlib import Path
from datetime import datetime

SCRIPTS_DIR  = Path.home() / "scripts"
CACHE_DIR    = Path.home() / ".cache" / "notion_cache"
LOG_FILE     = Path.home() / "job_hunt" / "claude_bridge.log"

BATTERY_KB_DB_ID = "34d1871a-83af-81bf-9345-e03d30f89b92"


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[notion_cache] {msg}")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [notion_cache] {msg}\n")


def _load_env():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import pic2knowledge as p2k
    p2k._load_openclaw_env()


def _notion_get(url: str, params: dict = None) -> dict:
    import requests
    token = os.environ.get("NOTION_TOKEN", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    return r.json()


def _notion_post(url: str, body: dict) -> dict:
    import requests
    token = os.environ.get("NOTION_TOKEN", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=body, timeout=15)
    return r.json()


def _extract_rich_text(rich_text_list: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text_list)


def _extract_page_title(page: dict) -> str:
    """从 Notion page properties 提取标题"""
    props = page.get("properties", {})
    for key in ("Name", "标题", "Title", "title"):
        if key in props:
            p = props[key]
            if p.get("type") == "title":
                return _extract_rich_text(p.get("title", []))
    return "untitled"


def _page_to_markdown(page: dict, blocks: list) -> str:
    """将 Notion page + blocks 转为 markdown"""
    title = _extract_page_title(page)
    props = page.get("properties", {})

    # 提取基本属性
    meta_lines = [f"# {title}", ""]
    for key, val in props.items():
        if key in ("Name", "标题", "Title", "title"):
            continue
        vtype = val.get("type", "")
        text = ""
        if vtype == "rich_text":
            text = _extract_rich_text(val.get("rich_text", []))
        elif vtype == "select":
            sel = val.get("select")
            text = sel["name"] if sel else ""
        elif vtype == "multi_select":
            text = ", ".join(s["name"] for s in val.get("multi_select", []))
        elif vtype == "date":
            d = val.get("date")
            text = d["start"] if d else ""
        elif vtype == "url":
            text = val.get("url", "") or ""
        if text:
            meta_lines.append(f"**{key}**: {text}")

    meta_lines.append("")
    meta_lines.append("---")
    meta_lines.append("")

    # 转换 blocks
    content_lines = []
    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        text  = _extract_rich_text(bdata.get("rich_text", []))

        if btype == "heading_1":
            content_lines.append(f"# {text}")
        elif btype == "heading_2":
            content_lines.append(f"## {text}")
        elif btype == "heading_3":
            content_lines.append(f"### {text}")
        elif btype == "paragraph":
            content_lines.append(text)
        elif btype == "bulleted_list_item":
            content_lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            content_lines.append(f"1. {text}")
        elif btype == "code":
            lang = bdata.get("language", "")
            content_lines.append(f"```{lang}\n{text}\n```")
        elif btype == "table_row":
            cells = bdata.get("cells", [])
            row = " | ".join(_extract_rich_text(c) for c in cells)
            content_lines.append(f"| {row} |")
        elif btype == "divider":
            content_lines.append("---")

    return "\n".join(meta_lines + content_lines)


def fetch_database_pages(db_id: str, list_only: bool = False) -> list[dict]:
    """获取数据库所有页面（处理分页）"""
    pages = []
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        result = _notion_post(
            f"https://api.notion.com/v1/databases/{db_id}/query", body
        )

        if "results" not in result:
            _log(f"查询失败: {result.get('message', result)}")
            break

        pages.extend(result["results"])
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return pages


def fetch_page_blocks(page_id: str) -> list[dict]:
    """获取页面所有 blocks"""
    blocks = []
    cursor = None

    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor

        result = _notion_get(
            f"https://api.notion.com/v1/blocks/{page_id}/children", params
        )

        if "results" not in result:
            break

        blocks.extend(result["results"])
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return blocks


def sync_database(db_id: str, db_name: str, list_only: bool = False) -> int:
    """同步单个数据库到本地缓存"""
    cache_dir = CACHE_DIR / db_name
    cache_dir.mkdir(parents=True, exist_ok=True)

    _log(f"同步数据库: {db_name} ({db_id[:8]}...)")
    pages = fetch_database_pages(db_id)
    _log(f"  找到 {len(pages)} 个页面")

    if list_only:
        for p in pages:
            print(f"  - {_extract_page_title(p)}")
        return len(pages)

    synced = 0
    for page in pages:
        try:
            title    = _extract_page_title(page)
            page_id  = page["id"].replace("-", "")   # 完整32位ID
            safe_title = "".join(c if c.isalnum() or c in "_ -" else "_" for c in title)
            filename = f"{safe_title[:60]}_{page_id[:8]}.md"
            out_path = cache_dir / filename

            # 检查是否需要更新（对比 last_edited_time）
            # 用完整page_id做meta key，避免ID前缀碰撞导致167页被跳过的bug
            last_edited = page.get("last_edited_time", "")
            meta_file   = cache_dir / f".meta_{page_id}.json"   # 完整ID
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                if meta.get("last_edited") == last_edited:
                    continue  # 未修改，跳过

            # 获取页面内容
            blocks = fetch_page_blocks(page["id"])
            md     = _page_to_markdown(page, blocks)
            out_path.write_text(md, encoding="utf-8")

            # 保存 meta
            meta_file.write_text(json.dumps({"last_edited": last_edited}))
            synced += 1
            time.sleep(0.3)  # Notion API 限速
        except Exception as e:
            _log(f"  页面处理失败: {e}")

    _log(f"  完成: 新增/更新 {synced} 个文件")
    return synced


def sync_all(list_only: bool = False):
    """同步所有配置的数据库"""
    _load_env()

    databases = {
        "battery_knowledge": BATTERY_KB_DB_ID,
    }

    # 如果有 Dragon 求职 DB 也加入
    dragon_db = os.environ.get("NOTION_DB_ID", "")
    if dragon_db and dragon_db != BATTERY_KB_DB_ID:
        databases["job_tracking"] = dragon_db

    total = 0
    for name, db_id in databases.items():
        total += sync_database(db_id, name, list_only=list_only)

    # 同步完成后重建搜索索引
    if not list_only and total > 0:
        _log("更新本地搜索索引...")
        import search_my_files as smf
        # 将 notion_cache 加入搜索根
        if CACHE_DIR not in smf.SEARCH_ROOTS:
            smf.SEARCH_ROOTS.append(CACHE_DIR)
        smf.build_index()
        _log("索引更新完成")

    return total


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="只列出，不下载")
    args = parser.parse_args()
    sync_all(list_only=args.list)
