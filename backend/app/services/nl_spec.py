"""《确定支持的例句清单》的唯一数据源。

为什么是代码而不是写在文档里的一段话：

- **测试**要用它做表格驱动断言 → 清单改了测试立刻知道
- **录制脚本**要用它生成 DEMO fixture → 保证演示兜底覆盖的就是承诺支持的句子
- **文档**由它渲染 → 不会出现"文档写了但代码不支持"

期望值用**相对量**表达：`(day_offset, hour, minute)`。
这样同一份清单既能对固定基准日做断言，也能在任何一天重新录制 fixture
（DEMO 回放因此是"常青"的，不会因录制于三天前而给出过期日期）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


def next_calendar_week_offset(today: date, weekday_index: int) -> int:
    """「下周X」距今天的天数。与解析器同一套定义：下一个日历周，周一起算。"""
    next_monday = today + timedelta(days=(7 - today.weekday()))
    target = next_monday + timedelta(days=weekday_index)
    return (target - today).days


@dataclass(frozen=True)
class Example:
    text: str
    title: str
    start: tuple[int, int, int]          # (day_offset, hour, minute)
    end: tuple[int, int, int] | None     # None = 该句拿不到结束时间
    priority: int
    status: str                          # resolved / need_user
    note: str = ""
    week_index: int | None = None        # 非 None 时 day_offset 按日历周动态计算

    def start_tuple(self, today: date) -> tuple[int, int, int]:
        offset = self.start[0]
        if self.week_index is not None:
            offset = next_calendar_week_offset(today, self.week_index)
        return (offset, self.start[1], self.start[2])

    def end_tuple(self, today: date) -> tuple[int, int, int] | None:
        return self.end


#: 相对今天的 day_offset：0=今天 1=明天 2=后天 3=大后天，以此类推
SUPPORTED_EXAMPLES: tuple[Example, ...] = (
    Example("明天上午9点到11点开会", "开会", (1, 9, 0), (1, 11, 0), 3, "resolved",
            "最常见的完整写法"),
    Example("今天下午3点到4点半评审PR", "评审PR", (0, 15, 0), (0, 16, 30), 3, "resolved",
            "第二个时间点继承『下午』时段"),
    Example("后天晚上8点看电影", "看电影", (2, 20, 0), None, 3, "need_user",
            "只有起始点 → 结束留空待填，绝不默认 +1 小时"),
    Example("下周三上午10点团队周会", "团队周会", (0, 10, 0), None, 3, "need_user",
            "『下周』按日历周解析，会给出提示", week_index=2),
    Example("11月3日下午2点到4点客户拜访", "客户拜访", (5, 14, 0), (5, 16, 0), 3, "resolved",
            "显式月日 + 时段继承"),
    Example("30号晚上7点健身", "健身", (1, 19, 0), None, 3, "need_user",
            "『30号』按最近未来日期解读"),
    Example("明天9:00到10:30写项目方案", "写项目方案", (1, 9, 0), (1, 10, 30), 3, "resolved",
            "冒号写法；未写时段会明确提示"),
    Example("后天下午2点到3点面试高优先级", "面试", (2, 14, 0), (2, 15, 0), 2, "resolved",
            "优先级关键词生效"),
    Example("明天早上7点跑步", "跑步", (1, 7, 0), None, 3, "need_user",
            "早间时段词"),
    Example("今天中午12点午饭", "午饭", (0, 12, 0), None, 3, "need_user",
            "中午不参与上下午换算"),
    Example("今天晚上11点到明天凌晨1点值班", "值班", (0, 23, 0), (1, 1, 0), 3, "resolved",
            "★ 跨零点：旧版会算成长度 0"),
    Example("11月20日23:30到11月21日1:00上线", "上线", (22, 23, 30), (23, 1, 0), 3, "resolved",
            "★ 跨零点 + 两个显式日期"),
    Example("大后天下午5点交材料", "交材料", (3, 17, 0), None, 3, "need_user",
            "三天后的相对表达"),
    Example("明天14:00到15:30客户沟通低优先级", "客户沟通", (1, 14, 0), (1, 15, 30), 4, "resolved",
            "低优先级关键词"),
    Example("12月1日上午9点到12点季度复盘", "季度复盘", (33, 9, 0), (33, 12, 0), 3, "resolved",
            "跨月的显式日期"),
)

#: 明确不支持、会返回结构化错误的写法。列出来是为了**管理预期**：
#: 演示时不要用清单外的句子。这些正是 P4 接 LLM 之后最先被覆盖的场景。
UNSUPPORTED_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("月底前交季度报告", "『月底』没有确定日期 → NXC-NEED-LLM"),
    ("下周抽空做复盘", "『抽空』无确定语义 → NXC-NEED-LLM"),
    ("明天上午10点开会然后下午3点写文档", "一句话多件事 → NXC-NEED-LLM"),
    ("明天上午9点到8点开会", "结束早于开始 → NXC-1"),
    ("今天晚上11点到1点值班", "跨天但没写结束日期 → NXC-1"),
    ("明天9点10点11点各一个会", "出现 3 个时间点 → NXC-T1"),
)


def iter_supported_texts() -> list[str]:
    return [e.text for e in SUPPORTED_EXAMPLES]
