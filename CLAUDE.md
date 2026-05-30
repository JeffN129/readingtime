# CLAUDE.md — ReadingTime 项目构建指南

你的任务是从零构建 **ReadingTime**，一个运行在 Mac 本地的 AI Agent，
自动维护一个永远包含 10 本 EPUB 书籍的文件夹，并通过用户的文件操作行为学习阅读偏好。

---

## 一、项目目标（理解透彻再动手）

核心行为循环：
- 书架文件夹内**永远保持 10 本 EPUB 书**
- 用户**手动删除/移走**某本书 → 系统判定为「喜欢」，提取其特征，强化同类推荐
- 某本书**在架超过 30 天未被手动删除** → 系统自动删除，判定为「无感」，规避同类
- 任何一本书离开书架（无论原因）→ **立即自动补缺**，从书源搜索并下载一本新书

附加功能：
- 每本书旁边生成一个 `.readingnote.md`（摘要 + 推荐理由）
- 书架根目录有一个 `READING_TIME.md`，每日更新，列出 10 本书的预估剩余时间和阅读进度
- 下载的书籍目标路径支持 iCloud Drive（`~/Library/Mobile Documents/...`），实现 iPhone 自动同步

---

## 二、技术栈（严格遵守）

| 用途 | 库 |
|---|---|
| 语言 | Python 3.11+ |
| CLI 界面 | `click` + `rich` |
| 文件监控 | `watchdog` |
| 数据库 | `sqlite3`（标准库，无需额外安装） |
| EPUB 元数据 | `ebooklib` |
| HTTP 请求 | `httpx`（异步）+ `requests`（同步备用）|
| 定时任务 | `schedule` |
| LLM 调用 | `anthropic`（主）；预留 OpenAI 适配器接口 |
| 配置文件 | `pyyaml` + `python-dotenv` |
| 测试 | `pytest` |

**不要引入 LangChain / LangGraph**，所有 Agent 逻辑用原生 Python + Anthropic SDK 实现，保持代码可控、依赖简洁。

---

## 三、目录结构（按此创建，不要自作主张增减顶层目录）

```
readingtime/
├── CLAUDE.md                  # 本文件
├── README.md                  # 用户文档（最后生成）
├── pyproject.toml             # 项目元数据与依赖（用 uv 或 pip）
├── .env.example               # 环境变量模板
├── config.yaml                # 用户配置（初始化时生成）
│
├── readingtime/               # 主包
│   ├── __init__.py
│   ├── main.py                # CLI 入口（click）
│   ├── config.py              # 配置加载与验证
│   ├── database.py            # 所有 SQLite 操作
│   │
│   ├── monitor/
│   │   ├── __init__.py
│   │   └── watcher.py         # watchdog 文件监控
│   │
│   ├── shelf/
│   │   ├── __init__.py
│   │   ├── manager.py         # 书架核心逻辑（增删补缺）
│   │   └── epub_utils.py      # EPUB 元数据提取与封面处理
│   │
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py            # 抽象基类 BookSource
│   │   ├── gutenberg.py       # Project Gutenberg（默认，需完整实现）
│   │   ├── openlibrary.py     # Open Library（备用，需完整实现）
│   │   └── zlibrary.py        # Z-Library（仅写 stub，留 TODO）
│   │
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── profiler.py        # 用户画像：从行为提取偏好特征
│   │   ├── recommender.py     # 推荐引擎：根据画像生成搜索词 + LLM 评分
│   │   ├── summarizer.py      # 生成摘要和推荐语
│   │   └── prompts.py         # 所有 LLM prompt 模板集中在这里
│   │
│   └── scheduler/
│       ├── __init__.py
│       └── tasks.py           # 定时任务：30 天检查、每日更新 READING_TIME.md
│
└── tests/
    ├── test_database.py
    ├── test_shelf_manager.py
    ├── test_watcher.py
    ├── test_profiler.py
    └── test_sources.py
```

---

## 四、数据库设计（用 SQLite，在 `database.py` 中实现所有操作）

### 表 1：`books`
```sql
CREATE TABLE books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    author          TEXT,
    filename        TEXT NOT NULL UNIQUE,   -- 仅文件名，不含路径
    added_at        DATETIME NOT NULL,
    removed_at      DATETIME,
    removal_type    TEXT,                   -- 'manual' | 'auto_expired' | 'system_init'
    source          TEXT,                   -- 'gutenberg' | 'openlibrary' | 'zlibrary'
    source_id       TEXT,
    language        TEXT DEFAULT 'en',
    tags            TEXT,                   -- JSON 数组字符串，如 '["fiction","mystery"]'
    page_count      INTEGER,
    is_protected    INTEGER DEFAULT 0       -- 1 = 用户正在阅读，延长保护期
);
```

### 表 2：`signals`（行为信号）
```sql
CREATE TABLE signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER REFERENCES books(id),
    signal      TEXT NOT NULL,              -- 'liked' | 'neutral'
    features    TEXT,                       -- JSON：{tags, author, language, ...}
    created_at  DATETIME NOT NULL
);
```

### 表 3：`profile`（用户画像快照，定期更新）
```sql
CREATE TABLE profile (
    id              INTEGER PRIMARY KEY,    -- 永远只有一行，id=1
    liked_tags      TEXT,                   -- JSON 数组，按权重排序
    liked_authors   TEXT,                   -- JSON 数组
    neutral_tags    TEXT,                   -- JSON 数组
    lang_pref       TEXT DEFAULT 'en',
    updated_at      DATETIME
);
```

### 表 4：`system_state`（防止误判）
```sql
CREATE TABLE system_state (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
-- 用于存储 'agent_is_deleting' = 'filename.epub'，
-- 让 watcher 知道当前删除是系统行为而非用户行为
```

---

## 五、核心模块详细规格

### 5.1 `sources/base.py` — 书源抽象接口

```python
from dataclasses import dataclass
from typing import List, Optional
from abc import ABC, abstractmethod

@dataclass
class BookResult:
    source_id: str
    title: str
    author: str
    language: str
    tags: List[str]
    formats: List[str]          # ['epub', 'pdf', ...]
    epub_download_url: Optional[str]
    cover_url: Optional[str]
    page_count: Optional[int]
    description: Optional[str]  # 原始简介，用于 LLM 摘要

class BookSource(ABC):
    @abstractmethod
    def search(self, query: str, language: str = 'en',
               limit: int = 10) -> List[BookResult]:
        """搜索书籍，必须返回有 epub 格式的结果"""
        ...

    @abstractmethod
    def download(self, result: BookResult, save_path: str) -> bool:
        """下载 EPUB 到 save_path，成功返回 True"""
        ...
```

### 5.2 `sources/gutenberg.py` — Gutenberg 实现（完整实现，这是默认书源）

- 使用 Gutenberg 搜索 API：`https://gutendex.com/books/?search={query}&languages={lang}`
- 搜索结果过滤：只保留有 `formats['application/epub+zip']` 的书
- `download()` 直接 GET 该 URL 并写入文件
- 实现限速：每次请求之间 sleep 1 秒
- 实现重试：失败最多重试 3 次，指数退避

### 5.3 `sources/openlibrary.py` — Open Library 实现（备用）

- 搜索 API：`https://openlibrary.org/search.json?q={query}&language={lang}`
- EPUB 下载：通过 `https://archive.org/download/{ia_id}/{ia_id}.epub`
- 注意：Open Library 的 EPUB 覆盖率较低，搜不到就跳过，不要报错

### 5.4 `sources/zlibrary.py` — Z-Library（仅 Stub）

```python
class ZLibrarySource(BookSource):
    """
    TODO: Z-Library 集成（用户尚未配置）

    接入方式选项：
    1. 个人专用域名 + 账号 Cookie（最稳定）
    2. singlelogin.re 个人 API Token
    3. Telegram Bot API

    在用户完成 Z-Library 配置后，在此实现：
    - self.domain  从 config.yaml 读取
    - self.token   从 .env 读取 ZLIBRARY_TOKEN

    当前返回空列表，不影响其他书源正常工作。
    """
    def search(self, query, language='en', limit=10):
        raise NotImplementedError("Z-Library 尚未配置，请运行 readingtime config --zlibrary")

    def download(self, result, save_path):
        raise NotImplementedError("Z-Library 尚未配置")
```

### 5.5 `monitor/watcher.py` — 文件系统监控

关键逻辑：区分「用户删除」vs「系统删除」

```python
# 伪代码，用 watchdog FileSystemEventHandler 实现
class ShelfHandler(FileSystemEventHandler):
    def on_deleted(self, event):
        filename = Path(event.src_path).name
        if not filename.endswith('.epub'):
            return
        
        # 查询 system_state 表，看是否是 agent 自己在删
        agent_deleting = db.get_state('agent_is_deleting')
        if agent_deleting == filename:
            db.clear_state('agent_is_deleting')
            return  # 系统行为，忽略，已在 manager 里处理
        
        # 用户行为
        shelf_manager.handle_user_removal(filename)
```

系统删除书时，**必须先写入 system_state**，再执行删除：
```python
def system_delete_book(self, filename):
    db.set_state('agent_is_deleting', filename)
    os.remove(shelf_path / filename)
    # watcher 收到事件后看到 flag，知道是系统行为
```

### 5.6 `shelf/manager.py` — 书架管理器（最核心）

实现以下方法：
- `get_current_books() -> List[dict]`：返回当前书架状态
- `handle_user_removal(filename)`：记录 liked 信号 → 触发补缺
- `handle_auto_expiry(filename)`：记录 neutral 信号 → 触发补缺
- `refill(n=1)`：补缺 n 本书（调用推荐引擎 → 搜索 → 下载）
- `check_expirations()`：检查是否有超过 30 天的书，执行 auto_expiry
- `initialize_shelf()`：首次运行时填满 10 本书（不走推荐引擎，直接热门书）

补缺流程（`refill` 的内部步骤）：
1. 调用 `agent.profiler.get_profile()` 获取当前画像
2. 调用 `agent.recommender.generate_queries(profile)` 生成 3 条搜索词
3. 对每条搜索词，按 source_priority 依次调用书源搜索，汇总候选（去重，排除已在架/历史过的书）
4. 如果候选 >= 3 本，调用 `agent.recommender.score_candidates(candidates, profile)` 用 LLM 打分
5. 取评分最高的书下载
6. 下载成功后：调用 `agent.summarizer.generate_note(book)` 生成 `.readingnote.md`
7. 写入数据库，记录 `added_at`

### 5.7 `agent/profiler.py` — 用户画像

- `extract_features(book: dict) -> dict`：从书籍信息提取特征（tags, author, language, era 等）
- `update_profile(signal: str, features: dict)`：更新 profile 表
  - liked：对应 tag/author 权重 +1
  - neutral：对应 tag/author 权重 -0.5（不是硬性排除）
- `get_profile() -> dict`：返回当前画像，格式为：
  ```json
  {
    "liked_tags": ["mystery", "philosophy", "classic"],
    "liked_authors": ["Dostoevsky", "Borges"],
    "neutral_tags": ["romance"],
    "lang_pref": "en"
  }
  ```

### 5.8 `agent/recommender.py` — 推荐引擎

- `generate_queries(profile: dict) -> List[str]`：
  调用 LLM（使用 `prompts.QUERY_GENERATION_PROMPT`），根据画像生成 3~5 条搜索词
  例：画像喜欢 mystery + Dostoevsky → 生成 `["classic Russian literature", "psychological thriller 19th century", "existential fiction"]`

- `score_candidates(candidates: List[BookResult], profile: dict) -> List[tuple]`：
  调用 LLM（使用 `prompts.SCORING_PROMPT`），批量评分，返回 `[(book, score), ...]`
  评分 1~10，LLM 需返回 JSON 格式

### 5.9 `agent/summarizer.py` — 摘要生成器

- `generate_note(book: BookResult, epub_path: str) -> str`：
  1. 用 `ebooklib` 提取 EPUB 内文前 2000 字（作为 context）
  2. 调用 LLM（`prompts.SUMMARY_PROMPT`），生成：
     - 300 字摘要（中文或英文，跟随书籍语言）
     - 一句「为什么你会喜欢这本书」（中文）
  3. 写入 `{epub_path}.readingnote.md`，格式如下：

```markdown
# {书名}

**作者**：{author}  
**语言**：{language}  
**预计阅读时间**：约 {hours} 小时  
**加入书架**：{added_at}  
**书源**：{source}  

---

## 摘要

{300 字摘要}

---

## 为什么你会喜欢这本书

> {一句推荐语}
```

### 5.10 `agent/prompts.py` — 所有 Prompt 集中管理

在此文件中定义所有 prompt 为字符串常量，禁止在其他文件内写 prompt：

- `QUERY_GENERATION_PROMPT`
- `SCORING_PROMPT`
- `SUMMARY_PROMPT`

每个 prompt 都要包含明确的输出格式要求（JSON 或纯文本），评分 prompt 要求 LLM 仅返回 JSON。

### 5.11 `scheduler/tasks.py` — 定时任务

使用 `schedule` 库：
- **每天凌晨 2 点**：调用 `shelf_manager.check_expirations()` 检查并淘汰超龄书
- **每天凌晨 2 点 10 分**：重新生成 `READING_TIME.md`
- **每 30 分钟**：检查书架数量是否等于 10，不足则触发 `refill()`（防止意外情况）

---

## 六、CLI 命令规格（`main.py` 用 click 实现）

```
readingtime init           首次初始化：创建 config.yaml、初始化数据库、填满书架
readingtime start          启动 Agent 守护进程（watcher + scheduler 同时运行）
readingtime stop           停止守护进程
readingtime status         打印当前书架状态表格（用 rich.Table）
readingtime profile        打印用户画像摘要
readingtime refill         立即触发补缺（调试用）
readingtime check          立即检查超龄书（调试用）
readingtime add "书名"     手动搜索并添加一本书到书架
readingtime history        查看历史书架记录
```

`readingtime start` 的守护进程行为：
- 用 `daemon=True` 的子线程跑 scheduler
- 主线程跑 watchdog observer
- 捕获 SIGTERM/SIGINT 优雅退出，写入 `~/.readingtime/daemon.pid`

---

## 七、配置文件规格

`config.yaml`（由 `readingtime init` 生成，用户可手动编辑）：
```yaml
shelf:
  path: "~/Books/ReadingTime"   # 支持 iCloud 路径
  size: 10
  book_lifetime_days: 30
  language: "en"                # 书籍语言偏好

llm:
  provider: "claude"
  model: "claude-sonnet-4-5"
  max_tokens: 1000

sources:
  priority:
    - gutenberg
    - openlibrary
    - zlibrary
  zlibrary:
    enabled: false
    domain: ""

logging:
  level: "INFO"
  file: "~/.readingtime/logs/agent.log"
```

`.env`（不提交到 git）：
```
ANTHROPIC_API_KEY=sk-...
ZLIBRARY_TOKEN=           # 暂时留空
```

---

## 八、`READING_TIME.md` 格式

每日由 scheduler 更新，放在书架根目录：

```markdown
# 📚 ReadingTime 书架 · 2025-01-15 更新

| # | 书名 | 作者 | 语言 | 预计阅读时长 | 入架天数 | 剩余天数 |
|---|------|------|------|------------|---------|---------|
| 1 | The Trial | Kafka | EN | 约 6 小时 | 3 天 | 27 天 |
| 2 | ... | ... | ... | ... | ... | ... |

---
*手动移走一本书 = 告诉系统你喜欢它，会推荐更多类似书籍*  
*30 天内未移走的书会被自动替换*
```

预计阅读时长计算：`page_count / 250 * 60` 分钟（250页/小时为默认阅读速度）。

---

## 九、错误处理规范

- **所有书源下载失败**：记录错误日志，尝试下一候选书，不崩溃
- **LLM 调用失败**：降级处理——跳过摘要生成，仍然完成下载；记录 warning
- **LLM 返回非 JSON**：用正则从响应中提取 JSON，失败则打印 warning 并跳过评分步骤（直接用第一个搜索结果）
- **iCloud 路径无写权限**：在 `init` 阶段检测，提前报错提示用户
- **文件被锁定（正在阅读）**：捕获 `PermissionError`，自动将该书的保护期延长 7 天，写入日志

---

## 十、实现顺序（严格按此顺序，每步完成后在 commit 信息中注明）

```
Step 1  项目脚手架：pyproject.toml、目录结构、__init__.py、.env.example
Step 2  config.py：加载 config.yaml + .env，提供全局 Config 单例
Step 3  database.py：建表、CRUD 操作、所有 SQL 封装完毕
Step 4  sources/base.py + sources/gutenberg.py：能搜索和下载 EPUB
Step 5  sources/openlibrary.py：备用书源
Step 6  sources/zlibrary.py：stub，加清晰 TODO 注释
Step 7  shelf/epub_utils.py：EPUB 元数据提取、封面提取
Step 8  shelf/manager.py：书架核心逻辑，先实现不依赖 LLM 的部分
Step 9  monitor/watcher.py：watchdog 监控 + 系统/用户删除区分
Step 10 agent/prompts.py + agent/profiler.py
Step 11 agent/recommender.py + agent/summarizer.py（LLM 接入）
Step 12 scheduler/tasks.py：定时任务
Step 13 main.py：CLI 命令全部接通
Step 14 tests/：核心逻辑单元测试
Step 15 README.md：用户文档
```

---

## 十一、必须遵守的约束

1. **Z-Library 只写 stub**，不要尝试实现或猜测其 API，等待用户后续提供接入方式
2. **所有 prompt 集中在 `prompts.py`**，不要分散
3. **不使用 LangChain**，所有 Agent 逻辑原生实现
4. **每个文件顶部写清楚该模块的职责注释**
5. **配置不硬编码**，所有路径/参数从 `config.yaml` 或 `.env` 读取
6. **LLM 调用失败不崩溃**，必须有降级逻辑
7. **每一步完成后询问我是否继续下一步**，不要一次性把所有代码全写完

---

## 十二、开始前的确认步骤

在写任何代码之前，先执行以下操作：

1. 运行 `python --version` 确认 Python >= 3.11
2. 运行 `pip show anthropic` 确认已安装 Anthropic SDK
3. 检查 `.env` 文件是否存在 `ANTHROPIC_API_KEY`
4. 如果以上任一不满足，**暂停并告知用户**，不要继续

确认完毕后，从 **Step 1** 开始，完成后向我汇报并等待指令。
