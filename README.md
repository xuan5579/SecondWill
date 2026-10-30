# 每日规划助手 · AI 执行教练

> 从 Tkinter 桌面版重制为 Web 全栈版。定位不是待办清单，而是**拖延干预系统**：
> 分析执行数据 → 检测冲突 → 给出重排建议 → 主动干预 → 用宠物状态反馈执行力。

架构设计见 [`docs/01-架构设计.md`](docs/01-架构设计.md)。

---

## ⚠️ 范围声明：v1.0 单用户、无鉴权

这是**有意的范围决策，不是安全疏漏**。v1.0 面向单人单机演示，引入登录流程只会增加与核心叙事无关的工作量。

代价与边界：

- 数据存放在本地 SQLite 文件 `data/app.db`，**没有任何访问控制**；
- 任何能访问本机该进程的人都能读写全部数据；
- 若要公开部署，必须先补：身份体系、按 `user_id` 的数据隔离、API 鉴权中间件。

代码中因此不存在 `models/user.py` 与 `core/security.py`，接口契约中不含 `user_id`。

---

## 当前进度

| 分期 | 主题 | 状态 |
|---|---|---|
| P1 | 数据底座与迁移 | ✅ 完成 |
| P2 | 确定性内核（冲突 / 信号 / 宠物） | ✅ 完成 |
| P3 | 前端与实时通道 + 自然语言录入 | ✅ 完成 · 169 tests passed |
| P4 | LLM 决策与降级 | ✅ 完成 · 245 tests passed |
| P5 | 干预节奏控制与反馈回流 | ✅ 完成 · 273 tests passed |

v1.0 最小闭环至此全部完成。闭环是这样的：

```
改日程 → 冲突检测 → 信号聚合 → 宠物状态（同步快路径）
                            ↓
                       节奏闸门 → LLM 决策（可降级）→ 干预卡片 → 用户反馈
                            ↑                                        │
                            └────────── 反馈回流进 prompt ────────────┘
```

### P5 演示脚本（同样全程无需 API Key）

```bash
# 1. 造一处硬冲突，问一次决策器 —— 注意返回里的 delivered / rhythm
curl -s -X POST 127.0.0.1:8000/api/decisions -H 'Content-Type: application/json' \
  -d '{"trigger":"manual"}'
# → delivered=true  rhythm={used_today:1, remaining_today:2}

# 2. 点「稍后提醒」—— 冷却时钟从**反馈时刻**重新起算
curl -s -X POST 127.0.0.1:8000/api/interventions/<intervention_id>/feedback \
  -H 'Content-Type: application/json' -d '{"kind":"snooze"}'

# 3. 立刻再触发一次自动决策 —— 必须被冷却期拦下
curl -s -X POST 127.0.0.1:8000/api/decisions -H 'Content-Type: application/json' \
  -d '{"trigger":"schedule_change"}'
# → triggered=false  blocked_by="cooldown"

# 4. 连续三次 mute 同一类建议（每次需先产生一条新的 reschedule 干预）
curl -s 127.0.0.1:8000/api/interventions/profile
# → muted_intervention_types: ["reschedule"]
# → 之后该类建议 delivered=false、blocked_by="muted"
```

节奏三参数全部集中在 `backend/app/core/constants.py` §5，**业务代码零字面量**：

| 参数 | 常量 | 值 |
|---|---|---|
| 冷却期 | `INTERVENTION_COOLDOWN_MINUTES` | 45 分钟 |
| 每日上限 | `INTERVENTION_DAILY_LIMIT` | 3 条（按**本地日**重置） |
| 降权阈值 | `INTERVENTION_MUTE_THRESHOLD` | 3 次 |

两条刻意的取舍：**手动触发绕过闸门**（用户主动问一次不该被"额度用完"拒绝），
但仍刷新冷却时钟；**降权是双通道的** —— 既进闸门黑名单，也进 prompt 的负面清单，
让模型自己不再提，而不是每次辛苦生成一条注定被丢掉的建议。

详见 [`docs/13-干预节奏与反馈回流.md`](docs/13-干预节奏与反馈回流.md)。

### P4 演示脚本（默认无 API Key，跑的正是降级路径）

```bash
# 造一处硬冲突（两条日程重叠 30 分钟），然后问一次决策器
curl -s -X POST 127.0.0.1:8000/api/decisions -H 'Content-Type: application/json' \
  -d '{"trigger":"manual"}' | python -m json.tool
# → source=fallback、decision_type=reschedule、1 条 move 建议、pet_line 有台词
# → 再发一次同样请求：source=cache、attempts=0（命中缓存，不再调用）

# 采用建议（走既有 PATCH 链路，不另起一条）
curl -s -X PATCH 127.0.0.1:8000/api/records/<target_record_id> \
  -H 'Content-Type: application/json' \
  -d '{"start_at":"<proposed_start>","end_at":"<proposed_end>"}'
# → 硬冲突归零，/api/state 的 pet.mood 从 alert 回落到 normal
```

卡片上 `source` 有三档：`llm`（模型）/ `cache`（命中缓存或录制）/ `fallback`（规则兜底）。
**fallback 不会伪装成模型输出** —— `caveats` 里明说自己是规则生成的，前端用橙色角标标注。
配好 `DEEPSEEK_API_KEY` 后可用 `scripts/probe_llm.py` 实测能力边界、
用 `python scripts/record_demo.py --decisions --nl-llm` 录制模型真实响应供离线演示回放。
详见 [`docs/12-LLM决策与降级.md`](docs/12-LLM决策与降级.md)。

### P3 演示脚本（同样全程无 LLM 调用）

```bash
# 1. 自然语言录入：解析 → 草稿卡片 → 确认入库 → 自动触发冲突与宠物更新
curl -s -X POST 127.0.0.1:8000/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"text":"今天下午4点到6点产品评审会"}'
# 取 draft 确认（注意：/api/parse 只读，不写库）
curl -s -X POST 127.0.0.1:8000/api/records -H 'Content-Type: application/json' -d '<上面的 draft>'
curl -s -X POST 127.0.0.1:8000/api/parse -H 'Content-Type: application/json' \
  -d '{"text":"今天下午5点到7点技术方案讨论"}'
# 第二次确认后：硬冲突 1 处 overlap 60min，/api/state 的 pet.mood 从 normal → alert
```

支持与不支持的句子以 [`docs/11-自然语言录入.md`](docs/11-自然语言录入.md) 为准
（清单同时也是代码：`backend/app/services/nl_spec.py`，每条都有单测锁定）。

### P2 演示脚本（全程无 LLM 调用）

```bash
python scripts/seed.py          # 生成演示数据（刻意含 1 处重叠 + 1 处零缓冲）
curl -s 127.0.0.1:8000/api/state | python -m json.tool
```

预期输出：`pet.mood == "alert"`，`signals.conflicts` 含 1 条 `overlap`(hard) 与 1 条 `no_buffer`(soft)。

把参与重叠的两条日程挪开，`pet.mood` 应回落到 `tired` —— 这演示了
**前瞻信号（alert）与回顾信号（tired）的严格分离**与优先级短路。

---

## 复现性声明

本项目开发初期未启用版本控制，代码以本地文件形式演进。开发全过程的时间戳证据留存于：docs/prompts/ 各轮对话存档、docs/09 实现决策日志、以日期命名的原始探针输出（如 probe_b10_2026-10-29.json）及各文档内嵌的时间戳。仓库于 2026-10-30 建立，提交历史按实际开发顺序逐里程碑重建：各提交的 Author Date 为对应里程碑的真实开发日期，Committer Date 为仓库建立日（真实提交时间）。重建方法、文件归属规则与逐里程碑证据对照（含证据等级标注）记录于 docs/09 §9。里程碑提交为当时工作内容的快照归档，跨里程碑多次修改的文件以最终形态归入其诞生里程碑，因此不保证每个中间提交可独立构建；完整可复现状态以最终提交为准（见下方 10 分钟复现指南）。2026-10-30 仓库建立后的所有提交均为实时小步提交。

---

## 10 分钟极速复现指南

### 复现五要素

| 要素 | 内容 |
|---|---|
| **环境要求** | Python ≥3.11（`backend/pyproject.toml` 的 `requires-python`）、Node ≥20.19。本机实测：Python 3.13.14、Node v22.22.2、npm 10.9.7（前端基于 Vite 8，更低的 Node 未验证） |
| **依赖安装** | 后端 `cd backend && pip install -e ".[dev]"`；前端 `cd frontend && npm install`。Windows 上一行搞定用 `python scripts/dev.py`（见下方「一键启动」） |
| **环境变量** | `cp .env.example .env`（Windows：`copy .env.example .env`），各变量含义见下表。**不配也能跑**，只有接真实模型才需要填 Key |
| **无 API Key 可跑通闭环** | 不填 Key 时决策器走规则降级（`source=fallback`），冲突检测、宠物情绪、干预节奏、反馈回流全部可用，端到端闭环完整跑通 |
| **DEMO_MODE 开关** | `DEMO_MODE=true`：决策走预录制的 30 条 fixture 回放，零网络请求、离线可答辩；`DEMO_RECORD=true`：把真实调用录制下来供日后回放；重录 `python scripts/record_demo.py --clean --decisions --nl-llm`。**改完 `.env` 必须重启后端**（`load_dotenv` 只在导入时执行一次） |

`.env.example` 变量说明（默认值取自 `backend/app/core/config.py`）：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `DEMO_MODE` | `false` | `true` 时决策走预录制回放，不真实调用模型（答辩/离线演示） |
| `DEMO_RECORD` | `false` | `true` 时把真实调用结果录制下来，供以后回放 |
| `DATABASE_PATH` | `data/app.db` | SQLite 本地文件位置 |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 允许的前端源，按 Origin 逐字节匹配 |
| `DEEPSEEK_API_KEY` | 无 | 接真实模型才需要；不填即走降级路径。`.env` 已被 `.gitignore` 忽略，不会进仓库 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 该账号实测只开放 `deepseek-flash` / `deepseek-v4-pro`；旧的 `deepseek-chat` 已退役，写了会直接 400 |
| `LLM_TIMEOUT_SEC` | `15` | 单次调用超时秒数 |
| `LLM_MAX_TOKENS` | `4096` | 推理与输出共用额度；实测 2048 会被推理 token 吃满导致空 content |
| `LLM_TEMPERATURE` | `0.2` | 决策场景偏好低随机 |

> **统一用 `127.0.0.1`，不要用 `localhost`。**
> 本机 `localhost` 优先解析到 IPv6 `::1`，而后端只绑定 IPv4 `127.0.0.1`；
> 同时 CORS 白名单是按 Origin 逐字节匹配的，`http://localhost:5173` 与
> `http://127.0.0.1:5173` 是两个不同的源。两者不一致时，浏览器会静默拦掉
> 全部请求——页面能打开但数据永远是空的。

### 后端

```bash
cd backend
pip install -e ".[dev]"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

### 前端

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

默认 <http://127.0.0.1:5173>。

### 接入真实模型（可选）

不配 Key 也能跑完整流程 —— 决策走规则降级，宠物情绪、冲突检测、干预节奏都不受影响。
要接 DeepSeek 就把 `.env.example` 复制成项目根目录的 `.env` 并填 Key：

```bash
cp .env.example .env      # Windows: copy .env.example .env
# 然后编辑 .env，填 DEEPSEEK_API_KEY=sk-xxxx
```

**必须重启后端才生效** —— `load_dotenv` 只在模块导入时执行一次。

```bash
python scripts/probe_llm.py                              # 探测能力边界（B10 实测）
python scripts/record_demo.py --clean --decisions --nl-llm  # 录制真实响应，供断网演示
```

`.env` 已被 `.gitignore` 忽略（含 `.env.*` 变体），不会进仓库。
模型名、token 额度等实测结论见 [`docs/01-架构设计.md` §6.2](docs/01-架构设计.md)。

### 一键启动（推荐）

**Windows 上最快的方式**：双击项目根目录的 `start-dev.bat`
（桌面上另有一个同名启动器，双击效果完全一样）。
它会起好两个服务、自动打开浏览器，并在结束时停住窗口让你看见退出码。

想要**无地址栏的独立窗口**，改用 `start.bat`（详见「桌面化呈现」）。

```bash
python scripts/dev.py            # 后端 + 前端，Ctrl+C 一起停
python scripts/dev.py --open     # 就绪后自动打开浏览器（bat 用的就是这个）
python scripts/dev.py --backend  # 只起后端
python scripts/dev.py --no-kill  # 不清理端口（端口被别的服务占用时）
```

手动敲那两条命令也能跑，但有三个坑值得用脚本一次性绕掉：

| 坑 | 表现 | 脚本怎么处理 |
| --- | --- | --- |
| 上一轮进程残留占着端口 | 后端 `Address already in use`；vite 悄悄换到 5174，而书签里是 5173 | `netstat` 精确定位监听 PID 后 `taskkill`，并**等端口真正释放**再启动 |
| 本机 `http_proxy` 指向 127.0.0.1:63000 | 后端活着，页面却一直"连接失败" | 子进程环境里清掉代理变量 |
| 本机 node 不在默认 PATH | Git Bash 里 `npm: command not found` | 自动把 node 目录补进 PATH |

> 端口清理**只杀按端口精确匹配到的监听进程**，不会批量杀 `python.exe` / `node.exe`
> ——这台机器上还跑着别的项目。

### 数据

```bash
python scripts/seed.py            # 写入演示数据（先清空 records/events）
python scripts/seed.py --dry-run  # 只看不写
python scripts/record_demo.py --clean        # 只录 NL 规则解析（21 条，无需 Key）
python scripts/record_demo.py --clean --decisions --nl-llm   # 连模型部分一起录（需 Key）

# 导入旧版 Tkinter 应用的 JSON（默认预演，加 --apply 才写库）
python scripts/import_legacy.py --input 旧数据.json --report
python scripts/import_legacy.py --input 旧数据.json --apply
```

> `import_legacy.py` 的字段映射**已按真实样例校准**（README 时态：已完成）。
> 旧版结构是 `{version, updated_at, plans:[{title, date, start, end, priority, note, done, ...}]}`。
> `--report` 可打印逐字段映射说明。旧版有但新版无对应字段的键
> （`exp_awarded`/`fish_awarded`/`postpone_count`/`original_date`/`id`）会明确标为"有意不导入"。

### 测试

```bash
cd backend && pytest -q
```

确定性内核（`conflict` / `signal` / `pet`）是**纯函数**，单测不启动 Web、不碰数据库、
不访问网络。`tests/test_conflict.py` 含用户点名的四类对抗性用例 + 跨零点。

### 演示模式

```bash
DEMO_MODE=true    # 决策走预录制回放，不真实调用模型
DEMO_RECORD=true  # 把真实调用结果录下来，供以后回放
```

配置项见 `.env.example`。

---

## 桌面化呈现（可选）

> **浏览器是运行载体，桌面窗口只是可选的呈现方式。**
>
> 本项目没有、也不会引入 Tauri / Electron 之类的桌面壳：那会把一个纯 Web 项目
> 变成需要编译的桌面项目，既破坏"clone 下来就能跑"的复现性，也会往 Git 历史里
> 塞进大量工具链产物。下面三种方式都不改变架构 —— 页面、接口、SSE、宠物状态机
> 全部照旧，**没有为任何一个"窗口"写过一行前端分支代码**。

| 方式 | 怎么用 | 额外依赖 |
| --- | --- | --- |
| ① 独立应用窗口（推荐） | 双击 `start.bat`（Windows）/ `./start.sh`（macOS、Linux） | 无（用系统已有的 Chrome / Edge） |
| ② 安装为桌面应用（PWA） | 打开页面 → 浏览器菜单「安装 每日规划助手」 | 无 |
| ③ 原生窗口（可选） | `pip install pywebview && python desktop.py` | pywebview |

### ① 独立应用窗口

`start.bat` / `start.sh` 会起好服务，再用浏览器的 `--app=` 模式打开一个
**无地址栏、无标签页**的独立窗口。浏览器按 **Chrome → Edge** 的顺序找
（两者都支持 `--app=`）；都没有才回退默认浏览器，并在控制台明说
"不是独立窗口"，**不假装成功**。

窗口使用独立的 `--user-data-dir`（`%LOCALAPPDATA%\DailyPlanner\AppProfile`），
不继承你日常浏览器的登录态与历史记录。

> 地址固定用 `127.0.0.1`，**不写 `localhost`**：本机 localhost 优先解析到 IPv6 `::1`，
> 而后端只绑 IPv4；且 CORS 白名单按 Origin 逐字节匹配，两者是不同的源。

### ② 安装为桌面应用（PWA）

`frontend/public/manifest.json` 提供了名称、图标与 `display: standalone`，
所以在 Chrome / Edge 里点菜单的「安装 每日规划助手」，就能得到一个独立图标，
打开后观感与 `--app` 窗口一致，还能固定到任务栏 / 程序坞。

图标是**纯文本 SVG**（`icon.svg` / `icon-maskable.svg`）——
没有往仓库里塞 PNG 之类的二进制文件，这与"不污染 Git 历史"是同一个原则。

### ③ 原生窗口（可选，不进主流程）

`desktop.py` 用 pywebview 开一个原生窗口指向本地服务：

```bash
pip install pywebview
python desktop.py            # 关闭窗口即停止服务
python desktop.py --debug    # 打开调试菜单
```

它是**独立的单个文件**：不改 `scripts/dev.py` 的任何默认行为，只是 `import dev`
复用同一套启动函数；没装 pywebview 时给出安装提示并以退出码 1 结束，
不影响其它任何启动方式。关窗口 / Ctrl+C 都会把后端与前端收干净
（含 npm 拉起的 vite 子进程 —— 只 `terminate()` 父进程会留下占端口的孤儿）。

---

## 目录结构

```
backend/     FastAPI 应用（单用户，无鉴权）
frontend/    React + TypeScript + Vite
scripts/     dev.py（一键启动）、seed.py（演示数据）、import_legacy.py（旧版迁移）、
             record_demo.py（DEMO 录制）、probe_llm.py（DeepSeek 能力探测）
desktop.py   可选：pywebview 原生窗口（不进主流程，需另装依赖）
start.bat    可选：无地址栏的独立应用窗口
docs/        架构设计
data/        SQLite 数据库（gitignore）
```

| 文档 | 内容 |
|---|---|
| [`docs/01-架构设计.md`](docs/01-架构设计.md) | 架构设计：模块划分、接口契约、数据流、降级策略、分期计划 |
| [`docs/09-实现决策日志.md`](docs/09-实现决策日志.md) | 排查全过程：B10 实测、"空 content"的真实成因、被推翻的判断、环境坑 |
| [`docs/10-旧功能对照表.md`](docs/10-旧功能对照表.md) | 旧版 Tkinter 全部功能 → 新版的迁移状态与决策理由 |
| [`docs/11-自然语言录入.md`](docs/11-自然语言录入.md) | 一句话录入：支持例句清单、错误码、防硬编设计、DEMO 录制 |
| [`docs/12-LLM决策与降级.md`](docs/12-LLM决策与降级.md) | LLM 决策器：契约、10 条校验、失败阶梯、规则降级引擎 |
| [`docs/13-干预节奏与反馈回流.md`](docs/13-干预节奏与反馈回流.md) | 节奏闸门三参数、两道检查点、一次 mute 的完整落地链 |

---

## 设计决策速览

这些是本项目**刻意的选择**，不是默认结果。答辩时被问到"为什么这么做"时，
答案都在这里：

| 决策 | 选择 | 理由 |
|---|---|---|
| 宠物情绪 | 确定性状态机，LLM 只写台词 | 情绪是仪表盘，必须可复现；模型抖动会让"因果感"断裂 |
| 冲突检测 / 节奏 | 后端纯函数，不交给 LLM | 可单测、可脱离 Web 运行；错了能定位 |
| 降级方案 | 与 LLM **同接口**，标注 `source=fallback` | 降级是"换一个实现"，不是"走另一条代码路径"；且**不伪装成模型输出** |
| 存储时间 | UTC naive，只在 API 层转本地 | 单一转换点；到处转迟早错位一个时区 |
| 桌面化 | 浏览器是载体，窗口只是可选呈现 | 引入 Tauri/Electron 会破坏"clone 下来就能跑" |
| 图标 | 纯文本 SVG，不生成 PNG | 二进制进仓库与"不污染提交"直接冲突 |
| 鉴权 | v1.0 不做 | 有意的范围决策，见上方范围声明 |

详细目录结构见 `docs/01-架构设计.md` §7。
