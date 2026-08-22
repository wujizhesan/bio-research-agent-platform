"""Streamlit chat adapter for the shared CADD and omics Agent runtime."""
import json
import shlex
from pathlib import Path

try:
    from . import agent
    from .domain_registry import run_tool as run_domain_tool
    from .domain_registry import active_tool_specs, active_domains, active_domain_catalog
    from .workflow_runner import run_workflow
    from .api_server import route_request
except ImportError:
    import agent
    from domain_registry import run_tool as run_domain_tool
    from domain_registry import active_tool_specs, active_domains, active_domain_catalog
    from workflow_runner import run_workflow
    from api_server import route_request


def configure():
    agent.BASE_URL, agent.MODEL, agent.API_KEY = agent.load_llm_config()
    return {
        "model": agent.MODEL,
        "base_url": agent.BASE_URL,
        "configured": bool(agent.API_KEY),
    }


def system_message(domain):
    return {"role": "system", "content": agent._system_prompt(domain, agent.select_tools(domain))}


def execute_tool(name, arguments, domain):
    if domain == "cadd" and name in agent.TOOLS:
        return run_domain_tool(f"cadd_{name}", arguments)
    return run_domain_tool(name, arguments)


def help_text():
    domains = '|'.join((*active_domains(), 'all'))
    return (
        f'支持 /domain {domains} 切换领域、/tools 查看工具、'
        '/plugins 查看插件状态、/run <tool> <JSON> 直接调用工具、/workflow <path> 运行工作流。'
    )

def tools_text(domain):
    selected = None if domain == "all" else domain
    lines = [f"当前领域：`{domain}`", ""]
    for spec in active_tool_specs(selected):
        lines.append(f"- `{spec['name']}`：{spec['description']}")
    return "\n".join(lines)


def _workflow_command(parts, project_root):
    if len(parts) < 2:
        return "用法：`/workflow examples/workflows/rnaseq.yaml [manifest.json]`"
    workflow_path = Path(parts[1])
    if not workflow_path.is_absolute():
        workflow_path = project_root / workflow_path
    manifest_path = Path(parts[2]) if len(parts) > 2 else project_root / "output" / "streamlit_workflow_manifest.json"
    if not manifest_path.is_absolute():
        manifest_path = project_root / manifest_path
    result = run_workflow(workflow_path, manifest_path)
    return f"工作流结果：\n```json\n{json.dumps(result, ensure_ascii=False, indent=2, default=str)}\n```"


def handle_command(text, domain, project_root):
    if text.startswith("/help"):
        return {"answer": help_text(), "domain": domain, "reset": False, "traces": []}
    if text.startswith("/domain"):
        parts = shlex.split(text)
        selected = parts[1] if len(parts) > 1 else "all"
        if selected not in {*active_domains(), "all"}:
            return {"answer": f"可选领域：{'、'.join((*active_domains(), 'all'))}", "domain": domain, "reset": False, "traces": []}
        return {
            "answer": f"已切换到 `{selected}` 领域，Agent 上下文已重置。",
            "domain": selected,
            "reset": True,
            "traces": [],
        }
    if text.startswith("/plugins"):
        return {
            "answer": json.dumps(active_domain_catalog(), ensure_ascii=False, indent=2),
            "domain": domain,
            "reset": False,
            "traces": [],
        }
    if text.startswith("/tools"):
        return {"answer": tools_text(domain), "domain": domain, "reset": False, "traces": []}
    if text.startswith("/submit"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            return {"answer": "用法：/submit <tool> <JSON>", "domain": domain, "reset": False, "traces": []}
        try:
            arguments = json.loads(parts[2]) if len(parts) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
            status, result = route_request("POST", "/jobs", {"tool": parts[1], "arguments": arguments})
        except (json.JSONDecodeError, ValueError) as exc:
            status, result = 400, {"status": "error", "error": str(exc)}
            arguments = {}
        return {
            "answer": f"后台任务已提交（HTTP {status}），使用 /job <job_id> 查询。",
            "domain": domain,
            "reset": False,
            "traces": [{"name": "submit:" + parts[1], "arguments": arguments, "result": result}],
        }
    if text.startswith("/retry"):
        parts = shlex.split(text)
        if len(parts) < 2:
            return {"answer": "用法：/retry <job_id>", "domain": domain, "reset": False, "traces": []}
        status, result = route_request("POST", "/jobs/" + parts[1] + "/retry")
        return {"answer": json.dumps(result, ensure_ascii=False, indent=2), "domain": domain, "reset": False, "traces": []}
    if text.startswith("/jobs"):
        parts = shlex.split(text)
        query = "/jobs" + ("?limit=" + parts[1] if len(parts) > 1 else "")
        status, result = route_request("GET", query)
        return {"answer": json.dumps(result, ensure_ascii=False, indent=2), "domain": domain, "reset": False, "traces": []}
    if text.startswith("/job"):
        parts = shlex.split(text)
        if len(parts) < 2:
            return {"answer": "用法：/job <job_id>", "domain": domain, "reset": False, "traces": []}
        status, result = route_request("GET", "/jobs/" + parts[1])
        return {"answer": json.dumps(result, ensure_ascii=False, indent=2), "domain": domain, "reset": False, "traces": []}
    if text.startswith("/run"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            return {"answer": "用法：`/run omics_search_gene_evidence {\"gene_ids\": [\"TP53\"]}`", "domain": domain, "reset": False, "traces": []}
        try:
            arguments = json.loads(parts[2]) if len(parts) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
            result = execute_tool(parts[1], arguments, domain)
        except (json.JSONDecodeError, ValueError) as exc:
            result = {"status": "error", "error": str(exc)}
            arguments = {}
        return {
            "answer": "工具已执行，详情见下方。",
            "domain": domain,
            "reset": False,
            "traces": [{"name": parts[1], "arguments": arguments, "result": result}],
        }
    if text.startswith("/preset"):
        parts = shlex.split(text)
        if len(parts) < 2:
            return {"answer": "用法：`/preset bgi_research_demo`", "domain": domain, "reset": False, "traces": []}
        preset = parts[1]
        output_dir = project_root / "output" / preset
        result = run_domain_tool("research_run_preset", {
            "preset": preset,
            "dry_run": False,
            "output_path": str(output_dir / "research_manifest.json"),
            "report_path": str(output_dir / "research_report.md"),
        })
        return {
            "answer": f"研究预设 `{preset}` 已执行，结果见下方。",
            "domain": domain,
            "reset": False,
            "traces": [{"name": "research_run_preset", "arguments": {"preset": preset}, "result": result}],
        }
    if text.startswith("/workflow"):
        try:
            answer = _workflow_command(shlex.split(text), project_root)
        except Exception as exc:
            answer = f"工作流执行失败：{exc}"
        return {"answer": answer, "domain": domain, "reset": False, "traces": []}
    return None


def run_agent_turn(text, domain, context):
    configure()
    context.append({"role": "user", "content": text})
    if not agent.API_KEY:
        return (
            "当前未配置 LLM API Key。你仍可以使用 `/help`、`/tools`、`/run` 和 `/workflow` 操作本地工具；"
            "配置 `CADD_API_KEY` 或 `OPENAI_API_KEY` 后即可使用自然语言 Agent。",
            [],
        )
    active_tools = agent.select_tools(domain)
    response = agent.call_llm(context, active_tools)
    message = response["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    context.append({
        "role": "assistant",
        "content": message.get("content") or "",
        **({"tool_calls": tool_calls} if tool_calls else {}),
    })
    traces = []
    for tool_call in tool_calls:
        name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            arguments = {}
            result = {"status": "error", "error": f"invalid tool arguments: {exc}"}
        else:
            result = execute_tool(name, arguments, domain)
        traces.append({"name": name, "arguments": arguments, "result": result})
        context.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps(result, ensure_ascii=False, default=str),
        })
    if tool_calls:
        final = agent.call_llm(context, active_tools)
        final_message = final["choices"][0]["message"]
        context.append(final_message)
        answer = final_message.get("content") or "工具已执行，但模型没有返回文字解释。"
    else:
        answer = message.get("content") or "模型没有返回文字内容。"
    return answer, traces
