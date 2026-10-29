# P4 · LLM 决策与降级（实现日志）

> 本文记录 P4 实现期的**判断与踩坑**，与 `docs/01-架构设计.md` 的分期计划配套阅读。
> 架构文档写"应该是什么样"，本文写"实际做成什么样、为什么做这些取舍"。

---

## 1. 交付清单

| 文件 | 职责 |
|---|---|
| `backend/app/llm/contract.py` | 输入/输出契约**唯一真源**；同时服务后端校验、prompt 示例、前端类型 |
| `backend/app/llm/prompts/decision.py` | 决策提示词（含"必须允许 no_action"等硬约束） |
| `backend/app/llm/prompts/nl_parse.py` | 自然语言兜底解析的提示词 |
| `backend/app/llm/provider.py` | **唯一**外部调用出口（httpx 直连，不引入 SDK） |
| `backend/app/llm/validator.py` | 10 条校验规则 |
| `backend/app/llm/orchestrator.py` | 触发判定 / 指纹缓存 / 重试 / 降级 |
| `backend/app/services/fallback.py` | 规则降级引擎（与 LLM **同接口**） |
| `backend/app/services/decision_flow.py` | 决策链路的 IO 边界（读库 → 决策 → 落库 → 广播） |
| `backend/app/services/nl_llm.py` | NL 的 LLM 兜底 |
| `backend/app/api/decisions.py` | `POST /api/decisions`、`GET /api/decisions/latest` |
| `frontend/src/components/InterventionCard.tsx` | 干预卡片 + `source` 角标 |
| `scripts/probe_llm.py` | DeepSeek 能力探测（B10 实测） |
| `scripts/record_demo.py --decisions --nl-llm` | 录制模型的真实响应 |

**新增依赖：0。** DeepSeek 提供 OpenAI 兼容接口，装 SDK 最省事，但本项目坚持用 httpx 直连：

1. 少一个依赖少一个失败面（FastAPI 自带 httpx）；
2. **降级策略依赖错误分类**——`finish_reason == "length"` 要抬 token 重试、429 才是限流。
   SDK 会把这些统一包成泛化的 `APIError`，而区分它们正是 `LLMErrorKind` 存在的理由。

   （**已修订**：空 content 早年按"偶发故障直接 L0"处理，2026-10-29 实测推翻了这一点 ——
   它的 `finish_reason` 同样是 `length`、`reasoning_tokens == max_tokens`，
   是**推理把额度吃光**的极端形态。现在它与截断共用同一条抬档重试路径，见 `docs/09` §3②。）

---

## 2. 触发判定（文档 B3）

```
hard 冲突 > 0           → 触发
拖延指数 ≥ 0.5          → 触发
trigger == "manual"     → 恒触发（绕过判定，演示用）
其余                    → 不触发
```

**"改了日程但没冲突就不调用"是有意为之。** 这是最常见的操作，每次都问模型既慢又费钱，
而"你改了个日程，模型说：好的"这种交互没有任何价值。

前端在记录增删改与事件上报后，通过 FastAPI 的 `BackgroundTasks` 异步触发决策：
宠物情绪走**同步快路径**（仪表盘不等模型），决策走后台任务（模型要几秒，不能让用户等保存按钮转圈）。

---

## 3. 10 条校验：模型的输出是"待验证的提案"

| # | 规则 | 处理 | 实现位置 |
|---|---|---|---|
| 1 | 是 JSON 对象 | L1 重试 | `validator.validate_payload` |
| 2 | 符合 `DecisionPayload` 结构 | L1 重试（回传错误原文） | 同上 |
| 3 | 枚举合法 | L1 重试 | 同上 |
| 4 | `confidence ∈ [0,1]` | L1 重试 | 同上 |
| 5 | `target_record_id` 必须存在 | 丢弃该条 | 同上 |
| 6 | 时间有效且落在工作时间内 | 丢弃该条 | 同上 |
| 7 | 不得改动 `immutable_record_ids` | 丢弃该条 | 同上 |
| 8 | 应用后不得引入新硬冲突 | 丢弃该条；全废则 L2→L0 | 同上 |
| 9 | 条数 ≤ `max_suggestions` | 截断 | 同上 |
| 10 | 文本超长 | 截断（**不判失败**） | 同上 |

### 三个实现期的判断

**① 规则 8 用"差集 + 必须有改善"双重判据。**
只判"不得引入新冲突"是不够的：把一条日程挪到**仍然撞车**的位置，既不新增也不解决，
那种建议出现在卡片上比没有建议更糟。所以判据是：

- 不得出现 baseline 中没有的 hard 冲突对（**差集**，保住模型的部分解）；
- 且 hard 冲突数必须**严格减少**（证明它确实解决了问题）。

只写第一条，模型给出的部分可用建议会被整包否掉；只写第二条，部分解会被误杀。两者要一起用。

**② 规则 10 在 Pydantic 校验之前执行。**
契约里写了 `maxLength`，若交给 Pydantic 判，超长会变成 L1 重试——为"模型太啰嗦"浪费一次调用不划算。
所以先截断原始 dict，再走结构校验。

**③ 重试时把校验错误原文回传给模型。**
与其说"你错了"，不如写清"suggestions[0].confidence: 值不在 [0,1] 内"。
`test_retry_sends_validation_errors_back` 锁定了这个行为。

---

## 4. 降级：与 LLM 同接口的另一种实现

```python
build_fallback(req: DecisionRequest) -> DecisionPayload   # 与 LLM 调用同签名
```

这不是巧合。**降级应该是"换一个实现"，不是"走另一条代码路径"** ——
编排器不需要知道现在是模型在说话还是规则在说话，它只看到同一份契约输出。

算法的性格：保守、可解释、绝不激进。

- 按 `(priority, start)` 排序，优先级高（数值小）的先占位，后来的让路；
- 落点从"挡路者结束 + 缓冲"开始找，步进 **15 分钟**（生成 `10:07` 这种时间只会让人怀疑系统坏了）；
- 只在 `working_hours` 内找，最多往后推 3 天；
- 找不到落点就**不硬排**，转为 nudge 提醒一句；
- 不缩短任何任务（缩短等于替用户做价值判断，规则层没这个资格）。

**降级方案会自称降级。** `caveats` 里写明"由本地规则引擎生成"，
前端把 `fallback` 渲染成橙色角标 —— 冒充模型判断是事故，不是技巧。

### L3：模型说"不干预"，但信号很强

`decision_type == "no_action"` 且拖延指数 ≥ 0.8 时，后端仍发一条规则提醒。
这不是不信任模型，而是承认一件事：**模型看不到"用户已经三天没动"的重量**，它只看得见当前这段对话。
反过来，信号不强时模型说"不干预"就真的不干预（`test_model_no_action_is_respected_when_signal_is_weak`）——
两条测试成对存在，否则"后端可以覆盖模型"会变成过度干预的借口。

---

## 5. 踩过的坑（按代价排序）

### ① naive 时间被二次转换 —— 建议整体平移 8 小时

契约输入侧（DB 读出来的记录）已经是 **UTC naive**，输出侧（模型给的字符串）是**本地墙上时间**。
两类时间如果共用一个类型，`local_to_utc` 会被多调一次，建议整体偏移一个时区。

修法是把它们拆成两个类型：

```python
UtcDT  # 输入侧：naive 视作内部表示（UTC naive），不再转换
IsoDT  # 输出侧：naive 字符串按用户本地时区解释（模型不会写 +08:00）
```

对 `datetime` 输入（规则引擎构造建议时传的）也按 UTC 处理 —— 否则 fallback 生成的建议会被自己平移一次。
这是 P3 在 NL 上已经踩过的同一个坑，第二次出现就该用类型系统解决，而不是再调一次函数顺序。

### ② 指纹随日期漂移 —— 录制的 fixture 隔天全部落空

决策请求里全是绝对时间，直接取哈希的话，今天录的响应明天就再也不命中。
这与 NL 录制时"存 `day_offset` 而不是日期"是同一个问题的两种形态。

修法：指纹计算前把 payload 里所有 ISO 时间替换成**距 `triggered_at` 的分钟数**
（`orchestrator._relativize`）。于是"场景整体平移"不再改变指纹。
`test_fingerprint_is_stable_when_whole_scenario_shifts_a_day` 锁定了这一点。

### ③ `attempts` 曾被写成"看起来合理"的假数字

provider 不可用（没配 Key / DEMO 模式）时一次都没调用过，却返回 `attempts = MAX_RETRIES + 1`。
`attempts` 是要给人看的可观测字段，填假数字比留空更糟。改为由 `_call_with_retry` 通过 `stats` 回传真实次数。

### ④ 后台任务复用请求会话

`run_decision_bg` 必须**自建 Session**。请求会话在响应返回后就关闭了，
后台任务里用它必然踩到 "Session is closed"。

### ⑤ 删除日程后没有联动

P3 的 `DELETE` 完全不触发重算：删掉一条冲突日程后宠物还停在 `alert`，直到手动刷新才恢复。
P4 补上了。另外删除后对象已从会话脱离，访问 `record.id` 会抛 `DetachedInstanceError`，
所以 `_on_record_changed` 接受 `Record | str`。

---

## 6. NL 接 LLM：schema 两期一致的兑现

P3 定契约时就把 `source` 留成了 `rule / llm / cache` 三态，P4 只是多了一个取值来源：

| 阶段 | `NEED_LLM` 怎么处理 | 对外 `source` |
|---|---|---|
| P3 | 降级为 `error`（code=`NXC-NEED-LLM`） | 恒 `rule` |
| P4 | 真调模型；不通过校验则退回 `need_user` / `error` | 可能出现 `llm` |

**前端草稿卡片一行代码都没改** —— 这就是「切分点落在数据源而非功能上」这条设计决策的兑现（§0.1 D-4）。

模型不是免检通道：它给出的时间同样要过区间校验、顺序校验、跨度校验，
拿不到就 `need_user`，绝不悄悄填一个默认值。

录制回放的 fixture 单独存一类（`nl_parse_llm`）。混在一个 key 空间里会出现
"P3 录的 error 结果挡住了 P4 的模型结果"。

---

## 7. 怎么演示

### 无 API Key（默认，也是答辩断网时的形态）

- 决策 → `source=fallback`，卡片上橙色角标"规则兜底"，`caveats` 明说自己是规则生成；
- 宠物情绪照常工作（情绪是确定性仪表盘，与这条链路无关）；
- NL 清单外的句子 → 与 P3 完全一致的 `error` + hint。

### 有 API Key

```bash
# 1. 配 Key
echo "DEEPSEEK_API_KEY=sk-xxx" >> .env

# 2. 实测能力边界（B10：thinking 与 json_object 能否同用）
python scripts/probe_llm.py --json-out probe_result.json

# 3. 录制模型真实响应（答辩前一天跑一次）
python scripts/record_demo.py --clean --decisions --nl-llm

# 4. 打开 DEMO_MODE 演示：命中录制 → source=cache，全程不联网
echo "DEMO_MODE=true" >> .env
```

DEMO_MODE 下**未命中录制就转规则降级**，绝不偷偷真调 ——
否则"断网演示"会在某一次请求上悄悄变成联网请求，现场直接穿帮。

---

## 8. 待办

- **B10（`thinking` 与 `json_object` 能否同用）：仍未实测**，本项目当前没有 API Key。
  探测脚本已就绪（`scripts/probe_llm.py`），拿到 Key 后跑一次把结论回填到 §6.2 与 §9.3。
  这一项不接受猜测。
- 干预的节奏控制（冷却期 / 每日上限 / 类型降权）与反馈回流属 **P5**，本期"有建议就发"。
