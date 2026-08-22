"""CADD 虚拟筛选结果可视化页面。"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src.config_loader import latest_run_dir
from src.streamlit_chat import configure, handle_command, run_agent_turn, system_message
from src.domain_registry import active_domains, domain_catalog
from src.api_server import list_run_manifests
from src.plugin_manager import PluginManager

BASE = Path(__file__).resolve().parent
OUTPUT_ROOT = BASE / 'output'
OUTPUT = latest_run_dir(OUTPUT_ROOT)
PLUGIN_MANAGER = PluginManager(state_path=OUTPUT_ROOT / 'plugin_state.json')


def file_signature(path):
    try:
        return str(path), path.stat().st_mtime_ns
    except OSError:
        return str(path), 0


def format_metric(value):
    return 'n/a' if value is None else f'{value:.2f}'


def submit_agent_prompt(prompt):
    st.session_state.agent_display.append({'role': 'user', 'content': prompt})
    command = handle_command(prompt, st.session_state.agent_domain, BASE)
    if command is not None:
        if command['reset']:
            st.session_state.agent_domain = command['domain']
            st.session_state.agent_context = [system_message(command['domain'])]
        else:
            st.session_state.agent_domain = command['domain']
        answer = command['answer']
        traces = command['traces']
    else:
        answer, traces = run_agent_turn(prompt, st.session_state.agent_domain, st.session_state.agent_context)
    st.session_state.agent_display.append({'role': 'assistant', 'content': answer, 'traces': traces})

@st.cache_data
def load_docking(path_text, stamp):
    p = Path(path_text)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
        required = {'mol_name', 'tag', 'affinity'}
        if not required.issubset(df.columns):
            return None
        return df.sort_values('affinity')
    except (OSError, ValueError, pd.errors.ParserError):
        return None


@st.cache_data
def load_ml(path_text, stamp):
    p = Path(path_text)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
        required = {'mol_name', 'tag', 'ml_prob'}
        if not required.issubset(df.columns):
            return None
        return df.sort_values('ml_prob', ascending=False)
    except (OSError, ValueError, pd.errors.ParserError):
        return None


@st.cache_data
def load_ml_metrics(path_text, stamp):
    p = Path(path_text)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
@st.cache_data
def load_manifest(path_text, stamp):
    p = Path(path_text)
    if not p.exists():
        return None
    try:
        value = json.loads(p.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

st.set_page_config(page_title='CADD 虚拟筛选结果', layout='wide')
st.markdown("""
<style>
:root {
  --wb-blue-950: #172554;
  --wb-blue-900: #1e3a8a;
  --wb-blue-700: #1d4ed8;
  --wb-blue-100: #dbeafe;
  --wb-amber-700: #b45309;
  --wb-slate-950: #0f172a;
  --wb-slate-700: #334155;
  --wb-slate-500: #64748b;
  --wb-slate-200: #e2e8f0;
  --wb-slate-100: #f1f5f9;
  --wb-surface: #ffffff;
}
[data-testid="stAppViewContainer"] {
  background: #f8fafc;
}
[data-testid="stHeader"] {
  background: rgba(248, 250, 252, 0.88);
}
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #0f172a 0%, #172554 100%);
  border-right: 1px solid rgba(148, 163, 184, 0.18);
}
[data-testid="stSidebar"] * {
  color: #e2e8f0;
}
[data-testid="stSidebar"] button {
  border-color: rgba(226, 232, 240, 0.24);
  border-radius: 10px;
  transition: border-color 180ms ease, background 180ms ease, transform 180ms ease;
}
[data-testid="stSidebar"] button:hover {
  border-color: #93c5fd;
  background: rgba(59, 130, 246, 0.2);
  transform: translateY(-1px);
}
.block-container {
  max-width: 1480px;
  padding-top: 2rem;
  padding-bottom: 4rem;
}
.hero-shell {
  position: relative;
  overflow: hidden;
  margin: 0 0 1.5rem;
  padding: 2rem 2.2rem;
  border: 1px solid #bfdbfe;
  border-radius: 24px;
  background: linear-gradient(135deg, #ffffff 0%, #eff6ff 58%, #dbeafe 100%);
  box-shadow: 0 18px 42px rgba(30, 64, 175, 0.11);
}
.hero-shell::after {
  position: absolute;
  right: -5rem;
  top: -6rem;
  width: 18rem;
  height: 18rem;
  border-radius: 999px;
  background: rgba(59, 130, 246, 0.13);
  content: "";
}
.hero-content {
  position: relative;
  z-index: 1;
}
.hero-eyebrow {
  margin-bottom: 0.65rem;
  color: var(--wb-blue-700);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.16em;
}
.hero-title {
  margin: 0;
  color: var(--wb-blue-950);
  font-size: clamp(2rem, 4vw, 3.35rem);
  font-weight: 750;
  letter-spacing: -0.055em;
  line-height: 1.05;
}
.hero-subtitle {
  max-width: 47rem;
  margin: 0.9rem 0 1.15rem;
  color: var(--wb-slate-700);
  font-size: 1rem;
  line-height: 1.65;
}
.hero-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
}
.hero-tag {
  display: inline-flex;
  align-items: center;
  min-height: 2rem;
  padding: 0.35rem 0.7rem;
  border: 1px solid #bfdbfe;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.72);
  color: var(--wb-blue-900);
  font-size: 0.78rem;
  font-weight: 600;
}
.stMetric {
  min-height: 6.2rem;
  padding: 0.95rem 1.05rem;
  border: 1px solid var(--wb-slate-200);
  border-radius: 16px;
  background: var(--wb-surface);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.045);
}
[data-testid="stMetricLabel"] {
  color: var(--wb-slate-500);
}
[data-testid="stMetricValue"] {
  color: var(--wb-blue-900);
  letter-spacing: -0.035em;
}
[data-testid="stExpander"] {
  border: 1px solid var(--wb-slate-200);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.72);
}
[data-testid="stDataFrame"] {
  overflow: hidden;
  border: 1px solid var(--wb-slate-200);
  border-radius: 14px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.035);
}
[data-testid="stChatMessage"] {
  margin: 0.5rem 0;
  border: 1px solid var(--wb-slate-200);
  border-radius: 14px;
  background: var(--wb-surface);
}
[data-testid="stChatInput"] {
  border-color: #93c5fd;
  border-radius: 14px;
}
button:focus-visible, [data-baseweb="select"]:focus-within, textarea:focus-visible {
  outline: 3px solid rgba(59, 130, 246, 0.35);
  outline-offset: 2px;
}
h1, h2, h3 {
  color: var(--wb-blue-950);
  letter-spacing: -0.025em;
}
h2 {
  margin-top: 2rem;
}
@media (max-width: 768px) {
  .block-container {
    padding: 1rem 0.75rem 3rem;
  }
  .hero-shell {
    padding: 1.45rem 1.2rem;
    border-radius: 18px;
  }
  .hero-title {
    font-size: 2rem;
  }
  .hero-subtitle {
    font-size: 0.92rem;
  }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header('运行控制')
    if st.button('刷新结果', type='primary', width='stretch'):
        st.cache_data.clear()
        st.rerun()
    st.caption('流水线重跑后点击刷新，或等待文件修改时间自动触发缓存更新。')

st.markdown("""
<div class="hero-shell">
  <div class="hero-content">
    <div class="hero-eyebrow">LIFE SCIENCE · PLUGGABLE AGENT PLATFORM</div>
    <div class="hero-title">Bioinformatics Research Workbench</div>
    <div class="hero-subtitle">把 CADD、组学、证据检索和序列设计组合成可追溯的科研工作流。</div>
    <div class="hero-tags">
      <span class="hero-tag">Tool contracts</span>
      <span class="hero-tag">MCP + HTTP API</span>
      <span class="hero-tag">Traceable workflows</span>
      <span class="hero-tag">Evidence-aware</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)
docking_path = OUTPUT / 'top_hits.csv'
ml_path = OUTPUT / 'ml_scores.csv'
metrics_path = OUTPUT / 'ml_metrics.json'
manifest_path = OUTPUT / 'run_manifest.json'
docking = load_docking(*file_signature(docking_path))
ml = load_ml(*file_signature(ml_path))
ml_metrics = load_ml_metrics(*file_signature(metrics_path))
manifest = load_manifest(*file_signature(manifest_path))

if manifest:
    status = manifest.get('status', 'unknown')
    status_text = '已完成' if status == 'completed' else '完成但有失败' if status == 'completed_with_failures' else '运行中' if status == 'running' else status
    status_cols = st.columns(4)
    status_cols[0].metric('最近运行', status_text)
    status_cols[1].metric('输入配体', manifest.get('ligand_count', 'n/a'))
    status_cols[2].metric('成功对接', manifest.get('successful_ligands', 'n/a'))
    status_cols[3].metric('失败配体', len(manifest.get('failed_ligands', [])))
    failed = manifest.get('failed_ligands', [])
    if failed:
        st.warning('失败配体: ' + ', '.join(str(name) for name in failed))
else:
    st.info('当前结果目录没有运行清单，以下结果可能来自较早的运行版本。')

recent_runs = list_run_manifests(OUTPUT_ROOT, limit=10)
plugin_catalog = PLUGIN_MANAGER.list()
available_plugins = sum(item.get('status') == 'available' for item in plugin_catalog)
enabled_plugins = sum(item.get('enabled', True) for item in plugin_catalog)
tool_count = sum(item.get('tool_count', 0) for item in plugin_catalog if item.get('enabled', True))
st.subheader('工作台概览')
overview_cols = st.columns(4)
overview_cols[0].metric('可用插件', f'{available_plugins}/{len(plugin_catalog)}')
overview_cols[1].metric('已启用插件', enabled_plugins)
overview_cols[2].metric('可调用工具', tool_count)
overview_cols[3].metric('最近运行', len(recent_runs))
if recent_runs:
    st.subheader('最近运行')
    st.dataframe(pd.DataFrame([
        {
            'run_id': item['run_id'],
            'workflow': item['workflow'] or 'unknown',
            'status': item['status'],
            'completed_steps': item['completed_steps'],
            'failed_steps': item['failed_steps'],
            'report_path': item['report_path'],
        }
        for item in recent_runs
    ]), width='stretch', hide_index=True)
st.header('1. 对接打分（AutoDock Vina）')
if docking is None:
    st.warning('未找到或无法解析 output/top_hits.csv，请先运行 python src/pipeline.py')
else:
    summary_cols = st.columns(3)
    summary_cols[0].metric('分子总数', len(docking))
    summary_cols[1].metric('活性对照', int((docking['tag'] == 'active').sum()))
    summary_cols[2].metric('Top hit', docking.iloc[0]['mol_name'])

    show = docking.copy()
    tag_filter = st.radio('显示类型', ['全部', '活性(active)', '非活性(inactive)'], horizontal=True)
    if tag_filter == '活性(active)':
        show = show[show['tag'] == 'active']
    elif tag_filter == '非活性(inactive)':
        show = show[show['tag'] == 'inactive']
    st.dataframe(show, width='stretch')
    st.download_button(
        '下载对接结果 CSV',
        docking.to_csv(index=False).encode('utf-8-sig'),
        'top_hits.csv',
        'text/csv',
    )

    plot_path = OUTPUT / 'vs_plot.png'
    if plot_path.exists():
        st.image(str(plot_path), caption='对接打分排序（红=活性, 灰=非活性）')

st.header('2. ML 活性预测（随机森林）')
if ml is None:
    st.info('未找到或无法解析 ml_scores.csv，可用 python src/ml_predictor.py 生成')
else:
    if ml_metrics:
        metric_cols = st.columns(4)
        metric_cols[0].metric('Balanced accuracy', format_metric(ml_metrics.get('balanced_accuracy')))
        metric_cols[1].metric('ROC-AUC', format_metric(ml_metrics.get('roc_auc')))
        metric_cols[2].metric('PR-AUC', format_metric(ml_metrics.get('pr_auc')))
        enrichment = ml_metrics.get('top30_enrichment')
        lift = format_metric(ml_metrics.get('pr_auc_lift'))
        st.caption(
            f'PR-AUC baseline={format_metric(ml_metrics.get("pr_auc_baseline"))}; '
            f'lift={lift if lift == "n/a" else lift + "x"}'
        )
        metric_cols[3].metric('Top 30% 富集', f'{format_metric(enrichment)}x')
        st.caption(
            f"LOO-CV：{ml_metrics.get('n_samples', 0)} 个样本，"
            f"活性 {ml_metrics.get('n_active', 0)} 个；{ml_metrics.get('warning', '')}"
        )
    st.dataframe(ml, width='stretch')
    st.download_button(
        '下载 ML 结果 CSV',
        ml.to_csv(index=False).encode('utf-8-sig'),
        'ml_scores.csv',
        'text/csv',
    )

st.header('3. 最强 hit 分子结构')
top_mol_path = OUTPUT / 'top_mol.png'
if top_mol_path.exists():
    st.image(str(top_mol_path), caption='排名靠前的命中分子 2D 结构')
else:
    st.info('未找到 top_mol.png')

st.divider()
st.caption('可插拔生物信息学 Agent · CADD · Omics · Literature · Knowledge · Sequence')

st.divider()
st.header('4. Agent 对话')
st.caption('可插拔生物信息学 Agent · CADD · Omics · Literature · Knowledge · Sequence')

if 'agent_domain' not in st.session_state:
    st.session_state.agent_domain = 'all'
if 'agent_context' not in st.session_state:
    st.session_state.agent_context = [system_message(st.session_state.agent_domain)]
if 'agent_display' not in st.session_state:
    st.session_state.agent_display = []

runtime = configure()

with st.sidebar:
    st.subheader('新手快捷操作')
    if runtime['configured']:
        st.caption(f"LLM：已配置 · {runtime['model']}")
    else:
        st.caption('LLM：未配置；本地命令和 workflow 仍可使用')
    if st.button('查看全部工具', width='stretch'):
        submit_agent_prompt('/tools')
        st.rerun()
    if st.button('运行 RNA-seq 示例', width='stretch'):
        submit_agent_prompt('/workflow examples/workflows/rnaseq.yaml')
        st.rerun()
    if st.button('运行 BGI 研究预设', width='stretch'):
        submit_agent_prompt('/preset bgi_research_demo')
        st.rerun()
    if st.button('异步运行 BGI 研究预设', width='stretch'):
        submit_agent_prompt('/submit research_run_preset {"preset":"bgi_research_demo","dry_run":false}')
        st.rerun()
    with st.expander('插件与运行状态', expanded=False):
        for plugin in PLUGIN_MANAGER.list():
            domain = plugin['domain']
            state = '启用' if plugin.get('enabled', True) else '禁用'
            st.markdown(f"**{plugin.get('name', domain)}** · `{domain}`")
            st.caption(f"{state} · {plugin.get('status', 'unknown')} · {plugin.get('tool_count', 0)} 个工具")
            if plugin.get('status') == 'available':
                action = '禁用' if plugin.get('enabled', True) else '启用'
                if st.button(action, key=f'plugin_toggle_{domain}', width='stretch'):
                    if plugin.get('enabled', True):
                        PLUGIN_MANAGER.disable(domain)
                    else:
                        PLUGIN_MANAGER.enable(domain)
                    st.rerun()
    st.divider()
    domain_options = ['all', *active_domains()]
    if st.session_state.agent_domain not in domain_options:
        st.session_state.agent_domain = 'all'
    selected_domain = st.selectbox('Agent 领域', domain_options, index=domain_options.index(st.session_state.agent_domain))
    if selected_domain != st.session_state.agent_domain:
        st.session_state.agent_domain = selected_domain
        st.session_state.agent_context = [system_message(selected_domain)]
        st.session_state.agent_display = []
        st.rerun()

for item in st.session_state.agent_display:
    with st.chat_message(item['role']):
        st.markdown(item['content'])
        for trace in item.get('traces', []):
            with st.expander(f"工具调用：{trace['name']}"):
                st.json({'arguments': trace['arguments'], 'result': trace['result']})

prompt = st.chat_input('输入问题，或使用 /help 查看工具命令')
if prompt:
    submit_agent_prompt(prompt)
    st.rerun()