"""Chainlit conversational UI for the pluggable CADD and omics Agent."""
import asyncio
import json
import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import chainlit as cl
except ImportError as exc:
    raise SystemExit("Chainlit UI requires: pip install -r requirements-ui.txt") from exc

from src import agent
from src.domain_registry import run_tool as run_domain_tool
from src.domain_registry import active_tool_specs, active_domains, active_domain_catalog
from src.workflow_runner import run_workflow
from src.api_server import route_request


def _json_text(value, limit=8000):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + "\n..."


def _domain():
    return cl.user_session.get("domain") or "all"


def _active_tools(domain):
    return agent.select_tools(domain)


def _execute_tool(name, arguments, domain):
    if domain == "cadd" and name in agent.TOOLS:
        return run_domain_tool(f"cadd_{name}", arguments)
    return run_domain_tool(name, arguments)


def _tool_catalog(domain):
    selected = None if domain == "all" else domain
    return active_tool_specs(selected)


def _help_text():
    domains = '|'.join((*active_domains(), 'all'))
    return (
        '## Bioinformatics Agent\n'
        f'支持 /domain {domains} 切换工具域\n'
        '- /plugins 查看插件状态\n        - /tools 查看当前域工具\n'
        '- /run <tool> <JSON> 直接执行工具\n'
        '- /workflow <YAML/JSON路径> [manifest路径] 运行工作流\n'
        '- /help 查看帮助'
    )

def _configure():
    agent.BASE_URL, agent.MODEL, agent.API_KEY = agent.load_llm_config()
    return bool(agent.API_KEY)


async def _send_tool_result(name, arguments, result):
    await cl.Message(
        content=f"工具 {name} 已执行：\n{_json_text({'arguments': arguments, 'result': result})}"
    ).send()


async def _llm_reply(text):
    if not _configure():
        await cl.Message(content="未配置 CADD_API_KEY 或 OPENAI_API_KEY，无法启动 LLM 对话。").send()
        return
    domain = _domain()
    messages = cl.user_session.get("messages") or [{
        "role": "system",
        "content": agent._system_prompt(domain, _active_tools(domain)),
    }]
    messages.append({"role": "user", "content": text})
    active_tools = _active_tools(domain)
    try:
        response = await asyncio.to_thread(agent.call_llm, messages, active_tools)
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            **({"tool_calls": tool_calls} if tool_calls else {}),
        })
        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            try:
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                arguments = {}
                result = {"status": "error", "error": f"invalid tool arguments: {exc}"}
            else:
                result = await asyncio.to_thread(_execute_tool, name, arguments, domain)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        if tool_calls:
            final = await asyncio.to_thread(agent.call_llm, messages, active_tools)
            final_message = final["choices"][0]["message"]
            messages.append(final_message)
            answer = final_message.get("content") or "工具已执行，但模型没有返回文字说明。"
        else:
            answer = message.get("content") or "模型没有返回文字内容。"
        cl.user_session.set("messages", messages)
        await cl.Message(content=answer).send()
    except Exception as exc:
        await cl.Message(content=f"LLM 对话失败：{exc}").send()


@cl.on_chat_start
async def on_chat_start():
    _configure()
    domain = "all"
    cl.user_session.set("domain", domain)
    cl.user_session.set("messages", [{
        "role": "system",
        "content": agent._system_prompt(domain, _active_tools(domain)),
    }])
    await cl.Message(content="Bioinformatics Agent 已就绪。可使用 /help 查看命令。").send()
async def on_message(message: cl.Message):
    text = (message.content or "").strip()
    if not text:
        return
    if text.startswith("/help"):
        await cl.Message(content=_help_text()).send()
        return
    if text.startswith("/domain"):
        parts = shlex.split(text)
        domain = parts[1] if len(parts) > 1 else "all"
        if domain not in {*active_domains(), "all"}:
            await cl.Message(content=f"可选域：{'、'.join((*active_domains(), 'all'))}").send()
            return
        cl.user_session.set("domain", domain)
        cl.user_session.set("messages", [{
            "role": "system",
            "content": agent._system_prompt(domain, _active_tools(domain)),
        }])
        await cl.Message(content=f"已切换到 `{domain}` 域，工具上下文已重置。").send()
        return
    if text.startswith("/plugins"):
        await cl.Message(content=_json_text(active_domain_catalog())).send()
        return
    if text.startswith("/tools"):
        lines = [f"当前域：`{_domain()}`", ""]
        for spec in _tool_catalog(_domain()):
            lines.append(f"- `{spec['name']}`：{spec['description']}")
        await cl.Message(content="\n".join(lines)).send()
        return
    if text.startswith("/submit"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await cl.Message(content="用法：/submit <tool> <JSON>").send()
            return
        try:
            arguments = json.loads(parts[2]) if len(parts) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
            status, result = await asyncio.to_thread(
                route_request,
                "POST",
                "/jobs",
                {"tool": parts[1], "arguments": arguments},
            )
        except (json.JSONDecodeError, ValueError) as exc:
            status, result = 400, {"status": "error", "error": str(exc)}
        await cl.Message(content=f"后台任务已提交（HTTP {status}）：\n{_json_text(result)}").send()
        return
    if text.startswith("/retry"):
        parts = shlex.split(text)
        if len(parts) < 2:
            await cl.Message(content="用法：/retry <job_id>").send()
            return
        status, result = await asyncio.to_thread(route_request, "POST", "/jobs/" + parts[1] + "/retry")
        await cl.Message(content=f"重试任务（HTTP {status}）：\n{_json_text(result)}").send()
        return
    if text.startswith("/jobs"):
        parts = shlex.split(text)
        query = "/jobs" + ("?limit=" + parts[1] if len(parts) > 1 else "")
        status, result = await asyncio.to_thread(route_request, "GET", query)
        await cl.Message(content=f"后台任务列表（HTTP {status}）：\n{_json_text(result)}").send()
        return
    if text.startswith("/job"):
        parts = shlex.split(text)
        if len(parts) < 2:
            await cl.Message(content="用法：/job <job_id>").send()
            return
        status, result = await asyncio.to_thread(route_request, "GET", "/jobs/" + parts[1])
        await cl.Message(content=f"后台任务详情（HTTP {status}）：\n{_json_text(result)}").send()
        return
    if text.startswith("/run"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await cl.Message(content="用法：`/run omics_run_analysis {\"expression\": \"...\"}`").send()
            return
        try:
            arguments = json.loads(parts[2]) if len(parts) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            await cl.Message(content=f"参数解析失败：{exc}").send()
            return
        name = parts[1]
        result = await asyncio.to_thread(_execute_tool, name, arguments, _domain())
        await _send_tool_result(name, arguments, result)
        return
    if text.startswith("/preset"):
        parts = shlex.split(text)
        if len(parts) < 2:
            await cl.Message(content="用法：`/preset bgi_research_demo`").send()
            return
        preset = parts[1]
        output_dir = PROJECT_ROOT / "output" / preset
        result = await asyncio.to_thread(run_domain_tool, "research_run_preset", {
            "preset": preset,
            "dry_run": False,
            "output_path": str(output_dir / "research_manifest.json"),
            "report_path": str(output_dir / "research_report.md"),
        })
        await cl.Message(content=f"研究预设 `{preset}` 已执行：\n```json\n{_json_text(result)}\n```").send()
        return
    if text.startswith("/workflow"):
        parts = shlex.split(text)
        if len(parts) < 2:
            await cl.Message(content="用法：`/workflow examples/workflows/rnaseq.yaml [manifest.json]`").send()
            return
        workflow_path = Path(parts[1])
        if not workflow_path.is_absolute():
            workflow_path = PROJECT_ROOT / workflow_path
        manifest_path = Path(parts[2]) if len(parts) > 2 else PROJECT_ROOT / "output" / "chainlit_workflow_manifest.json"
        if not manifest_path.is_absolute():
            manifest_path = PROJECT_ROOT / manifest_path
        result = await asyncio.to_thread(run_workflow, workflow_path, manifest_path)
        await cl.Message(content=f"工作流结果：\n```json\n{_json_text(result)}\n```").send()
        return
    await _llm_reply(text)
