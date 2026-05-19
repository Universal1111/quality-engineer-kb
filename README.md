# Quality Engineer Knowledge Base

魏伟的工程知识库 + AI任务中转系统

## 📂 结构
- `scripts/` — 自动化脚本（USB处理/Notion同步/情报系统）
- `reports/` — 每日情报简报存档
- `tasks/` — 任务执行记录
- `docs/` — 知识库文档

## 🤖 GitHub Issues 任务中转
在 Issues 中创建任务，Mac 上的 AI 系统会自动执行并回复结果。

**使用方法：** 创建 Issue，标题加 `[BG]` 前缀，正文写任务描述。

### 支持指令
| 标题 | 功能 |
|------|------|
| `[BG] 状态` | 系统状态汇报 |
| `[BG] USB继续` | 继续处理U盘照片 |
| `[BG] Notion同步` | 同步Notion到本地 |
| `[BG] 情报` | 立即发今日简报 |
| `[BG] 任务` | 查看待办清单 |
| `[BG] [任意任务]` | Claude执行自由任务 |

## 🔗 关联系统
- Notion: Battery Knowledge DB
- 本地: `~/Engineer_KB/` + `~/Documents/Obsidian Vault/`
- 邮件: QQ→Gmail 双通道
