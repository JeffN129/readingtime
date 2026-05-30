# 📚 ReadingTime

AI 自动书架 — 维护 10 本电子书，从你的删除行为学习阅读偏好。

## 🚀 快速开始

### 1. 安装

```bash
pip install -e .
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入你的 DeepSeek API Key：

```env
DEEPSEEK_API_KEY=sk-你的key
```

> 免费注册：https://platform.deepseek.com/api_keys

### 3. 初始化书架

```bash
readingtime init
```

首次运行会自动下载 10 本书到桌面「书架」文件夹。

### 4. 启动后台监控

```bash
readingtime start
```

Agent 会在后台运行，实时监控你的文件操作。

## 📖 命令参考

| 命令 | 说明 |
|------|------|
| `readingtime init` | 首次初始化 |
| `readingtime start` | 启动后台守护 |
| `readingtime stop` | 停止守护 |
| `readingtime status` | 查看书架 |
| `readingtime add "书名"` | 搜索并添加一本书 |
| `readingtime refill -n 5` | 补 5 本书 |
| `readingtime profile` | 查看阅读偏好画像 |

## 🧠 工作原理

- **手动删除一本书** → 系统判定你喜欢它，推荐同类
- **30 天未删除** → 系统自动淘汰
- **书架不足 10 本** → 自动补缺

每本书附带 AI 生成的阅读笔记（`.readingnote.md`）。

## ⚙️ 配置

编辑 `config.yaml`：

```yaml
shelf:
  path: "E:/Desktop/书架"   # 书架路径
  size: 10                   # 维持数量
  book_lifetime_days: 30     # 淘汰天数
  language: "zh"             # 语言偏好
```

## 🤖 更换 AI 服务商

默认 **DeepSeek**。支持四家，`.env` 填对应 Key，`config.yaml` 改 `base_url` 和 `model` 即可。

### DeepSeek（默认）

注册：https://platform.deepseek.com/api_keys

```env
DEEPSEEK_API_KEY=sk-你的key
```

### OpenAI

```env
OPENAI_API_KEY=sk-你的key
```
```yaml
llm:
  model: "gpt-4o-mini"
  base_url: "https://api.openai.com/v1"
```

### Anthropic

```env
ANTHROPIC_API_KEY=sk-ant-你的key
```
```yaml
llm:
  model: "claude-sonnet-4-5"
  base_url: "https://api.anthropic.com/v1"
```

### Gemini

```env
GEMINI_API_KEY=你的key
```
```yaml
llm:
  model: "gemini-2.5-flash"
  base_url: "https://generativelanguage.googleapis.com/v1beta/openai"
```

## 🖥️ 开机自启（Windows）

将 `start_readingtime.vbs` 复制到启动文件夹：

```
Win+R → shell:startup → 粘贴进去
```

## 🛑 停止与删除

**停止运行：**
```bash
readingtime stop
```
或在任务管理器中结束 `python.exe` 进程。

**取消开机自启：**
删除启动文件夹中的 `start_readingtime.vbs`：
```
Win+R → shell:startup → 删除 start_readingtime.vbs
```

**彻底删除项目：**
```bash
pip uninstall readingtime
# 然后删除项目文件夹和书架文件夹
```

书架文件夹位置见 `config.yaml` 中的 `shelf.path`，默认在桌面「书架」。

## 📝 书源

当前使用 **苦瓜书盘 (kgbook.com)** — 中文电子书，无需代理，直链下载。

> 作者正在寻找更多优质中文书源，欢迎在 GitHub Issues 推荐！

## 📄 License

MIT
