from typing import TypedDict


class TaskSpec(TypedDict):
    """对用户可见的有限研究任务规格"""

    ordinal: int
    role: str
    title: str
    allowed_tools: list[str]
    depth: int
    token_budget: int
    time_budget_seconds: int


class ResearchBrief(TypedDict):
    """单个 Researcher 分支的只读研究范围"""

    ordinal: int
    focus: str
    allowed_tools: list[str]
    depth: int


class PlannerOutput(TypedDict):
    """Planner 生成的有限计划和并行研究范围"""

    tasks: list[TaskSpec]
    research_briefs: list[ResearchBrief]


def plan(question: str) -> PlannerOutput:
    """根据问题生成有限计划，不递归派生 Agent"""
    tasks: list[TaskSpec] = [
            {
                "ordinal": 1,
                "role": "researcher",
                "title": "检索空间资料与可用网页证据",
                "allowed_tools": ["document_search", "web_search"],
                "depth": 1,
                "token_budget": 2400,
                "time_budget_seconds": 30,
            },
            {
                "ordinal": 2,
                "role": "verifier",
                "title": "核对证据与引用边界",
                "allowed_tools": ["citation_read"],
                "depth": 1,
                "token_budget": 1600,
                "time_budget_seconds": 20,
            },
            {
                "ordinal": 3,
                "role": "writer",
                "title": "分析用户问题并形成初步结论",
                "allowed_tools": [],
                "depth": 0,
                "token_budget": 2000,
                "time_budget_seconds": 20,
            },
        ]
    if (
        any(keyword in question for keyword in ("计算", "绘图", "数据分析", "算一下"))
    ):
        tasks.append(
            {
                "ordinal": 4,
                "role": "python_sandbox",
                "title": "在受限 Python Sandbox 中完成计算",
                "allowed_tools": ["python_sandbox"],
                "depth": 0,
                "token_budget": 200,
                "time_budget_seconds": 15,
            }
        )
    return {
        "tasks": tasks,
        "research_briefs": [
            {
                "ordinal": 1,
                "focus": f"检索问题的直接证据：{question}",
                "allowed_tools": ["document_search", "web_search"],
                "depth": 1,
            },
            {
                "ordinal": 2,
                "focus": f"检索问题的差异、限制与反例：{question}",
                "allowed_tools": ["document_search", "web_search"],
                "depth": 1,
            },
        ],
    }
