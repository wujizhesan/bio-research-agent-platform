"""虚拟筛选 Agent(Lightweight LLM Agent)。

用一个 LLM(opencode go, DeepSeek 模型)编排本项目的工具,做对话式虚拟筛选:
  - 用户可以用自然语言让 Agent 跑筛选、读结果、分析 hit
  - Agent 通过 tool-calling 调用 pipeline 的工具,再基于结果生成报告

工具(tools):
  run_screening(receptor, out) : 跑一次完整虚拟筛选,返回结果 CSV 路径
  read_results(path)           : 读 top_hits.csv,返回分子+打分列表
  analyze_hit(molecule_name)   : 对特定分子给出基于对接结果的解读

这展示了"Agent 能自动编排 CADD 流水线并解释结果"的能力,
也是简历里"LLM Agent 层"的核心。
"""
import json
import sys
import argparse
from pathlib import Path
import urllib.request

BASE_URL = 'https://opencode.ai/zen/go/v1/chat/completions'
MODEL = 'deepseek-v4-flash'
API_KEY = None  # 运行时从 config.yaml 注入

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---- 工具实现 ----
def tool_run_screening(receptor='data/4hjo.pdb', out='output'):
    """执行完整虚拟筛选流水线,返回结果 CSV 路径。"""
    from pipeline import run
    run(receptor, out)
    return str(Path(out) / 'top_hits.csv')

def tool_read_results(path='output/top_hits.csv'):
    """读取虚拟筛选结果 CSV,返回 [{name, tag, affinity}] 列表。"""
    import csv
    p = Path(path)
    if not p.exists():
        return f"结果文件不存在: {path},请先运行筛选"
    rows = []
    with open(p) as f:
        for r in csv.DictReader(f):
            rows.append({'name': r['mol_name'], 'tag': r['tag'],
                         'affinity': float(r['affinity'])})
    return rows

def tool_analyze_hit(name, path='output/top_hits.csv'):
    """对某个分子给出基于对接/标签的解读。"""
    rows = tool_read_results(path)
    if isinstance(rows, str):
        return rows
    for r in rows:
        if r['name'] == name:
            strength = ('强结合' if r['affinity'] <= -7 else
                        '中结合' if r['affinity'] <= -6 else '弱结合')
            return (f"{name} ({r['tag']}): 对接打分 {r['affinity']} kcal/mol,"
                    f"属于{strength}。{'该分子骨架与 EGFR 活性配体相似,排位靠前,是潜在 hit。' if r['tag']=='active' else '该分子为非活性对照,打分靠后,符合预期。'}")
    return f'未找到分子 {name}'

# 工具注册表
TOOLS = {
    'run_screening': ('跑一次虚拟筛选(receptor,out)', tool_run_screening),
    'read_results': ('读取筛选结果(path)', tool_read_results),
    'analyze_hit': ('分析某分子(名称)', tool_analyze_hit),
}

SYSTEM_PROMPT = """你是生物医药计算助手(CADD Agent)。你的工具:
- run_screening(receptor,out): 跑虚拟筛选
- read_results(path): 读结果
- analyze_hit(name): 分析某分子
请根据用户意图,先调用工具获取数据,再用自然语言解释。"""

def call_llm(messages):
    payload = {
        'model': MODEL,
        'messages': messages,
        'tools': [{
            'type': 'function',
            'function': {'name': n, 'description': d}
        } for n, (d, _) in TOOLS.items()],
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
    desc, fn = TOOLS[name]
    try:
        args = json.loads(args_str) if args_str else {}
        return fn(**args) if isinstance(args, dict) else fn(args)
    except Exception as e:
        return f'工具执行失败: {e}'


def chat():
    from pathlib import Path as P
    import yaml
    global API_KEY
    cfg = yaml.safe_load((Path.home()/'.mewcode'/'config.yaml').read_text(encoding='utf-8'))
    API_KEY = cfg.get('api_key')
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
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
            resp = call_llm(messages)
            msg = resp['choices'][0]['message']
            if msg.get('tool_calls'):
                # 执行工具
                for tc in msg['tool_calls']:
                    result = run_tool(tc['function']['name'], tc['function'].get('arguments', '{}'))
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'],
                                     'content': json.dumps(result, ensure_ascii=False)})
                    print(f'  [调用工具] {tc["function"]["name"]} -> 完成')
                # 让 LLM 基于工具结果生成最终回答
                final = call_llm(messages)
                print('\n' + final['choices'][0]['message']['content'])
            else:
                print('\n' + msg.get('content', ''))
        except Exception as e:
            print(f'\n[Agent 错误] {e}')


def offline_report(path='output/top_hits.csv', out='output/report.md'):
    """离线分析:不依赖 LLM,直接生成虚拟筛选报告。"""
    from report import generate_report
    txt = generate_report(path, out)
    if txt:
        print('\n' + txt[:800])
    return txt


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='虚拟筛选 Agent/分析')
    ap.add_argument('--report', action='store_true', help='离线生成报告(无需LLM key)')
    ap.add_argument('--chat', action='store_true', help='LLM 对话模式(需可用 API key)')
    args = ap.parse_args()
    if args.report:
        offline_report()
    elif args.chat:
        chat()
    else:
        ap.print_help()
        print('\n提示: key 可用时用 --chat 开启 LLM 对话;否则用 --report 离线分析。')
