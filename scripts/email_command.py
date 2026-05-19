#!/usr/bin/env python3
"""
email_command.py — BG邮件任务系统（魏伟↔AI）
=============================================
规则：
  · 主人邮箱 = QQ (2164898813@qq.com) → 发指令
  · AI邮箱   = QQ收件箱（监听） → 收指令、回复执行结果
  · 指令邮件：主题以 "BG" 开头，后接具体任务描述
  · 附件支持：PDF/图片/文本均可读取分析
  · Token限制：如遇Claude限速，记录到队列，限制解除后自动恢复

指令格式：
  主题: BG 状态          → 系统状态汇报
  主题: BG USB继续       → 继续处理U盘照片
  主题: BG Notion同步    → 同步Notion到本地
  主题: BG 情报          → 立即发今日简报
  主题: BG 任务          → 查看待办清单
  主题: BG [任意任务描述] → Claude分析并执行
  主题: BG 帮助          → 指令清单

用法：
  python3 email_command.py          # 守护模式（每15分钟轮询）
  python3 email_command.py --poll   # 单次轮询
  python3 email_command.py --test   # 发测试确认邮件
"""
import os, sys, json, imaplib, email, smtplib, subprocess, time, sqlite3, tempfile
from pathlib import Path
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header

SCRIPTS_DIR  = Path.home() / "scripts"
LOG_FILE     = Path.home() / "job_hunt" / "email_command.log"
STATE_FILE   = Path.home() / "job_hunt" / "email_command_state.json"
TASK_QUEUE   = Path.home() / "job_hunt" / "bg_task_queue.json"
DRAGON_DB    = Path.home() / "job_hunt" / "dragon_v4.db"
CLAUDE_BIN   = str(Path.home() / ".nvm/versions/node/v22.22.2/bin/claude")

# QQ邮件配置
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
QQ_USER   = os.environ.get("SMTP_USER", "2164898813@qq.com")
QQ_PASS   = os.environ.get("SMTP_PASS", "utzmxdxbakmsecfb")
MASTER_QQ = "2164898813@qq.com"   # 主人QQ邮箱
REPLY_TO  = "wei90wei@gmail.com"  # 回复到Gmail

# 来自主人的邮箱白名单（QQ为主）
ALLOWED_SENDERS = {
    "2164898813@qq.com",
    "wei90wei@gmail.com",
}
BG_PREFIX = "bg"  # 指令邮件前缀（不区分大小写）

# 指令关键词映射
COMMANDS = {
    "状态":    "status",
    "status":  "status",
    "system":  "status",
    "usb继续": "usb",
    "usb":     "usb",
    "u盘":     "usb",
    "照片":    "usb",
    "notion同步": "notion",
    "notion":  "notion",
    "同步":    "notion",
    "情报":    "report",
    "report":  "report",
    "简报":    "report",
    "任务":    "tasks",
    "tasks":   "tasks",
    "待办":    "tasks",
    "帮助":    "help",
    "help":    "help",
    "指令":    "help",
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    # 只写文件（launchd的stdout重定向也指向同一文件，用print会重复）
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)  # 终端交互时可见，launchd模式下忽略


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_uid": 0, "processed": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def decode_str(s) -> str:
    if isinstance(s, str):
        return s
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def extract_attachments(msg) -> list[dict]:
    """提取邮件附件，返回 [{name, content_type, text}]"""
    attachments = []
    for part in msg.walk():
        disposition = part.get("Content-Disposition", "")
        if "attachment" not in disposition and part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        fname = part.get_filename()
        if not fname and ctype == "text/plain":
            continue  # 跳过正文部分

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        fname = decode_str(fname or "unnamed")
        result = {"name": fname, "content_type": ctype, "text": ""}

        if ctype == "text/plain":
            result["text"] = payload.decode("utf-8", errors="replace")[:3000]
        elif ctype in ("application/pdf",):
            # PDF: 保存临时文件再用pdftotext或strings提取
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tf.write(payload)
                tf_path = tf.name
            try:
                r = subprocess.run(["strings", tf_path], capture_output=True, text=True, timeout=10)
                result["text"] = r.stdout[:3000]
            except Exception:
                result["text"] = "[PDF无法解析]"
            finally:
                Path(tf_path).unlink(missing_ok=True)
        elif ctype.startswith("image/"):
            # 图片: 用Vision OCR
            vision_bin = Path.home() / "scripts" / "vision_ocr"
            if vision_bin.exists():
                suffix = "." + ctype.split("/")[1]
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(payload)
                    tf_path = tf.name
                try:
                    r = subprocess.run([str(vision_bin), tf_path], capture_output=True, text=True, timeout=15)
                    result["text"] = r.stdout.strip()[:3000]
                except Exception:
                    result["text"] = "[图片OCR失败]"
                finally:
                    Path(tf_path).unlink(missing_ok=True)

        if result["text"]:
            attachments.append(result)
            log(f"  附件: {fname} ({ctype}) → {len(result['text'])}字")

    return attachments


def parse_bg_command(subject: str, body: str, attachments: list) -> tuple[str, str]:
    """
    解析BG指令邮件
    返回 (cmd_type, full_context)
    cmd_type: status/usb/notion/report/tasks/help/claude_task
    full_context: 完整任务上下文（含附件内容）
    """
    # 去掉BG前缀，获取指令正文
    task_text = subject.strip()
    for prefix in ("BG ", "bg ", "Bg ", "BG:", "bg:"):
        if task_text.startswith(prefix):
            task_text = task_text[len(prefix):].strip()
            break

    # 构建完整上下文
    context_parts = [f"指令: {task_text}"]
    if body.strip():
        context_parts.append(f"正文:\n{body[:2000]}")
    for att in attachments:
        context_parts.append(f"附件[{att['name']}]:\n{att['text']}")
    full_context = "\n\n".join(context_parts)

    # 匹配预定义指令
    task_lower = task_text.lower()
    for keyword, cmd in COMMANDS.items():
        if keyword in task_lower:
            return cmd, full_context

    # 非预定义→交给Claude执行
    return "claude_task", full_context


def cmd_claude_task(context: str) -> tuple[str, str]:
    """用Claude执行自由任务"""
    prompt = f"""你是魏伟的AI助手（运行在他的Mac上）。主人通过邮件给你发来了一个任务指令，请认真完成。

{context}

请：
1. 理解任务目标和边界
2. 执行任务（如需运行系统命令，说明你会做什么）
3. 给出具体可行动的结果或建议

用中文回复，简洁精炼（500字以内）。"""

    clean_env = {k: v for k, v in os.environ.items()
                 if k != "ANTHROPIC_API_KEY" or not v.startswith("sk-placeholder")}
    clean_env["HOME"] = str(Path.home())

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=120,
            env=clean_env, stdin=subprocess.DEVNULL
        )
        text = result.stdout.strip() or f"Claude无输出: {result.stderr[:200]}"
        return text, f"<pre>{text}</pre>"
    except subprocess.TimeoutExpired:
        # Token限制或超时 → 加入队列
        queue_task(context)
        text = "⏳ Claude正忙（可能是token限制），任务已加入队列，限制解除后自动执行"
        return text, text


def queue_task(context: str):
    """将任务加入队列（token限制时使用）"""
    queue = []
    if TASK_QUEUE.exists():
        try:
            queue = json.loads(TASK_QUEUE.read_text())
        except Exception:
            pass
    queue.append({"context": context, "queued_at": datetime.now().isoformat()})
    TASK_QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    log(f"  任务已加入队列，当前队列: {len(queue)} 条")


def process_queue():
    """处理积压队列中的任务（每次轮询时尝试）"""
    if not TASK_QUEUE.exists():
        return
    queue = json.loads(TASK_QUEUE.read_text())
    if not queue:
        return

    log(f"处理队列: {len(queue)} 条待执行任务")
    remaining = []
    for item in queue:
        text, _ = cmd_claude_task(item["context"])
        if "加入队列" in text:  # 仍然限速
            remaining.append(item)
        else:
            # 成功执行，发送结果
            send_reply(REPLY_TO, f"✅ 队列任务完成 · {datetime.now().strftime('%H:%M')}",
                       text, f"<pre>{text}</pre>")

    TASK_QUEUE.write_text(json.dumps(remaining, ensure_ascii=False, indent=2))
    if remaining:
        log(f"  仍有 {len(remaining)} 条任务等待token解除限制")


def send_reply(to_addr: str, subject: str, body_text: str, body_html: str = None):
    """发送回复邮件"""
    if not QQ_USER or not QQ_PASS:
        log("❌ QQ SMTP未配置")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = QQ_USER
        msg["To"]      = to_addr
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.login(QQ_USER, QQ_PASS)
            srv.sendmail(QQ_USER, [to_addr], msg.as_string())
        log(f"✅ 回复已发送 → {to_addr}: {subject}")
        return True
    except Exception as e:
        log(f"❌ 发送失败: {e}")
        return False


# ── 任务执行 ──────────────────────────────────────────────────

def cmd_status() -> tuple[str, str]:
    """系统状态汇报"""
    # 知识库
    obs_dir = Path.home() / "Documents" / "Obsidian Vault"
    kb_dir  = Path.home() / "Engineer_KB" / "02_Knowledge"
    obs_count = sum(1 for _ in obs_dir.rglob("*.md") if ".obsidian" not in str(_))
    usb_count = sum(1 for _ in (kb_dir / "USB_Photos").glob("*.md")) if (kb_dir / "USB_Photos").exists() else 0

    # USB进度
    done_file = Path.home() / "job_hunt" / "usb_photo_done.json"
    done_count = len(json.loads(done_file.read_text())) if done_file.exists() else 0
    total_heic = sum(1 for _ in Path("/Volumes/DEEPINOS").rglob("*.HEIC")
                     if not _.name.startswith("._")) if Path("/Volumes/DEEPINOS").exists() else 0

    # Notion缓存
    notion_md = sum(1 for _ in Path.home().joinpath(".cache/notion_cache").rglob("*.md")) if Path.home().joinpath(".cache/notion_cache").exists() else 0

    # Dragon DB
    job_info = "Dragon DB不存在"
    if DRAGON_DB.exists():
        try:
            conn = sqlite3.connect(DRAGON_DB)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM applications")
            total = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM applications WHERE date(applied_at) = '{date.today()}'")
            today = cur.fetchone()[0]
            conn.close()
            job_info = f"总投递{total}家，今日{today}家"
        except Exception as e:
            job_info = f"查询失败: {e}"

    # caffeinate
    import subprocess
    cafe = subprocess.run(["pgrep", "-x", "caffeinate"], capture_output=True).returncode == 0

    text = f"""🤖 魏伟AI系统状态 · {datetime.now().strftime('%Y-%m-%d %H:%M')}

📚 知识库
  Obsidian总卡片: {obs_count} 张
  USB知识卡: {usb_count} 张
  Notion本地缓存: {notion_md} 个MD

📸 USB照片处理
  已处理: {done_count} 张
  U盘剩余: {total_heic - done_count} 张
  U盘已挂载: {'是' if Path('/Volumes/DEEPINOS').exists() else '否'}

💼 求职数据
  {job_info}

🔋 系统
  防睡眠(caffeinate): {'已启动✅' if cafe else '未运行⚠️'}
  时间: {datetime.now().strftime('%H:%M:%S')}
"""
    html = f"<pre style='font-family:monospace'>{text}</pre>"
    return text, html


def cmd_usb() -> tuple[str, str]:
    """继续处理USB照片"""
    if not Path("/Volumes/DEEPINOS").exists():
        text = "❌ U盘未挂载，请插入U盘后重试"
        return text, text
    log("执行USB照片处理...")
    result = subprocess.run(
        ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR / "usb_photo_to_kb.py"),
         "--run", "--limit", "200"],
        capture_output=True, text=True, timeout=1800,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "HOME": str(Path.home())}
    )
    output = result.stdout[-2000:] if result.stdout else result.stderr[-500:]
    text = f"USB照片处理完成\n\n{output}"
    return text, f"<pre>{text}</pre>"


def cmd_notion() -> tuple[str, str]:
    """同步Notion到本地"""
    log("执行Notion同步...")
    result = subprocess.run(
        ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR / "notion_cache.py")],
        capture_output=True, text=True, timeout=300,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "HOME": str(Path.home())}
    )
    output = result.stdout[-2000:] if result.stdout else result.stderr[-500:]
    # 统计结果
    notion_md = sum(1 for _ in Path.home().joinpath(".cache/notion_cache").rglob("*.md"))
    text = f"Notion同步完成 · 本地缓存: {notion_md} 个MD\n\n{output}"
    return text, f"<pre>{text}</pre>"


def cmd_report() -> tuple[str, str]:
    """触发发送今日情报简报"""
    log("触发今日情报简报...")
    result = subprocess.run(
        ["/opt/homebrew/bin/python3", str(SCRIPTS_DIR / "daily_intelligence.py"), "--send"],
        capture_output=True, text=True, timeout=120,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "HOME": str(Path.home())}
    )
    text = f"情报简报已发送\n{result.stdout[-500:]}"
    return text, f"<pre>{text}</pre>"


def cmd_tasks() -> tuple[str, str]:
    """当前待办任务"""
    done_file = Path.home() / "job_hunt" / "usb_photo_done.json"
    done_count = len(json.loads(done_file.read_text())) if done_file.exists() else 0
    total_heic = sum(1 for _ in Path("/Volumes/DEEPINOS").rglob("*.HEIC")
                     if not _.name.startswith("._")) if Path("/Volumes/DEEPINOS").exists() else 0
    notion_md = sum(1 for _ in Path.home().joinpath(".cache/notion_cache").rglob("*.md"))

    text = f"""📋 当前待办任务

🔴 紧急
  1. USB照片处理: 已处理{done_count}张，剩余约{max(0, total_heic-done_count)}张
     → 指令: 发邮件写「USB继续」

🟡 进行中
  2. Notion全量同步: 已缓存{notion_md}个MD（目标273个）
     → 指令: 发邮件写「Notion同步」

🟢 已完成
  3. Dragon v4求职系统 ✅
  4. Obsidian结构重组 ✅（201张卡片归档至7个领域）
  5. 每日情报邮件 ✅（每天08:30自动发送）
  6. 20张USB知识卡已上传Notion ✅

💡 常驻任务（永不结束）
  · 知识库漏洞发现与修补
  · 求职机会漏洞发现
  · AI行业进入机会评估
"""
    return text, f"<pre>{text}</pre>"


def cmd_help() -> tuple[str, str]:
    text = f"""🤖 魏伟AI系统 · BG邮件指令规则

发邮件到 {QQ_USER}
主题格式: BG [指令关键词或任务描述]
可带附件: PDF/图片/文本 均会被读取分析

══ 快捷指令 ══
  BG 状态       → 系统状态汇报
  BG USB继续    → 继续处理U盘照片
  BG Notion同步 → 同步Notion到本地
  BG 情报       → 立即发今日简报
  BG 任务       → 待办清单
  BG 帮助       → 显示本帮助

══ 自由任务 ══
  BG [任何任务描述] → Claude理解并执行
  例: BG 分析附件中的JD，评估匹配度
  例: BG 统计本周知识卡新增情况

💡 每15分钟自动检查收件箱
⏳ Token限制时任务自动入队，恢复后继续执行
"""
    return text, f"<pre>{text}</pre>"


COMMAND_HANDLERS = {
    "status":      cmd_status,
    "usb":         cmd_usb,
    "notion":      cmd_notion,
    "report":      cmd_report,
    "tasks":       cmd_tasks,
    "help":        cmd_help,
    "claude_task": cmd_claude_task,
}


def poll_once(state: dict) -> int:
    """轮询一次收件箱，返回处理的邮件数"""
    # 先处理积压队列
    process_queue()

    import socket
    socket.setdefaulttimeout(30)  # 所有网络操作30秒超时，防止IMAP永久挂死

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(QQ_USER, QQ_PASS)
        mail.select("INBOX")

        # 搜索未读邮件
        _, data = mail.search(None, "UNSEEN")
        uids = data[0].split() if data[0] else []
        log(f"发现 {len(uids)} 封未读邮件")

        processed = 0
        for uid in uids:
            uid_int = int(uid)
            if uid_int in state.get("processed", []):
                continue

            _, msg_data = mail.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # 解析发件人
            from_raw = msg.get("From", "")
            from_addr = email.utils.parseaddr(from_raw)[1].lower()

            # 白名单检查
            if from_addr not in ALLOWED_SENDERS:
                log(f"  忽略非白名单邮件: {from_addr}")
                state.setdefault("processed", []).append(uid_int)
                continue

            subject = decode_str(msg.get("Subject", ""))

            # 必须以 BG 开头（不区分大小写）
            if not subject.lower().startswith(BG_PREFIX):
                log(f"  非BG指令邮件，跳过: {subject[:30]}")
                state.setdefault("processed", []).append(uid_int)
                continue

            # 提取正文
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get_filename():
                        try:
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass

            # 提取附件
            attachments = extract_attachments(msg)

            log(f"  BG邮件: [{from_addr}] {subject[:50]} | 附件{len(attachments)}个")

            # 解析指令
            cmd, full_context = parse_bg_command(subject, body[:1000], attachments)
            log(f"  → 指令类型: {cmd}")

            # 执行（claude_task需传context）
            handler = COMMAND_HANDLERS.get(cmd)
            if handler:
                try:
                    if cmd == "claude_task":
                        result_text, result_html = cmd_claude_task(full_context)
                    else:
                        result_text, result_html = handler()
                    reply_subject = f"✅ BG完成: {subject[3:33].strip()} · {datetime.now().strftime('%H:%M')}"
                    send_reply(REPLY_TO, reply_subject, result_text, result_html)
                    processed += 1
                except Exception as e:
                    err = f"执行失败: {cmd} → {e}"
                    log(f"  {err}")
                    send_reply(REPLY_TO, f"❌ BG异常: {subject[:30]}", err)

            state.setdefault("processed", []).append(uid_int)
            # 标记为已读
            mail.store(uid, "+FLAGS", "\\Seen")

        save_state(state)
        return processed

    except imaplib.IMAP4.error as e:
        log(f"IMAP错误: {e}")
        return 0
    except Exception as e:
        log(f"轮询异常: {e}")
        return 0
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def run_daemon(interval: int = 900):
    """持续运行守护进程"""
    log("=== 邮件命令系统启动 (守护模式) ===")
    log(f"轮询间隔: {interval}秒 | QQ账户: {QQ_USER}")

    # 发送启动通知到Gmail
    send_reply(
        REPLY_TO,
        "🤖 BG系统已启动 · 等待你的QQ指令",
        f"""魏伟AI邮件任务系统已启动！

📬 指令规则：
  · 从QQ ({MASTER_QQ}) 发邮件到 {QQ_USER}
  · 主题格式: BG [指令关键词或任务描述]
  · 可带附件（PDF/图片/文本）
  · 结果回复到 {REPLY_TO}

示例：
  BG 状态            → 系统状态汇报
  BG USB继续          → 继续处理U盘照片
  BG 分析这份JD       → 附件中的JD分析
  BG 帮助            → 完整指令清单

每{interval//60}分钟检查一次。Token限制自动排队恢复。
启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    )

    state = load_state()
    while True:
        try:
            n = poll_once(state)
            if n > 0:
                log(f"本次处理 {n} 条指令")
        except Exception as e:
            log(f"守护循环异常: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    import argparse

    # 加载环境变量
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import notion_cache as nc
        nc._load_env()
        # 更新QQ凭证（如果环境变量中有）
        QQ_USER = os.environ.get("SMTP_USER", QQ_USER)
        QQ_PASS = os.environ.get("SMTP_PASS", QQ_PASS)
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="QQ邮件命令交互系统")
    parser.add_argument("--daemon", action="store_true", help="持续运行守护模式")
    parser.add_argument("--test",   action="store_true", help="发送测试邮件")
    parser.add_argument("--poll",   action="store_true", help="单次轮询")
    parser.add_argument("--interval", type=int, default=900, help="轮询间隔秒数(默认900)")
    args = parser.parse_args()

    if args.test:
        ok = send_reply(
            "wei90wei@gmail.com",
            "🧪 邮件命令系统测试",
            "邮件命令系统工作正常！\n\n可用指令：状态 / USB继续 / Notion同步 / 情报 / 任务 / 帮助"
        )
        sys.exit(0 if ok else 1)
    elif args.poll:
        state = load_state()
        n = poll_once(state)
        log(f"轮询完成，处理 {n} 条指令")
    elif args.daemon:
        run_daemon(args.interval)
    else:
        run_daemon(args.interval)
