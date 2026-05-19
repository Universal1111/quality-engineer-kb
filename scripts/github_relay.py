#!/usr/bin/env python3
"""
github_relay.py — GitHub Issues 任务中转系统
=============================================
比 QQ IMAP 更可靠：HTTP 轮询，无长连接，不会卡死。

规则：
  · 主人在 GitHub Issues 创建标题含 [BG] 的 issue
  · 本系统每5分钟轮询，识别新 issue 并执行
  · 执行结果作为 comment 回复到 issue，并关闭 issue
  · 同时发送结果到 Gmail

用法：
  python3 github_relay.py          # 守护模式（每5分钟）
  python3 github_relay.py --poll   # 单次轮询
  python3 github_relay.py --test   # 创建测试 issue 验证
"""
import os, sys, json, subprocess, time, smtplib, sqlite3
from pathlib import Path
from datetime import datetime, date
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SCRIPTS_DIR = Path.home() / "scripts"
LOG_FILE    = Path.home() / "job_hunt" / "github_relay.log"
STATE_FILE  = Path.home() / "job_hunt" / "github_relay_state.json"
DRAGON_DB   = Path.home() / "job_hunt" / "dragon_v4.db"
CLAUDE_BIN  = str(Path.home() / ".nvm/versions/node/v22.22.2/bin/claude")

GITHUB_REPO  = "Universal1111/quality-engineer-kb"
GITHUB_OWNER = "Universal1111"
BG_LABEL     = "bg-task"
BG_PREFIX    = "[bg]"

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
QQ_USER   = os.environ.get("SMTP_USER", "2164898813@qq.com")
QQ_PASS   = os.environ.get("SMTP_PASS", "utzmxdxbakmsecfb")
REPLY_TO  = "wei90wei@gmail.com"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def gh_headers() -> dict:
    token = os.environ.get("GH_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_issues": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send_email(subject: str, body: str):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = QQ_USER
        msg["To"]      = REPLY_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.login(QQ_USER, QQ_PASS)
            srv.sendmail(QQ_USER, [REPLY_TO], msg.as_string())
        log(f"✅ 邮件已发 → {REPLY_TO}: {subject[:40]}")
    except Exception as e:
        log(f"❌ 邮件发送失败: {e}")


def comment_issue(issue_number: int, body: str):
    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
        headers=gh_headers(),
        json={"body": body},
        timeout=15,
    )
    return r.status_code == 201


def close_issue(issue_number: int):
    requests.patch(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}",
        headers=gh_headers(),
        json={"state": "closed"},
        timeout=15,
    )


def ensure_label():
    """确保 bg-task label 存在"""
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/labels/{BG_LABEL}",
        headers=gh_headers(), timeout=10
    )
    if r.status_code == 404:
        requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/labels",
            headers=gh_headers(),
            json={"name": BG_LABEL, "color": "0075ca", "description": "BG后台任务指令"},
            timeout=10,
        )


# ── 指令执行（复用 email_command.py 的逻辑）────────────────────

def get_system_status() -> str:
    obs_dir  = Path.home() / "Documents" / "Obsidian Vault"
    kb_dir   = Path.home() / "Engineer_KB" / "02_Knowledge"
    obs_cnt  = sum(1 for _ in obs_dir.rglob("*.md") if ".obsidian" not in str(_))
    usb_cnt  = sum(1 for _ in (kb_dir / "USB_Photos").glob("*.md")) if (kb_dir/"USB_Photos").exists() else 0
    done_cnt = len(json.loads((Path.home()/".cache/usb_photo_kb_done.json").read_text())) \
               if (Path.home()/".cache/usb_photo_kb_done.json").exists() else 0
    notion_md = sum(1 for _ in (Path.home()/".cache/notion_cache").rglob("*.md"))
    cafe = subprocess.run(["pgrep","-x","caffeinate"], capture_output=True).returncode == 0

    job_info = "Dragon DB不存在"
    if DRAGON_DB.exists():
        try:
            conn = sqlite3.connect(DRAGON_DB)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM applications")
            total = cur.fetchone()[0]
            conn.close()
            job_info = f"总投递{total}家"
        except Exception as e:
            job_info = str(e)

    return f"""🤖 系统状态 · {datetime.now().strftime('%Y-%m-%d %H:%M')}

📚 知识库
  Obsidian: {obs_cnt} 张 | USB知识卡: {usb_cnt} 张
  Notion缓存: {notion_md} 个MD
  USB照片已处理: {done_cnt}/728

💼 {job_info}
🔋 防睡眠: {'✅' if cafe else '⚠️未运行'}
📍 GitHub: {GITHUB_REPO}"""


COMMAND_MAP = {
    "状态": "status", "status": "status",
    "usb": "usb", "usb继续": "usb", "照片": "usb",
    "notion": "notion", "notion同步": "notion",
    "情报": "report", "report": "report",
    "任务": "tasks", "tasks": "tasks",
}


def execute_command(title: str, body: str) -> str:
    task = title.lower()
    for prefix in ("[bg]", "[bg] "):
        if task.startswith(prefix):
            task = task[len(prefix):].strip()

    # 匹配预定义指令
    cmd = None
    for kw, c in COMMAND_MAP.items():
        if kw in task:
            cmd = c
            break

    if cmd == "status":
        return get_system_status()

    if cmd == "usb":
        if not Path("/Volumes/DEEPINOS").exists():
            return "❌ U盘未挂载，请插入后重试"
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR/"usb_photo_to_kb.py"), "--run", "--limit", "200"],
            capture_output=True, text=True, timeout=1800, cwd=str(SCRIPTS_DIR),
            env={**os.environ, "HOME": str(Path.home())}
        )
        return f"USB处理完成\n{result.stdout[-1500:]}"

    if cmd == "notion":
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR/"notion_cache.py")],
            capture_output=True, text=True, timeout=300, cwd=str(SCRIPTS_DIR),
            env={**os.environ, "HOME": str(Path.home())}
        )
        notion_md = sum(1 for _ in (Path.home()/".cache/notion_cache").rglob("*.md"))
        return f"Notion同步完成 · 本地{notion_md}个MD\n{result.stdout[-800:]}"

    if cmd == "report":
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR/"daily_intelligence.py"), "--send"],
            capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS_DIR),
            env={**os.environ, "HOME": str(Path.home())}
        )
        return f"情报简报已发送到Gmail\n{result.stdout[-300:]}"

    if cmd == "tasks":
        usb_done = len(json.loads((Path.home()/".cache/usb_photo_kb_done.json").read_text())) \
                   if (Path.home()/".cache/usb_photo_kb_done.json").exists() else 0
        return f"""📋 待办任务
① USB照片: 已处理{usb_done}/728张
② Notion扫描查重格式修复（待Notion授权后执行）
③ 三项永久任务持续优化中"""

    # 自由任务 → Claude
    context = f"任务: {title}\n正文: {body[:1500]}" if body.strip() else f"任务: {title}"
    clean_env = {k: v for k, v in os.environ.items()
                 if k != "ANTHROPIC_API_KEY" or not v.startswith("sk-placeholder")}
    clean_env["HOME"] = str(Path.home())
    try:
        prompt = f"""你是魏伟的AI助手，运行在他的Mac上。通过GitHub Issues收到任务指令：

{context}

请完成任务，中文回复，500字以内。"""
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=90,
            env=clean_env, stdin=subprocess.DEVNULL
        )
        return result.stdout.strip() or f"Claude无输出: {result.stderr[:100]}"
    except Exception as e:
        return f"执行异常: {e}"


# ── 主轮询 ─────────────────────────────────────────────────────

def poll_once(state: dict) -> int:
    import socket
    socket.setdefaulttimeout(20)

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        log("❌ GH_TOKEN未设置")
        return 0

    try:
        # 获取 open issues
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers=gh_headers(),
            params={"state": "open", "per_page": 20, "sort": "created", "direction": "asc"},
            timeout=15,
        )
        if r.status_code != 200:
            log(f"GitHub API错误: {r.status_code} {r.text[:100]}")
            return 0

        issues = r.json()
        bg_issues = [i for i in issues if i["title"].lower().startswith("[bg]")]
        if bg_issues:
            log(f"发现 {len(bg_issues)} 个BG任务")

        processed = 0
        for issue in bg_issues:
            iid = issue["number"]
            if iid in state.get("processed_issues", []):
                continue

            title = issue["title"]
            body  = issue.get("body") or ""
            log(f"  执行 Issue #{iid}: {title}")

            result = execute_command(title, body)

            # 回复 + 关闭
            comment_body = f"✅ 执行完成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n```\n{result}\n```"
            comment_issue(iid, comment_body)
            close_issue(iid)

            # 同时发邮件
            send_email(
                f"✅ GitHub任务完成: {title[:40]}",
                f"Issue #{iid}: {title}\n\n{result}"
            )

            state.setdefault("processed_issues", []).append(iid)
            processed += 1
            log(f"  ✅ Issue #{iid} 完成并关闭")

        save_state(state)
        return processed

    except Exception as e:
        log(f"轮询异常: {e}")
        return 0


def run_daemon(interval: int = 300):
    log("=== GitHub Issues 中转系统启动 ===")
    log(f"仓库: {GITHUB_REPO} | 轮询间隔: {interval}秒")
    ensure_label()

    # 启动通知
    send_email(
        "🤖 GitHub Issues中转系统已启动",
        f"""GitHub Issues 任务中转已激活！

仓库: https://github.com/{GITHUB_REPO}
使用方法: 在 Issues 创建标题含 [BG] 的任务

示例标题:
  [BG] 状态        → 系统状态汇报
  [BG] 情报        → 立即发今日简报
  [BG] USB继续     → 继续处理U盘照片
  [BG] [任意任务]  → Claude执行

每{interval//60}分钟自动轮询，结果回复到 issue + 发Gmail。
启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    )

    state = load_state()
    while True:
        try:
            n = poll_once(state)
            if n:
                log(f"本次处理 {n} 个任务")
        except Exception as e:
            log(f"守护循环异常: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    import argparse

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import notion_cache as nc
        nc._load_env()
        QQ_USER = os.environ.get("SMTP_USER", QQ_USER)
        QQ_PASS = os.environ.get("SMTP_PASS", QQ_PASS)
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--poll",   action="store_true")
    parser.add_argument("--test",   action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    if args.test:
        log("创建测试Issue...")
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers=gh_headers(),
            json={"title": "[BG] 状态", "body": "测试GitHub Issues中转系统"},
            timeout=15,
        )
        log(f"测试Issue创建: {r.status_code} → #{r.json().get('number')}")
    elif args.poll:
        state = load_state()
        poll_once(state)
    else:
        run_daemon(args.interval)
