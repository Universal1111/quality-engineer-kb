#!/usr/bin/env python3
"""
daily_intelligence.py — 每日AI求职情报简报
=============================================
功能：
  1. 搜集最新AI/电池/CCD岗位情报（BOSS直聘DB/招聘网站）
  2. 评估知识库三大漏洞（知识/求职/AI进入路径）
  3. 生成结构化简报 → 发送到 Gmail (wei90wei@gmail.com)
  4. 支持邮件回复触发动作（TODO: IMAP轮询）

用法：
  python3 daily_intelligence.py --send     # 生成并发送
  python3 daily_intelligence.py --preview  # 只生成不发送
  python3 daily_intelligence.py --test     # 测试邮件发送
"""
import os, sys, json, smtplib, sqlite3
from pathlib import Path
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SCRIPTS_DIR  = Path.home() / "scripts"
KB_DIR       = Path.home() / "Engineer_KB" / "02_Knowledge"
OBS_DIR      = Path.home() / "Documents" / "Obsidian Vault"
DRAGON_DB    = Path.home() / "job_hunt" / "dragon_v4.db"
LOG_FILE     = Path.home() / "job_hunt" / "daily_intelligence.log"

# 邮件配置（从环境变量读取）
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
TO_EMAIL  = "wei90wei@gmail.com"   # 收件Gmail

CLAUDE_BIN = str(Path.home() / ".nvm/versions/node/v22.22.2/bin/claude")


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


# ── 数据采集 ──────────────────────────────────────────────────

def get_kb_stats() -> dict:
    """知识库统计"""
    stats = {}
    for domain_dir in (KB_DIR / "USB_Photos").parent.iterdir():
        if domain_dir.is_dir():
            count = sum(1 for _ in domain_dir.rglob("*.md"))
            stats[domain_dir.name] = count
    obs_total = sum(1 for _ in OBS_DIR.rglob("*.md") if ".obsidian" not in str(_))
    stats["Obsidian总计"] = obs_total
    return stats


def get_job_stats() -> dict:
    """从Dragon DB获取求职数据"""
    if not DRAGON_DB.exists():
        return {"error": "Dragon DB不存在"}
    try:
        conn = sqlite3.connect(DRAGON_DB)
        cur = conn.cursor()
        today = date.today().isoformat()
        week_ago = (date.today().replace(day=max(1, date.today().day-7))).isoformat()

        stats = {}
        # 总投递数
        cur.execute("SELECT COUNT(*) FROM applications")
        stats["总投递"] = cur.fetchone()[0]
        # 今日新投递
        cur.execute("SELECT COUNT(*) FROM applications WHERE date(applied_at) = ?", (today,))
        stats["今日投递"] = cur.fetchone()[0]
        # 近7天
        cur.execute("SELECT COUNT(*) FROM applications WHERE date(applied_at) >= ?", (week_ago,))
        stats["近7天"] = cur.fetchone()[0]
        # 有回复
        try:
            cur.execute("SELECT COUNT(*) FROM applications WHERE status IN ('replied','interview','offer')")
            stats["有回复"] = cur.fetchone()[0]
        except Exception:
            stats["有回复"] = "N/A"
        conn.close()
        return stats
    except Exception as e:
        return {"error": str(e)}


def get_new_kb_cards(days: int = 1) -> list[str]:
    """获取最近新增的知识卡片"""
    import time
    cutoff = time.time() - days * 86400
    new_cards = []
    for md in KB_DIR.rglob("*.md"):
        if md.stat().st_mtime > cutoff and "Notion_Sync" not in str(md):
            new_cards.append(md.name[:60])
    return new_cards[:10]


def assess_gaps_with_claude() -> str:
    """用Claude评估三大漏洞"""
    import subprocess

    # 采集上下文
    kb_stats = get_kb_stats()
    job_stats = get_job_stats()
    new_cards = get_new_kb_cards(1)
    usb_done = sum(1 for _ in (KB_DIR / "USB_Photos").glob("*.md")) if (KB_DIR / "USB_Photos").exists() else 0

    prompt = f"""你是魏伟的AI助手，今天是{date.today()}。请基于以下数据，用中文输出今日情报简报（每节3-5句话）：

## 当前数据
知识库统计: {json.dumps(kb_stats, ensure_ascii=False)}
今日新增卡片: {new_cards if new_cards else '无新增'}
USB知识卡总数: {usb_done}
求职数据: {json.dumps(job_stats, ensure_ascii=False)}

## 三项永久任务评估（简短，每项2-3句）

### 1. 知识库漏洞
- 当前最大缺口是什么？（CCD/电池/AI哪个领域薄弱）
- 今日建议补充哪类内容？

### 2. 求职漏洞
- 基于投递数据，当前最大问题？
- 今日建议：应该投哪类岗位/联系哪个渠道？

### 3. AI行业进入机会
- 当前最值得学习的1个具体技能/项目（结合GitHub前沿）
- 最近一个可行动的切入点

## 今日行动TOP3
给出今天最重要的3件事（具体可执行）

输出要精炼，总字数不超过400字。"""

    try:
        # 移除sk-placeholder，确保Claude使用OAuth而非API key
        clean_env = {k: v for k, v in os.environ.items()
                     if k != "ANTHROPIC_API_KEY" or not v.startswith("sk-placeholder")}
        clean_env["HOME"] = str(Path.home())
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=90,
            env=clean_env,
            stdin=subprocess.DEVNULL,
        )
        return result.stdout.strip() or f"Claude无输出: {result.stderr[:100]}"
    except Exception as e:
        return f"评估异常: {e}"


# ── 邮件生成 ──────────────────────────────────────────────────

def build_email_html(assessment: str, kb_stats: dict, job_stats: dict, new_cards: list) -> tuple[str, str]:
    """生成邮件HTML和纯文本"""
    today = date.today().strftime("%Y年%m月%d日")
    weekday = ["周一","周二","周三","周四","周五","周六","周日"][date.today().weekday()]

    subject = f"🤖 魏伟AI情报简报 · {today} {weekday}"

    html = f"""
<html><body style="font-family: -apple-system, sans-serif; max-width: 640px; margin: 0 auto; color: #333;">
<div style="background: linear-gradient(135deg, #1a1a2e, #16213e); color: white; padding: 24px; border-radius: 12px 12px 0 0;">
  <h2 style="margin:0; font-size:20px;">🤖 魏伟AI日报 · {today} {weekday}</h2>
  <p style="margin:8px 0 0; opacity:0.8; font-size:13px;">知识库漏洞 · 求职漏洞 · AI进入机会</p>
</div>

<div style="border: 1px solid #e5e7eb; border-top: none; padding: 24px; border-radius: 0 0 12px 12px;">

<h3 style="color: #6366f1; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px;">📊 今日数据</h3>
<table style="width:100%; border-collapse: collapse; font-size:14px;">
  <tr style="background:#f9fafb;">
    <td style="padding:8px; border:1px solid #e5e7eb; font-weight:600;">知识卡总数</td>
    <td style="padding:8px; border:1px solid #e5e7eb;">{kb_stats.get("Obsidian总计", "N/A")} 张 (Obsidian)</td>
  </tr>
  <tr>
    <td style="padding:8px; border:1px solid #e5e7eb; font-weight:600;">今日投递</td>
    <td style="padding:8px; border:1px solid #e5e7eb;">{job_stats.get("今日投递", "N/A")} 家</td>
  </tr>
  <tr style="background:#f9fafb;">
    <td style="padding:8px; border:1px solid #e5e7eb; font-weight:600;">总投递</td>
    <td style="padding:8px; border:1px solid #e5e7eb;">{job_stats.get("总投递", "N/A")} 家 | 回复 {job_stats.get("有回复", "N/A")} 家</td>
  </tr>
  <tr>
    <td style="padding:8px; border:1px solid #e5e7eb; font-weight:600;">今日新知识卡</td>
    <td style="padding:8px; border:1px solid #e5e7eb;">{len(new_cards)} 张: {", ".join(new_cards[:3]) or "无"}</td>
  </tr>
</table>

<h3 style="color: #10b981; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; margin-top: 24px;">🧠 三项任务评估 + 今日行动</h3>
<div style="background: #f0fdf4; border-left: 4px solid #10b981; padding: 16px; border-radius: 0 8px 8px 0; font-size:14px; line-height:1.7; white-space: pre-wrap;">{assessment}</div>

<hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
<p style="font-size:12px; color:#9ca3af; margin:0;">
📮 回复此邮件可向系统发送指令（功能开发中）<br>
🔧 脚本: ~/scripts/daily_intelligence.py | 日志: ~/job_hunt/daily_intelligence.log
</p>
</div>
</body></html>"""

    text = f"魏伟AI日报 {today}\n\n{assessment}\n\n知识卡: {kb_stats.get('Obsidian总计','N/A')}张 | 总投递: {job_stats.get('总投递','N/A')}家 | 今日新增: {len(new_cards)}张"

    return subject, html, text


def send_email(subject: str, html: str, text: str) -> bool:
    """通过QQ SMTP发送到Gmail"""
    if not SMTP_USER or not SMTP_PASS:
        log("❌ SMTP未配置（SMTP_USER/SMTP_PASS）")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = TO_EMAIL
        msg["Reply-To"] = TO_EMAIL

        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())

        log(f"✅ 邮件已发送 → {TO_EMAIL}")
        return True
    except Exception as e:
        log(f"❌ 发送失败: {e}")
        return False


# ── 主入口 ────────────────────────────────────────────────────

def run(preview: bool = False, test: bool = False):
    log(f"=== 每日情报简报 {'[预览]' if preview else '[发送]'} ===")

    if test:
        # 简单测试邮件
        ok = send_email(
            "🧪 测试邮件 · 魏伟AI系统",
            "<h1>测试成功！</h1><p>QQ→Gmail邮件通道正常。</p>",
            "测试成功！QQ→Gmail邮件通道正常。"
        )
        return ok

    # 采集数据
    log("采集知识库数据...")
    kb_stats = get_kb_stats()
    job_stats = get_job_stats()
    new_cards = get_new_kb_cards(1)
    log(f"  知识卡: {kb_stats.get('Obsidian总计', '?')}张 | 投递: {job_stats.get('总投递', '?')}家")

    # Claude评估
    log("Claude评估三大漏洞...")
    assessment = assess_gaps_with_claude()
    log(f"  评估完成 ({len(assessment)}字)")

    # 生成邮件
    subject, html, text = build_email_html(assessment, kb_stats, job_stats, new_cards)

    if preview:
        print(f"\n{'='*60}")
        print(f"主题: {subject}")
        print(f"{'='*60}")
        print(text)
        print(f"\n[Claude评估]\n{assessment}")
        return True

    # 发送
    return send_email(subject, html, text)


if __name__ == "__main__":
    import argparse
    # 加载环境变量
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import notion_cache as nc
        nc._load_env()
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="每日情报简报")
    parser.add_argument("--send",    action="store_true", help="生成并发送邮件")
    parser.add_argument("--preview", action="store_true", help="预览不发送")
    parser.add_argument("--test",    action="store_true", help="测试邮件发送")
    args = parser.parse_args()

    if args.test:
        run(test=True)
    elif args.send:
        run(preview=False)
    elif args.preview:
        run(preview=True)
    else:
        run(preview=False)  # 默认发送
