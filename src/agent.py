"""虚拟筛选 Agent(Lightweight LLM Agent)。

用一个 LLM(opencode go, DeepSeek 模型)编排本项目的工具,做对话式虚拟筛选:
  - 用户可以用自然语言让 Agent 跑筛选、读结果、分析 hit
  - Agent 通过 tool-calling 调用 pipeline 的工具,再基于结果生成报告

工具(tools):
  run_screening(receptor, out) : 跑一次完整虚拟筛选,返回结果 CSV 路径
  read_results(path)           : 读 top_hits.csv,返回分子+打分列表
  analyze_hit(molecule_name)   : 对特定分子给出基于对接结果的解读

这展示了"Agent 能自动编排 CADD 流水线并解释结果"的能力,
也是项目里"LLM Agent 层"的核心。
"""
import json
import os
import sys
import argparse
from pathlib import Path
import urllib.request

PLUGIN_NAME = 'CADD virtual screening domain'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = ('cadd.screen', 'cadd.results', 'cadd.analysis')


BASE_URL = 'https://opencode.ai/zen/go/v1/chat/completions'
MODEL = 'deepseek-v4-flash'
API_KEY = None  # 运行时从 config.yaml 注入

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _configured_output_path(key, override=None, output_dir=None):
    from config_loader import latest_run_dir, load_config, resolve_path
    cfg = load_config()
    output_cfg = cfg.get('output', {})
    if override is not None:
        return resolve_path(override)
    directory = resolve_path(output_dir or output_cfg.get('dir', 'output'))
    directory = latest_run_dir(directory)
    default_names = {'csv': 'top_hits.csv', 'report': 'report.md'}
    return directory / output_cfg.get(key, default_names[key])

# ---- 工具实现 ----
def tool_run_screening(receptor=None, out=None, external_dataset=None):
    from pipeline import run
    result = run(receptor, out, external_dataset=external_dataset)
    return {
        'status': 'completed',
        'rows': int(len(result)),
        'result_csv': str(_configured_output_path('csv', output_dir=out)),
        'report': str(_configured_output_path('report', output_dir=out)),
        'external_dataset': str(external_dataset) if external_dataset else None,
    }


def tool_read_results(path=None):
    import csv
    p = _configured_output_path('csv', override=path)
    if not p.exists():
        return {'status': 'missing', 'path': str(p), 'rows': []}
    rows = []
    with open(p, encoding='utf-8', newline='') as handle:
        for row in csv.DictReader(handle):
            rows.append({
                'name': row['mol_name'],
                'tag': row['tag'],
                'affinity': float(row['affinity']),
            })
    return {'status': 'ok', 'path': str(p), 'rows': rows}


def tool_analyze_hit(name, path=None):
    result = tool_read_results(path)
    if result['status'] != 'ok':
        return result
    for row in result['rows']:
        if row['name'] == name:
            affinity = row['affinity']
            strength = 'strong' if affinity <= -7 else 'medium' if affinity <= -6 else 'weak'
            return {
                'status': 'ok',
                'name': name,
                'tag': row['tag'],
                'affinity': affinity,
                'strength': strength,
                'interpretation': (
                    'prioritized hit for follow-up'
                    if row['tag'] == 'active'
                    else 'inactive control with weaker docking score'
                ),
            }
    return {'status': 'not_found', 'name': name, 'path': result['path']}


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


TOOLS = {
    'run_screening': {
        'description': 'Run the configured CADD screening pipeline.',
        'parameters': _parameters({
            'receptor': {'type': 'string'},
            'out': {'type': 'string'},
            'external_dataset': {'type': 'string'},
        }),
        'function': tool_run_screening,
    },
    'read_results': {
        'description': 'Read structured docking results from a CSV.',
        'parameters': _parameters({
            'path': {'type': 'string'},
        }),
        'function': tool_read_results,
    },
    'analyze_hit': {
        'description': 'Analyze one molecule in docking results.',
        'parameters': _parameters({
            'name': {'type': 'string'},
            'path': {'type': 'string'},
        }, required=('name',)),
        'function': tool_analyze_hit,
    },
}
def select_tools(domain='cadd'):
    if domain == 'cadd':
        try:
            from .plugin_manager import is_domain_enabled
        except ImportError:
            from plugin_manager import is_domain_enabled
        return TOOLS if is_domain_enabled('cadd') else {}
    try:
        from .domain_registry import active_tool_specs
    except ImportError:
        from domain_registry import active_tool_specs
    return {
        spec['name']: {
            'description': spec['description'],
            'parameters': spec['parameters'],
            'function': spec['function'],
        }
        for spec in active_tool_specs(None if domain == 'all' else domain)
    }


def _system_prompt(domain, tools):
    if domain == 'cadd':
        return SYSTEM_PROMPT
    names = ', '.join(sorted(tools))
    return (
        '你是生命科学研究 Agent。先理解任务，再调用确定性工具完成分析。'
        '不得编造分析结果，必须依据工具输出回答。当前可用工具: ' + names
    )


SYSTEM_PROMPT = """你是生物医药计算助手(CADD Agent)。你的工具:
- run_screening(receptor,out): 跑虚拟筛选
- read_results(path): 读结果
- analyze_hit(name): 分析某分子
请根据用户意图,先调用工具获取数据,再用自然语言解释。"""

def _completion_url(base_url):
    value = str(base_url or '').rstrip('/')
    if value.endswith('/chat/completions'):
        return value
    return value + '/v1/chat/completions'


def load_llm_config():
    from config_loader import load_config
    try:
        project_cfg = load_config()
    except (FileNotFoundError, OSError, ValueError):
        project_cfg = {}
    llm_cfg = project_cfg.get('llm', {})
    base_url = _completion_url(llm_cfg.get('base_url', BASE_URL))
    model = llm_cfg.get('model', MODEL)
    api_key = os.environ.get('CADD_API_KEY') or os.environ.get('OPENAI_API_KEY') or llm_cfg.get('api_key')
    external_path = Path.home() / '.mewcode' / 'config.yaml'
    if not api_key and external_path.exists():
        try:
            import yaml
            external_cfg = yaml.safe_load(external_path.read_text(encoding='utf-8')) or {}
            api_key = external_cfg.get('api_key')
        except (OSError, ValueError):
            pass
    return base_url, model, api_key

def call_llm(messages, tools=None):
    if not API_KEY:
        raise RuntimeError('未配置 CADD_API_KEY 或 OPENAI_API_KEY，无法调用 LLM')
    active_tools = TOOLS if tools is None else tools
    payload = {
        'model': MODEL,
        'messages': messages,
        'tools': [{
            'type': 'function',
            'function': {
                'name': name,
                'description': spec['description'],
                'parameters': spec['parameters'],
            },
        } for name, spec in active_tools.items()],
        'tool_choice': 'auto',
    }
    req = urllib.request.Request(
        BASE_URL, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {API_KEY}',
                 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def run_tool(name, args_str):
    """执行工具调用,返回结果(JSON 可序列化)。"""
    try:
        from .plugin_manager import is_domain_enabled
    except ImportError:
        from plugin_manager import is_domain_enabled
    if not is_domain_enabled('cadd'):
        return {'status': 'error', 'plugin': 'cadd', 'error': 'plugin domain is disabled: cadd'}
    spec = TOOLS.get(name)
    if spec is None:
        return {'status': 'error', 'error': f'unknown tool: {name}'}
    try:
        args = json.loads(args_str) if args_str else {}
        return spec['function'](**args) if isinstance(args, dict) else spec['function'](args)
    except Exception as e:
        return f'工具执行失败: {e}'


def chat(domain='cadd'):
    global BASE_URL, MODEL, API_KEY
    BASE_URL, MODEL, API_KEY = load_llm_config()
    active_tools = select_tools(domain)
    execute_tool = run_tool
    if domain != 'cadd':
        from domain_registry import run_tool as execute_tool
    if not API_KEY:
        print('[Agent] 未配置 CADD_API_KEY 或 OPENAI_API_KEY，已跳过 LLM 对话')
        return
    messages = [{'role': 'system', 'content': _system_prompt(domain, active_tools)}]
    print('【虚拟筛选 Agent】输入指令(如: 跑筛选 / 读结果 / 分析 AQ4),输入 exit 退出')
    while True:
        try:
            user = input('\n> ').strip()
        except EOFError:
            break
        if user.lower() in ('exit', 'quit', 'q'):
            break
        messages.append({'role': 'user', 'content': user})
        try:
            resp = call_llm(messages, active_tools)
            msg = resp['choices'][0]['message']
            if msg.get('tool_calls'):
                messages.append({
                    'role': 'assistant',
                    'content': msg.get('content') or '',
                    'tool_calls': msg['tool_calls'],
                })
                # 执行工具
                for tc in msg['tool_calls']:
                    result = execute_tool(tc['function']['name'], tc['function'].get('arguments', '{}'))
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'],
                                     'content': json.dumps(result, ensure_ascii=False)})
                    print(f'  [调用工具] {tc["function"]["name"]} -> 完成')
                # 让 LLM 基于工具结果生成最终回答
                final = call_llm(messages, active_tools)
                final_message = final['choices'][0]['message']
                messages.append(final_message)
                print('\n' + (final_message.get('content') or ''))
            else:
                messages.append({'role': 'assistant', 'content': msg.get('content', '')})
                print('\n' + (msg.get('content') or ''))
        except Exception as e:
            print(f'\n[Agent 错误] {e}')


def offline_report(path=None, out=None):
    """离线分析:不依赖 LLM,直接生成虚拟筛选报告。"""
    from report import generate_report
    path = _configured_output_path('csv', override=path)
    out = _configured_output_path('report', override=out)
    txt = generate_report(path, out)
    if txt:
        print('\n' + txt[:800])
    return txt


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='虚拟筛选 Agent/分析')
    ap.add_argument('--report', action='store_true', help='离线生成报告(无需LLM key)')
    ap.add_argument('--chat', action='store_true', help='LLM 对话模式(需可用 API key)')
    ap.add_argument('--domain', choices=('cadd', 'omics', 'all'), default='cadd')
    args = ap.parse_args()
    if args.report:
        offline_report()
    elif args.chat:
        chat(args.domain)
    else:
        ap.print_help()
        print('\n提示: key 可用时用 --chat 开启 LLM 对话;否则用 --report 离线分析。')
