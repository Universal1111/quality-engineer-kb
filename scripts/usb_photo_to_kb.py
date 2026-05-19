#!/usr/bin/env python3
"""
usb_photo_to_kb.py — USB照片 → OCR → 知识卡片 → Obsidian + Notion
================================================================
流程：
  HEIC/JPG → sips转JPEG → tesseract OCR → 文本过滤 →
  Claude综合 → MD知识卡 → ~/Engineer_KB/02_Knowledge/USB_Photos/
                        → Notion Battery Knowledge DB

重点方向：电池工艺/CCD质检/AI算法（宁德时代相关）
"""
import os, sys, re, json, time, subprocess, hashlib, tempfile
from pathlib import Path
from datetime import datetime

PYTHON_BIN    = "/opt/homebrew/bin/python3"
CLAUDE_BIN    = str(Path.home() / ".nvm/versions/node/v22.22.2/bin/claude")
VISION_OCR    = str(Path.home() / "scripts" / "vision_ocr")   # macOS Vision OCR工具
SCRIPTS_DIR   = Path.home() / "scripts"
USB_DIR       = Path("/Volumes/DEEPINOS")
OUT_DIR       = Path.home() / "Engineer_KB" / "02_Knowledge" / "USB_Photos"
OBSIDIAN_DIR  = Path.home() / "Documents" / "Obsidian Vault" / "01_知识卡片" / "USB"
DONE_DB       = Path.home() / ".cache" / "usb_photo_kb_done.json"
LOG_FILE      = Path.home() / "job_hunt" / "usb_photo_kb.log"
NOTION_DB_ID  = "34d1871a-83af-81bf-9345-e03d30f89b92"

MIN_OCR_CHARS = 40      # OCR文本低于此字数跳过（纯风景照）
BATCH_SIZE    = 5       # 每批Claude综合的图片数
MAX_IMGS      = 9999    # 本次处理上限

# ── 日志 ──────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Done DB ───────────────────────────────────────────────
def load_done():
    if DONE_DB.exists():
        try:
            return json.loads(DONE_DB.read_text())
        except Exception:
            pass
    return {}

def save_done(db):
    DONE_DB.parent.mkdir(parents=True, exist_ok=True)
    DONE_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2))

def file_id(p: Path) -> str:
    return hashlib.md5(str(p).encode()).hexdigest()[:12]

# ── HEIC → JPEG 转换 ─────────────────────────────────────
def heic_to_jpeg(heic_path: Path, out_dir: Path) -> Path | None:
    out = out_dir / (heic_path.stem + ".jpg")
    if out.exists():
        return out
    try:
        # sips无法直接处理FAT32/exFAT上的HEIC，先复制到本地临时目录
        local_heic = out_dir / heic_path.name
        if not local_heic.exists():
            import shutil
            shutil.copy2(str(heic_path), str(local_heic))
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg",
             "-s", "formatOptions", "80",
             "--resampleWidth", "1600",   # 缩放到1600宽，加速OCR
             str(local_heic), "--out", str(out)],
            capture_output=True, timeout=30
        )
        # 清理本地HEIC副本
        try:
            local_heic.unlink()
        except Exception:
            pass
        return out if out.exists() else None
    except Exception as e:
        log(f"HEIC转换失败 {heic_path.name}: {e}")
        return None

# ── macOS Vision OCR（主力）+ tesseract（备用）─────────────
def ocr_image(img_path: Path) -> str:
    """优先使用macOS Vision框架OCR（中文精度远超tesseract），失败时回退tesseract"""
    # 主力：macOS Vision OCR
    vision_bin = Path(VISION_OCR)
    if vision_bin.exists():
        try:
            result = subprocess.run(
                [str(vision_bin), str(img_path)],
                capture_output=True, text=True, timeout=30
            )
            text = result.stdout.strip()
            if text:
                lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 1]
                return "\n".join(lines)
        except subprocess.TimeoutExpired:
            log(f"Vision OCR超时: {img_path.name}")
        except Exception as e:
            log(f"Vision OCR失败 {img_path.name}: {e}")

    # 备用：tesseract
    try:
        result = subprocess.run(
            ["tesseract", str(img_path), "stdout",
             "-l", "chi_sim+eng", "--psm", "3",
             "-c", "tessedit_char_whitelist="],
            capture_output=True, text=True, timeout=60
        )
        text = result.stdout.strip()
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 2]
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        log(f"tesseract超时: {img_path.name}")
        return ""
    except Exception as e:
        log(f"tesseract失败 {img_path.name}: {e}")
        return ""

# ── 内容分类（电池/CCD/AI相关优先）────────────────────────
BATTERY_KW  = re.compile(r'涂布|极片|卷绕|叠片|电芯|电池|正极|负极|隔膜|电解液|CATL|宁德|锂|NCM|NMC|容量|充放电|SOC|BMS|热失控', re.IGNORECASE)
CCD_KW      = re.compile(r'CCD|视觉|检测|缺陷|瑕疵|划伤|异物|漏涂|对齐|对位|AOI|视觉检测|图像|像素|相机|光源', re.IGNORECASE)
AI_KW       = re.compile(r'AI|算法|模型|神经网络|深度学习|YOLO|CNN|推理|训练|精度|召回|准确率|误检|漏检|标注|数据集', re.IGNORECASE)
QA_KW       = re.compile(r'FMEA|SPC|CP|CPK|控制图|不良|良率|失效|根因|8D|纠偏|规格|公差|过程能力', re.IGNORECASE)

def classify_text(text: str) -> tuple[str, str]:
    """返回 (category, tags)"""
    cats = []
    if BATTERY_KW.search(text): cats.append("Battery")
    if CCD_KW.search(text):     cats.append("CCD_Inspection")
    if AI_KW.search(text):      cats.append("AI_Algorithm")
    if QA_KW.search(text):      cats.append("Quality")
    if not cats:                cats.append("General")
    return cats[0], " ".join(f"#{c}" for c in cats)

# ── Claude 综合 → 知识卡片 ────────────────────────────────
CARD_PROMPT = """你是魏伟的工程知识助手，专注于动力电池制造、CCD视觉检测、AI质检算法。

以下是从{n}张照片中提取的文字内容（照片来自工程现场/技术资料）：

{ocr_texts}

请综合以上内容，生成一张结构化知识卡片，格式如下（严格Markdown）：

# {title}

## 核心知识点
（3-5条要点，每条一句话，聚焦技术原理/工艺参数/质量标准）

## 技术细节
（关键数字/参数/公式/流程步骤，有就写，没有就略）

## 与电池制造/CCD检测/AI算法的关联
（说明这批内容在电池工艺链条中的位置，以及AI/视觉检测如何应用）

## 实际应用建议
（基于魏伟的工程背景，如何把这个知识用于面试STAR表达或实际工作）

## 关键词
（5-8个关键词，用于搜索）

---
要求：中文输出，专业准确，无废话。如果OCR文本质量差看不懂，只输出能确认的内容。"""

def claude_synthesize(ocr_texts: list[str], title: str) -> str:
    combined = "\n\n---\n".join(
        f"[图片{i+1}]\n{t}" for i, t in enumerate(ocr_texts)
    )
    prompt = CARD_PROMPT.format(
        n=len(ocr_texts),
        ocr_texts=combined[:6000],  # 防超长
        title=title
    )
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": str(Path.home())},
            stdin=subprocess.DEVNULL,
        )
        return result.stdout.strip() or f"[Claude无输出] {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "[超时]"
    except Exception as e:
        return f"[异常] {e}"

# ── 保存到 Obsidian + Engineer_KB ────────────────────────
def save_to_obsidian(card_md: str, filename: str) -> Path:
    """同时写入 Engineer_KB 和 Obsidian Vault"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

    p1 = OUT_DIR / filename
    p2 = OBSIDIAN_DIR / filename
    p1.write_text(card_md, encoding="utf-8")
    p2.write_text(card_md, encoding="utf-8")
    return p1

# ── 上传到 Notion ─────────────────────────────────────────
def upload_to_notion(card_md: str, title: str, tags: str) -> bool:
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

        # 读取 Notion token
        import notion_cache as nc
        nc._load_env()
        token = os.environ.get("NOTION_TOKEN", "")
        if not token:
            log("Notion token未找到，跳过上传")
            return False

        import requests
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        }

        # 截取正文前2000字作为内容
        body_text = card_md[:2000]

        payload = {
            "parent": {"database_id": NOTION_DB_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Tags": {"multi_select": [
                    {"name": t.lstrip("#")} for t in tags.split() if t.startswith("#")
                ]},
                "Source": {"rich_text": [{"text": {"content": "USB_Photo_Import"}}]},
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": body_text}}]
                    }
                }
            ]
        }
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers, json=payload, timeout=30
        )
        if r.status_code == 200:
            return True
        else:
            log(f"Notion上传失败: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        log(f"Notion上传异常: {e}")
        return False

# ── 主流程 ─────────────────────────────────────────────────
def run(dry_run=False, limit=MAX_IMGS, start_from=0):
    if not USB_DIR.exists():
        log("U盘未挂载: /Volumes/DEEPINOS")
        return

    done = load_done()

    # 收集所有图片（含子目录），过滤macOS资源文件（._开头）
    def _collect(pattern):
        return [p for p in USB_DIR.rglob(pattern) if not p.name.startswith("._")]

    photos = sorted(
        _collect("*.HEIC") + _collect("*.heic") +
        _collect("*.jpg")  + _collect("*.JPG")  +
        _collect("*.jpeg") + _collect("*.png")
    )

    # 过滤已处理
    todo = [p for p in photos if file_id(p) not in done]
    log(f"共 {len(photos)} 张图片，待处理 {len(todo)} 张，本次上限 {limit}")

    todo = todo[start_from:start_from + limit]
    if not todo:
        log("没有新图片需要处理")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 批次处理
        batch_ocr: list[tuple[Path, str]] = []   # (原路径, ocr文本)
        processed = 0
        skipped_blank = 0
        cards_made = 0

        for i, photo in enumerate(todo):
            log(f"[{i+1}/{len(todo)}] OCR: {photo.name}")

            # 转JPEG
            if photo.suffix.upper() == ".HEIC":
                jpg = heic_to_jpeg(photo, tmp)
                if not jpg:
                    done[file_id(photo)] = {"status": "heic_fail", "ts": _now()}
                    continue
            else:
                jpg = photo

            # OCR
            text = ocr_image(jpg)
            processed += 1

            if len(text) < MIN_OCR_CHARS:
                skipped_blank += 1
                if not dry_run:
                    done[file_id(photo)] = {"status": "blank", "ts": _now()}
                else:
                    log(f"  跳过（文本{len(text)}字）")
                continue

            log(f"  OCR {len(text)}字 → 加入批次")
            batch_ocr.append((photo, text))

            # 每 BATCH_SIZE 张或到末尾，生成一张知识卡
            if len(batch_ocr) >= BATCH_SIZE or i == len(todo) - 1:
                if not batch_ocr:
                    continue

                names = [p.stem for p, _ in batch_ocr]
                title = f"USB知识卡_{names[0]}-{names[-1]}" if len(names) > 1 else f"USB知识卡_{names[0]}"
                _, tags = classify_text(" ".join(t for _, t in batch_ocr))

                log(f"  Claude综合 {len(batch_ocr)} 张 → {title}")

                if not dry_run:
                    card_md = claude_synthesize([t for _, t in batch_ocr], title)
                    # 添加frontmatter
                    frontmatter = (
                        f"---\n"
                        f"title: {title}\n"
                        f"tags: {tags}\n"
                        f"source: USB_DEEPINOS\n"
                        f"photos: {', '.join(names)}\n"
                        f"created: {_now()}\n"
                        f"---\n\n"
                    )
                    full_card = frontmatter + card_md
                    filename = f"{title}.md"
                    save_to_obsidian(full_card, filename)
                    upload_to_notion(card_md, title, tags)
                    cards_made += 1
                    log(f"  ✅ 知识卡已保存: {filename}")

                # 标记已处理（dry_run不保存，避免污染done DB）
                if not dry_run:
                    for p, _ in batch_ocr:
                        done[file_id(p)] = {"status": "done", "card": title, "ts": _now()}
                    save_done(done)
                batch_ocr = []

    log(f"\n处理完成: 扫描{processed}张，空白跳过{skipped_blank}张，生成{cards_made}张知识卡")
    return {"processed": processed, "skipped": skipped_blank, "cards": cards_made}


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ── 命令行 ────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--run",      action="store_true", help="正式运行")
    p.add_argument("--dry-run",  action="store_true", help="只OCR不生成卡片")
    p.add_argument("--limit",    type=int, default=50,  help="本次处理上限（默认50）")
    p.add_argument("--start",    type=int, default=0,   help="从第N张开始")
    p.add_argument("--reset",    action="store_true",   help="清空done记录重新处理")
    args = p.parse_args()

    if args.reset:
        DONE_DB.unlink(missing_ok=True)
        print("Done记录已清空")

    if args.dry_run:
        run(dry_run=True, limit=args.limit, start_from=args.start)
    elif args.run:
        run(dry_run=False, limit=args.limit, start_from=args.start)
    else:
        p.print_help()
