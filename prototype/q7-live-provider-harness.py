"""Q7 一次性 live provider harness

使用项目根目录 .env 的 OpenAI-compatible LiteLLM 配置，验证模型的 Tool 选择和
结构化 Observation 消费。该文件只用于 prototype，不属于生产代码
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from deep_researcher.settings import Settings
from litellm import acompletion


CORPUS = [
    {
        "revision_id": "rev-space-1",
        "path": "/sources/research/file-space.md",
        "scope": "sources",
        "media": "text/markdown",
        "hash": "sha256:" + "1" * 64,
        "text": (
            "Research File Space 需要统一的研究文件视图\n"
            "文件空间与版本定位必须保留稳定证据\n"
            "结构化 Tool 通过固定 Revision 保留可复现定位\n"
            "恢复时使用 cursor 继续读取截断 Observation\n"
            "来源文件是只读的，发布产物进入 artifacts\n"
            "不可变内容用于恢复和审计"
        ),
    },
    {
        "revision_id": "rev-runbook-1",
        "path": "/sources/runbook/errors.md",
        "scope": "sources",
        "media": "text/markdown",
        "hash": "sha256:" + "2" * 64,
        "text": (
            "恢复队列遇到暂时性失败时应保留 attempt\n"
            "错误处理：错误码 ERR-427 表示 revision manifest 过期\n"
            "处理日期为 2026-08-17\n"
            "读取固定 Revision 后再重试"
        ),
    },
    {
        "revision_id": "rev-unicode-1",
        "path": "/sources/笔记/恢复策略.md",
        "scope": "sources",
        "media": "text/markdown",
        "hash": "sha256:" + "3" * 64,
        "text": (
            "恢复策略需要记录不可变版本\n"
            "路径包含 Unicode 也必须返回稳定 locator\n"
            "不要把宿主路径当成文件身份\n"
            "Unicode 路径的版本定位仍然固定"
        ),
    },
    {
        "revision_id": "rev-long-1",
        "path": "/sources/logs/long.log",
        "scope": "sources",
        "media": "text/plain",
        "hash": "sha256:" + "4" * 64,
        "text": "\n".join(
            ["长日志用于验证截断恢复和版本定位"]
            + [
                f"attempt {i + 1} status={'ERR-427' if i in {3, 7, 11} else 'ok'} "
                f"cursor={i + 1}；固定 Revision rev-long-1"
                for i in range(12)
            ]
        ),
    },
    {
        "revision_id": "rev-duplicate-1",
        "path": "/sources/archive/report.md",
        "scope": "sources",
        "media": "text/markdown",
        "hash": "sha256:" + "5" * 64,
        "text": "归档报告：旧版本仅用于对照",
    },
    {
        "revision_id": "rev-duplicate-2",
        "path": "/artifacts/report.md",
        "scope": "artifacts",
        "media": "text/markdown",
        "hash": "sha256:" + "6" * 64,
        "text": "发布产物：正式产物由 work revision 发布并保留 provenance",
    },
    {
        "revision_id": "rev-binary-1",
        "path": "/sources/data/archive.bin",
        "scope": "sources",
        "media": "application/octet-stream",
        "hash": "sha256:" + "7" * 64,
        "text": "\x00\x01binary",
    },
    {
        "revision_id": "rev-revoked-1",
        "path": "/sources/revoked/old.md",
        "scope": "sources",
        "media": "text/markdown",
        "hash": "sha256:" + "8" * 64,
        "text": "已撤销来源，不应进入检索结果",
        "revoked": True,
    },
    {
        "revision_id": "rev-work-1",
        "path": "/work/tasks/q7/scratch.md",
        "scope": "work",
        "media": "text/markdown",
        "hash": "sha256:" + "9" * 64,
        "text": "mutable scratch：只属于当前 Task，不进入长期 file_search\n精确恢复游标为 cursor=4",
    },
]

# 精确 token 噪声用于验证 search top-k 与 grep 全量定位的真实差异
CORPUS[0:0] = [
    {
        "revision_id": f"rev-noise-{index:02d}",
        "path": f"/sources/noise/decoy-{index:02d}.txt",
        "scope": "sources",
        "media": "text/plain",
        "hash": "sha256:" + f"{index + 10:064x}"[-64:],
        "text": (
            f"索引噪声样本 {index:02d}，不是目标运行手册\n"
            "ERR-427 2026-08-17 revision manifest cursor=4"
        ),
    }
    for index in range(24)
]

TASKS = [
    *[
        {"id": f"E{i + 1}", "type": "exact", "query": q, "context": context, "target": target}
        for i, (q, context, target) in enumerate(
            [
                ("ERR-427", "恢复队列运行手册", "rev-runbook-1"),
                ("2026-08-17", "恢复队列运行手册", "rev-runbook-1"),
                ("revision manifest", "恢复队列运行手册", "rev-runbook-1"),
                ("cursor=4", "当前 Task 的 mutable work scratch", "rev-work-1"),
                ("ERR-427", "长日志中的错误记录", "rev-long-1"),
                ("不可变版本", "Unicode 恢复策略", "rev-unicode-1"),
                ("稳定 locator", "Unicode 恢复策略", "rev-unicode-1"),
                ("provenance", "正式发布产物", "rev-duplicate-2"),
            ]
        )
    ],
    *[
        {"id": f"C{i + 1}", "type": "concept", "query": q, "target": target}
        for i, (q, target) in enumerate(
            [
                ("恢复策略", "rev-unicode-1"),
                ("版本定位", "rev-space-1"),
                ("错误处理", "rev-runbook-1"),
                ("文件空间", "rev-space-1"),
                ("Unicode 路径", "rev-unicode-1"),
                ("不可变内容", "rev-space-1"),
                ("恢复队列", "rev-runbook-1"),
                ("发布产物", "rev-duplicate-2"),
            ]
        )
    ],
    *[
        {"id": f"M{i + 1}", "type": "mixed", "query": q, "exact": exact, "context": context, "target": target}
        for i, (q, exact, context, target) in enumerate(
            [
                ("恢复队列", "ERR-427", "恢复队列运行手册", "rev-runbook-1"),
                ("文件空间", "不可变", "Research File Space 说明", "rev-space-1"),
                ("Unicode", "稳定 locator", "Unicode 恢复策略", "rev-unicode-1"),
                ("长日志", "ERR-427", "长日志中的错误记录", "rev-long-1"),
                ("发布", "provenance", "正式发布产物", "rev-duplicate-2"),
                ("恢复策略", "版本", "Unicode 恢复策略", "rev-unicode-1"),
                ("错误处理", "2026-08-17", "恢复队列运行手册", "rev-runbook-1"),
                ("版本定位", "cursor=4", "长日志中的版本定位", "rev-long-1"),
            ]
        )
    ],
]


def _text_files(scope: str | None = None) -> list[dict[str, Any]]:
    """返回当前 capability 可见的文本 Revision"""
    return [
        item
        for item in CORPUS
        if not item.get("revoked")
        and item["media"].startswith("text/")
        and (scope is None or item["scope"] == scope)
    ]


def _path_error(value: str) -> str | None:
    """校验逻辑路径并拒绝越权片段"""
    if ".." in value or "\x00" in value:
        return "path_traversal_denied"
    return None


def _page_state(cursor: int, limit: int, total: int) -> dict[str, Any]:
    """生成统一的截断与 continuation 状态"""
    truncated = cursor + limit < total
    return {
        "truncated": truncated,
        "next_cursor": cursor + limit if truncated else None,
        "continuation_required": truncated,
    }


def _rejected(error: str) -> dict[str, Any]:
    """生成一致的 typed 拒绝 Observation"""
    return {
        "status": "rejected",
        "error": error,
        "truncated": False,
        "next_cursor": None,
        "continuation_required": False,
    }


def file_search(args: dict[str, Any]) -> dict[str, Any]:
    """执行确定性语义导航并返回 Revision/chunk locator"""
    query = str(args.get("query", "")).lower()
    aliases = [
        ["恢复", "重试", "继续", "recovery", "resume"],
        ["版本", "revision", "不可变", "hash"],
        ["错误", "失败", "error", "err"],
        ["队列", "queued", "queue"],
    ]
    rows: list[dict[str, Any]] = []
    searchable = [item for item in _text_files() if item["scope"] in {"sources", "artifacts"}]
    for item in searchable:
        haystack = item["text"].lower()
        score = sum(2 for group in aliases if any(word in query and word in haystack for word in group))
        score += sum(1 for word in query.split() if len(word) > 1 and word in haystack)
        if score:
            rows.append(
                {
                    "revision_id": item["revision_id"],
                    "hash": item["hash"],
                    "path": item["path"],
                    "chunk_locator": {"start_line": 1, "end_line": min(3, item["text"].count("\n") + 1)},
                    "preview": item["text"][:280],
                    "score": score,
                    "receipt": "fts+vector+rrf+rerank(live-harness-fixture)",
                }
            )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {
        "status": "ok",
        "candidates": rows[: int(args.get("top_k", 5))],
        "truncated": False,
        "next_cursor": None,
        "continuation_required": False,
    }


def file_list(args: dict[str, Any]) -> dict[str, Any]:
    """执行有界文件列表并返回当前 Revision 摘要"""
    prefix = str(args.get("prefix", ""))
    if error := _path_error(prefix):
        return _rejected(error)
    scope = str(args.get("scope", "sources"))
    rows = [item for item in _text_files(scope) if item["path"].startswith("/" + prefix.lstrip("/"))]
    if args.get("glob") == "**/*.md":
        rows = [item for item in rows if item["path"].endswith(".md")]
    cursor = int(args.get("cursor", 0))
    limit = min(int(args.get("limit", 20)), 20)
    page = rows[cursor : cursor + limit]
    return {
        "status": "ok",
        "entries": [
            {
                "entry_id": f"entry-{item['revision_id']}",
                "revision_id": item["revision_id"],
                "path": item["path"],
                "media": item["media"],
                "size": len(item["text"]),
                "hash": item["hash"],
            }
            for item in page
        ],
        **_page_state(cursor, limit, len(rows)),
    }


def file_read(args: dict[str, Any]) -> dict[str, Any]:
    """按固定 Revision 返回带行和字节定位的文本窗口"""
    item = next((row for row in CORPUS if row["revision_id"] == args.get("revision_id")), None)
    if item is None or item.get("revoked"):
        return _rejected("revision_not_found")
    if not item["media"].startswith("text/"):
        return _rejected("unsupported_media_type")
    lines = item["text"].splitlines()
    if args.get("tail_lines") is not None:
        start = max(0, len(lines) - int(args["tail_lines"]))
        count = int(args["tail_lines"])
    elif args.get("cursor") is not None:
        start = max(0, int(args["cursor"]))
        count = int(args.get("line_count", 5))
    else:
        start = max(0, int(args.get("start_line", 1)) - 1)
        count = int(args.get("line_count", 5))
    selected = lines[start : start + count]
    paginated = args.get("cursor") is not None or ("start_line" not in args and "tail_lines" not in args)
    truncated = paginated and start + count < len(lines)
    return {
        "status": "ok",
        "revision_id": item["revision_id"],
        "path": item["path"],
        "hash": item["hash"],
        "lines": [
            {"line": start + index + 1, "byte_start": sum(len(line) + 1 for line in lines[: start + index]), "text": text}
            for index, text in enumerate(selected)
        ],
        "truncated": truncated,
        "next_cursor": start + count if truncated else None,
        "continuation_required": truncated,
    }


def file_grep(args: dict[str, Any]) -> dict[str, Any]:
    """在冻结 Revision 集合上执行有界精确匹配"""
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return _rejected("empty_pattern")
    revisions = args.get("revision_ids")
    rows = [item for item in CORPUS if item["revision_id"] in revisions] if revisions else _text_files(str(args.get("scope", "sources")))
    if revisions and (len(rows) != len(set(revisions)) or any(item.get("revoked") for item in rows)):
        return _rejected("revision_not_found")
    if any(item["media"] == "application/octet-stream" for item in rows):
        return _rejected("unsupported_media_type")
    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    try:
        regex = re.compile(re.escape(pattern) if args.get("mode", "literal") == "literal" else pattern, flags)
    except re.error:
        return _rejected("invalid_regex")
    matches: list[dict[str, Any]] = []
    before = int(args.get("before", 0))
    after = int(args.get("after", 0))
    for item in rows:
        lines = item["text"].splitlines()
        for index, line in enumerate(lines):
            if regex.search(line):
                start = max(0, index - before)
                end = min(len(lines), index + after + 1)
                matches.append(
                    {
                        "revision_id": item["revision_id"],
                        "hash": item["hash"],
                        "path": item["path"],
                        "line": index + 1,
                        "byte_start": sum(len(value) + 1 for value in lines[:index]),
                        "preview": "\n".join(lines[start:end]),
                    }
                )
    cursor = int(args.get("cursor", 0))
    limit = min(int(args.get("limit", 20)), 20)
    page = matches[cursor : cursor + limit]
    return {
        "status": "ok",
        "matches": page,
        **_page_state(cursor, limit, len(matches)),
    }


def raw_shell(args: dict[str, Any]) -> dict[str, Any]:
    """模拟完全只读 raw shell 评估上界，不接受命令字符串"""
    query = str(args.get("query", "")).lower()
    hits = []
    for item in _text_files():
        for index, line in enumerate(item["text"].splitlines()):
            if query in line.lower():
                hits.append({"revision_id": item["revision_id"], "path": item["path"], "line": index + 1, "text": line})
    return {
        "status": "ok",
        "hits": hits,
        "truncated": False,
        "next_cursor": None,
        "continuation_required": False,
    }


EXECUTORS = {"file_search": file_search, "file_list": file_list, "file_read": file_read, "file_grep": file_grep, "raw_shell": raw_shell}


def tool_schema(group: str) -> list[dict[str, Any]]:
    """返回实验组允许的结构化 Tool schema"""
    search = {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "只用于概念、主题或同义表达导航。精确 token、日期、错误码和原句不要先调用本工具，应先用 file_grep。结果只提供固定 Revision/chunk locator 和 preview，最终证据必须 file_read",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 5}}, "required": ["query"], "additionalProperties": False},
        },
    }
    read = {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取固定 revision_id 的精确原文窗口并返回 hash 与行/字节 locator。若 continuation_required=true，必须用同一工具和 next_cursor 继续，直到 false 后才能声明读取完整",
            "parameters": {"type": "object", "properties": {"revision_id": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "line_count": {"type": "integer", "minimum": 1, "maximum": 20}, "tail_lines": {"type": "integer", "minimum": 1, "maximum": 20}, "cursor": {"type": "integer", "minimum": 0}}, "required": ["revision_id"], "additionalProperties": False},
        },
    }
    if group == "A":
        return [search, read]
    if group == "C":
        return [{"type": "function", "function": {"name": "raw_shell", "description": "只读评估上界：提交一个查询字符串并返回命中行；不能提交命令、flags 或路径", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}}]
    return [
        search,
        read,
        {"type": "function", "function": {"name": "file_list", "description": "只用于按路径、文件名或 glob 发现文件，或目录结构确实未知时查看授权 scope。内容查询、精确 token 和概念查询不要先调用本工具", "parameters": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["sources", "work", "artifacts"]}, "prefix": {"type": "string"}, "glob": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "cursor": {"type": "integer", "minimum": 0}}, "required": ["scope"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "file_grep", "description": "精确 token、日期、错误码、版本号或原句必须优先使用本工具，可对 sources/work/artifacts scope 分别调用。结果提供固定 Revision、hash、line/byte locator 和 preview；preview 仍需 file_read。若 continuation_required=true，必须用 next_cursor 继续到 false", "parameters": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["sources", "work", "artifacts"]}, "revision_ids": {"type": "array", "items": {"type": "string"}}, "pattern": {"type": "string"}, "mode": {"type": "string", "enum": ["literal", "regex"]}, "case_sensitive": {"type": "boolean"}, "before": {"type": "integer", "minimum": 0, "maximum": 3}, "after": {"type": "integer", "minimum": 0, "maximum": 3}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "cursor": {"type": "integer", "minimum": 0}}, "required": ["pattern"], "additionalProperties": False}}},
    ]


def _expected_first(task: dict[str, Any], group: str) -> str:
    """返回题型在实验组中的推荐首个 Tool"""
    if group == "A":
        return "file_search"
    if group == "C":
        return "raw_shell"
    return "file_grep" if task["type"] == "exact" else "file_search"


def _usage_dict(response: Any) -> dict[str, Any]:
    """提取不含密钥的 provider 用量摘要"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {key: getattr(usage, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


async def run_task(settings: Settings, task: dict[str, Any], group: str, max_steps: int) -> dict[str, Any]:
    """让真实模型完成一题并记录 Tool 选择和稳定 locator"""
    tools = tool_schema(group)
    allowed = {tool["function"]["name"] for tool in tools}
    public_task = {key: value for key, value in task.items() if key != "target"}
    routing_contract = (
        "题型为 exact 时，第一个 Tool 必须是 file_grep，并对可能的 sources/work/artifacts scope 检索；"
        "题型为 concept 时，第一个 Tool 必须是 file_search；"
        "题型为 mixed 时，第一个 Tool 必须是 file_search，再用 file_grep 核对 exact 字段。"
        "file_list 仅用于用户询问路径/文件名或目录结构确实未知，不能作为内容查询的第一个 Tool。"
        if group == "B"
        else "只使用当前实验组提供的 Tool 完成检索。"
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你正在参与 Q7 Research File Space Tool 实验。只调用提供的工具。"
                f"{routing_contract}"
                "file_search/file_grep preview 不能替代 file_read。"
                "任何 Observation 返回 continuation_required=true 时，必须使用同一 Tool 和 next_cursor 继续，"
                "直到 continuation_required=false 后才能声明检索完整。"
                "最终回答必须基于固定 revision_id、hash 和 line locator。"
                "不要提交 shell 命令、宿主路径、隐藏 target 或权限字段。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": public_task,
                    "要求": "完成检索并读取最终证据，最后用简短中文说明命中 Revision、hash 和行号",
                },
                ensure_ascii=False,
            ),
        },
    ]
    calls: list[str] = []
    observations: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    invalid = 0
    usage: list[dict[str, Any]] = []
    started = time.perf_counter()
    for _step in range(max_steps):
        response = await acompletion(model=settings.openai_model, api_key=settings.openai_api_key, api_base=settings.openai_api_base, custom_llm_provider="openai", messages=messages, tools=tools, tool_choice="auto", timeout=60)
        usage.append(_usage_dict(response))
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        assistant = {"role": "assistant", "content": getattr(message, "content", None)}
        if tool_calls:
            assistant["tool_calls"] = [{"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in tool_calls]
        messages.append(assistant)
        if not tool_calls:
            break
        for call in tool_calls:
            name = call.function.name
            calls.append(name)
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                invalid += 1
                args = {}
                result = {"status": "rejected", "error": "invalid_json_arguments"}
                errors.append({"tool": name, "error": result["error"]})
            else:
                if name not in allowed:
                    invalid += 1
                    result = {"status": "rejected", "error": "tool_not_allowed"}
                    errors.append({"tool": name, "error": result["error"]})
                else:
                    result = EXECUTORS[name](args)
                    if result.get("status") == "rejected":
                        invalid += 1
                        errors.append({"tool": name, "error": str(result.get("error", "rejected"))})
            observations.append({"tool": name, "args": args if isinstance(args, dict) else {}, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)})
    target = task["target"]
    evidence_success = any(observation["tool"] == "file_read" and observation["result"].get("status") == "ok" and observation["result"].get("revision_id") == target for observation in observations)
    if group == "C":
        evidence_success = any(observation["tool"] == "raw_shell" and any(hit.get("revision_id") == target for hit in observation["result"].get("hits", [])) for observation in observations)
    continuation_checks: list[bool] = []
    for index, observation in enumerate(observations):
        result = observation["result"]
        if not result.get("continuation_required") or result.get("next_cursor") is None:
            continue
        continuation_checks.append(
            any(
                later["tool"] == observation["tool"]
                and later["args"].get("cursor") == result["next_cursor"]
                for later in observations[index + 1 :]
            )
        )
    truncated = bool(continuation_checks)
    recovered = bool(continuation_checks) and all(continuation_checks)
    compact_observations = [
        {
            "tool": observation["tool"],
            "args": observation["args"],
            "status": observation["result"].get("status"),
            "truncated": observation["result"].get("truncated", False),
            "next_cursor": observation["result"].get("next_cursor"),
            "continuation_required": observation["result"].get("continuation_required", False),
            "result_revision_ids": sorted(
                {
                    item.get("revision_id")
                    for item in observation["result"].get("matches", []) + observation["result"].get("candidates", [])
                    if item.get("revision_id")
                }
            ),
        }
        for observation in observations
    ]
    return {
        "task": task,
        "group": group,
        "first_tool": calls[0] if calls else None,
        "expected_first": _expected_first(task, group),
        "first_tool_match": bool(calls) and calls[0] == _expected_first(task, group),
        "calls": calls,
        "invalid_calls": invalid,
        "errors": errors,
        "evidence_locator_success": evidence_success,
        "truncated": truncated,
        "recovered": recovered,
        "continuation_required_count": len(continuation_checks),
        "continuation_recovered_count": sum(continuation_checks),
        "observations": compact_observations,
        "usage": usage,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


async def main() -> int:
    """运行当前 .env 模型的 A/B/C 24 题实验"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--groups", default="A,B,C")
    args = parser.parse_args()
    settings = Settings()
    if not settings.openai_api_key or not settings.openai_api_base:
        print("live provider 配置不完整：需要 DEEP_RESEARCHER_OPENAI_API_KEY 和 DEEP_RESEARCHER_OPENAI_API_BASE", file=sys.stderr)
        return 2
    groups = [group.strip() for group in args.groups.split(",") if group.strip()]
    results: list[dict[str, Any]] = []
    for group in groups:
        for task in TASKS:
            result = await run_task(settings, task, group, args.max_steps)
            results.append(result)
            print(f"{group} {task['id']} first={result['first_tool']} locator={result['evidence_locator_success']} invalid={result['invalid_calls']} calls={','.join(result['calls'])}", flush=True)
    summary: dict[str, Any] = {"model": settings.openai_model, "groups": {}, "task_count": len(TASKS), "provider_live": True}
    for group in groups:
        rows = [row for row in results if row["group"] == group]
        summary["groups"][group] = {
            "locator_success": sum(row["evidence_locator_success"] for row in rows),
            "first_tool_match": sum(row["first_tool_match"] for row in rows),
            "invalid_calls": sum(row["invalid_calls"] for row in rows),
            "truncated_cases": sum(row["truncated"] for row in rows),
            "recovered_cases": sum(row["recovered"] for row in rows),
            "continuation_required": sum(row["continuation_required_count"] for row in rows),
            "continuation_recovered": sum(row["continuation_recovered_count"] for row in rows),
            "by_type": {task_type: sum(row["evidence_locator_success"] for row in rows if row["task"]["type"] == task_type) for task_type in ("exact", "concept", "mixed")},
            "latency_ms": {"p50": sorted(row["latency_ms"] for row in rows)[len(rows) // 2], "p95": sorted(row["latency_ms"] for row in rows)[max(0, int(len(rows) * 0.95) - 1)]},
            "usage": {"prompt_tokens": sum((item.get("prompt_tokens") or 0) for row in rows for item in row["usage"]), "completion_tokens": sum((item.get("completion_tokens") or 0) for row in rows for item in row["usage"])},
        }
    output = {"summary": summary, "results": results}
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", settings.openai_model)
    output_path = Path(f"/private/tmp/q7-live-provider-{safe_model}.json")
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"detail_file={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
