# Bio Research Agent Platform

CI：GitHub Actions 会运行 Python 3.11/3.12 测试、Alembic 迁移、领域/工作流冒烟和 Docker 镜像构建。

一个面向生物科研场景的可插拔 Agent 平台。平台通过统一的插件契约、领域注册表和工作流运行器，把 CADD、RNA-seq/Omics、mRNA 序列设计、文献证据和本地知识检索接入同一套 Agent 工具协议。

许可证：MIT，见 [LICENSE](LICENSE)。

## 平台领域

- **CADD**：RDKit、AutoDock Vina、虚拟筛选和活性预测
- **Omics**：RNA-seq 差异表达、通路富集、证据检索和报告生成
- **Sequence**：通过 mRNA-Forge 插件进行 mRNA 优化、评分和翻译验证
- **Literature**：本地证据、UniProt、PubMed、NCBI Gene 和 KEGG 适配
- **Knowledge**：本地 Markdown、文本和 HTML 科研资料索引与检索
- **Research**：根据任务选择领域、推断证据源并执行可追踪的跨领域工作流

CADD 是当前最完整的科学计算领域实现，其他领域通过同一套插件和工作流接口扩展。

## 服务层与本地部署

平台同时提供 FastAPI 异步服务层，使用统一插件目录提交后台任务，并通过 OpenAPI 自动生成 `/docs`。本地直接运行时默认使用 SQLite 保存服务层任务读模型；Docker Compose 使用 PostgreSQL。

提交后台任务后，可通过 `GET /api/v1/jobs/{job_id}/events` 使用 SSE 订阅状态变化，服务会推送 `queued`、`running` 和终态事件；接口支持 `interval_seconds`、`timeout_seconds` 查询参数。

研究输入可以通过 `POST /api/v1/files` 以 multipart 上传。服务端按扩展名和大小校验文件，使用随机文件 ID 隔离目录并记录 SHA-256；上传响应中的 `path` 可直接作为 `research_plan` 的输入，`GET /api/v1/files/{file_id}` 用于下载。默认限制为 50 MiB，可通过 `UPLOAD_ROOT` 和 `UPLOAD_MAX_BYTES` 配置。

React 工作台的研究模式采用两阶段交互：先提交 `research_plan` 展示领域、证据源、输入门槛和实际工具链，再由用户确认后提交 `research_execute`；mRNA 模式仍可直接运行 `sequence_pipeline`。两阶段任务都通过同一套 Job/SSE 生命周期展示。

```bash
python -m pip install -e .
bio-agent-api --port 8000
```

服务启动后访问 `http://127.0.0.1:8000/docs`。开发环境可以配置 `CADD_API_TOKEN` 使用兼容的管理员 Token；生产环境建议配置至少 32 字符的 `CADD_JWT_SECRET` 和 `CADD_AUTH_USERS_JSON`，通过 `POST /api/v1/auth/token` 获取 JWT。角色支持 `admin`、`researcher` 和 `viewer`，任务提交、文件上传下载和插件状态变更会写入 `output/audit.jsonl`。

JWT 用户配置示例：

```json
{"alice":{"password_hash":"pbkdf2_sha256$310000$<salt>$<digest>","roles":["researcher"]}}
```

`src.auth.hash_password("your-password")` 可以生成 PBKDF2 密码哈希。旧版明文 `password` 字段仅适合本地演示。

数据库迁移使用 Alembic：

```bash
alembic upgrade head
```

`/metrics` 提供 Prometheus 格式的 HTTP 请求量、延迟、任务提交和任务状态指标。

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
```

Compose 会启动 FastAPI、PostgreSQL 和持久化数据卷；科学计算所需的大型受体、分子库和 Vina 可执行文件需要按项目说明另行挂载或准备。

Compose 同时提供 React 研究工作台，访问 `http://127.0.0.1:5173`；本地开发前端可运行：

```bash
cd frontend
npm install
npm run dev
```

任务提交支持 `Idempotency-Key` 请求头，重复提交同一工具与参数会复用原任务；参数不一致会返回 `400`。任务可通过 `POST /api/v1/jobs/{job_id}/cancel` 取消，排队任务立即取消，运行中任务在执行线程结束后标记为 `cancelled`。

## 项目要解决什么

虚拟筛选（Virtual Screening）的核心价值：
> 不用做实验，用计算机把一个分子库逐个"放到"靶点蛋白的结合口袋，靠对接打分函数
> 算出谁结合最牢、最有可能是活性药物，从而从成千上万分子里 **优先筛出少量候选** 去实验验证。

本项目实现了这条流水线的**最小闭环**，并验证了方法对 EGFR 靶点的有效性。

## 技术栈（全部真实、可核实）

| 组件 | 用途 | License |
|---|---|---|
| **AutoDock Vina v1.2.7** | 分子对接引擎（官方 Windows exe） | Apache-2.0 |
| **RDKit** | 分子处理（SMILES→3D构象→指纹） | BSD-3-Clause |
| **Meeko** | SMILES/SDF → 配体 PDBQT | Apache-2.0 |
| scikit-learn / pandas | 结果分析与活性预测（可选） | BSD |

> 说明：AutoDock Vina 在 PyPI 仅提供 Linux wheel，Windows 上使用官方编译的 `vina_*.exe`（`tools/`），
> 通过子进程调用（`src/dock_vina.py`）。这是该项目在 Windows 能真实跑通的关键。

## 架构

```
user 指令
  │
  ▼
src/agent.py ──(LLM 对话,可选)──► 生成式报告摘要
  │                 │
  ▼                 ▼
src/pipeline.py ── 一条命令串起 ──► src/report.py (离线 markdown 报告)
  │
  ├─ src/prepare_receptor.py   PDB → 干净受体(去配体/水)
  ├─ src/build_library.py      SMILES → 3D SDF → 配体 PDBQT
  ├─ src/dock_vina.py          调 Vina 批量对接 + 打分解析
  └─ src/library_data.py       分子库(活性正对照 + 非活性负对照)
```

## 可插拔扩展

CADD backend 通过统一 PluginContract 校验 API version 和 capability，配置支持内置模块、module:attribute、mapping target，以及 Python entry point：cadd_agent.plugins。domain_registry 还支持通过 cadd_agent.domains 安装外部领域工具集。每次运行的 run_manifest.json 会记录插件名称、版本、API 版本和能力。

## 快速运行（一条命令）

```bash
# 环境(第一次)
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 跑完整虚拟筛选 → 出 top_hits.csv + report.md
.venv/Scripts/python src/pipeline.py --exhaustiveness 6

# Results are isolated under output/runs/<run_id>; use an explicit id when comparing experiments
.venv/Scripts/python src/pipeline.py --exhaustiveness 6 --run-id egfr-baseline

# 离线查看报告(不依赖 LLM key)
.venv/Scripts/python src/agent.py --report
.venv/Scripts/python src/agent.py --chat --domain all

# Build a property-matched hard-decoy benchmark from a strict external holdout
.venv/Scripts/python -m src.build_hard_decoy_benchmark --input output/bindingdb_egfr_10000_strict_holdout.csv --out output/bindingdb_egfr_hard_decoy.csv --provenance output/bindingdb_egfr_hard_decoy.provenance.json
.venv/Scripts/python -m src.build_hard_decoy_benchmark --input output/bindingdb_egfr_10000_strict_holdout.csv --out output/bindingdb_egfr_hard_decoy.csv --provenance output/bindingdb_egfr_hard_decoy.provenance.json --control-out output/bindingdb_egfr_random_control.csv --control-provenance output/bindingdb_egfr_random_control.provenance.json --max-pairs 50
.venv/Scripts/python -m src.compare_benchmarks --hard output/bindingdb_egfr_hard_decoy.csv --control output/bindingdb_egfr_random_control.csv --out output/bindingdb_egfr_benchmark_comparison.json

# Generate repeated random controls and bootstrap summaries
.venv/Scripts/python -m src.run_benchmark_replicates --input output/bindingdb_egfr_10000_strict_holdout.csv --hard output/bindingdb_egfr_hard_decoy.csv --hard-provenance output/bindingdb_egfr_hard_decoy.provenance.json --control-dir output/random_controls --seeds 11 22 33 44 55 66 77 88 --out output/bindingdb_egfr_benchmark_replicates.json

# RNA-seq domain adapter
.venv/Scripts/python -m src.omics_agent --expression examples/rnaseq/expression.csv --metadata examples/rnaseq/metadata.csv --gene-sets examples/rnaseq/gene_sets.csv --evidence examples/rnaseq/evidence.csv --out-dir output/rnaseq_demo
.venv/Scripts/python -m src.domain_registry --domain all

# MCP server
.venv/Scripts/python -m src.mcp_server --list
.venv/Scripts/python -m src.mcp_server

# Traceable cross-domain workflow
.venv/Scripts/python -m src.workflow_runner --workflow examples/workflows/rnaseq.yaml --dry-run --out output/workflow_demo_dry/workflow_manifest.json
.venv/Scripts/python -m src.workflow_runner --workflow examples/workflows/rnaseq.yaml --out output/workflow_demo/workflow_manifest.json

# End-to-end RNA-seq research Agent workflow
.venv/Scripts/python -m src.workflow_runner --workflow examples/workflows/rnaseq_research_agent.yaml --out output/rnaseq_research_agent/workflow_manifest.json

# Inspect an automatically planned multi-domain workflow
.venv/Scripts/python -c "from src.research_agent import research_plan; import json; print(json.dumps(research_plan('分析 RNA-seq 并使用 KEGG 解释通路和设计 mRNA', inputs={'expression_csv': 'examples/rnaseq/expression.csv', 'metadata_csv': 'examples/rnaseq/metadata.csv', 'gene_sets_csv': 'examples/rnaseq/gene_sets.csv', 'protein': 'MKT', 'output_dir': 'output/auto_research'}), ensure_ascii=False, indent=2))"

# Use live UniProt, PubMed, NCBI Gene or KEGG evidence with a local response cache
.venv/Scripts/python -m src.omics_agent --expression examples/rnaseq/expression.csv --metadata examples/rnaseq/metadata.csv --gene-sets examples/rnaseq/gene_sets.csv --evidence-provider uniprot --cache-dir output/uniprot_cache --out-dir output/rnaseq_uniprot
.venv/Scripts/python -m src.omics_agent --expression examples/rnaseq/expression.csv --metadata examples/rnaseq/metadata.csv --gene-sets examples/rnaseq/gene_sets.csv --evidence-provider pubmed --cache-dir output/pubmed_cache --out-dir output/rnaseq_pubmed
.venv/Scripts/python -m src.omics_agent --expression examples/rnaseq/expression.csv --metadata examples/rnaseq/metadata.csv --gene-sets examples/rnaseq/gene_sets.csv --evidence-provider ncbi_gene --cache-dir output/ncbi_gene_cache --out-dir output/rnaseq_ncbi_gene
.venv/Scripts/python -m src.omics_agent --expression examples/rnaseq/expression.csv --metadata examples/rnaseq/metadata.csv --gene-sets examples/rnaseq/gene_sets.csv --evidence-provider kegg --cache-dir output/kegg_cache --out-dir output/rnaseq_kegg

# MVP uses a replaceable SciPy statistics backend; production RNA-seq can swap in DESeq2 or edgeR without changing the Agent tool contract.
# 验收(自检 5 项断言)
.venv/Scripts/python src/qa_verify.py
```

## 核心结果

对 EGFR（PDB 4HJO）虚拟筛选 **17 个分子**（7 活性正对照 + 10 非活性负对照），双引擎评估。

**分子库构成（全部真实、可核实）**：
- **活性正对照（7 个）**：6 个**已上市 EGFR 抑制剂**（erlotinib/gefitinib/osimertinib/afatinib/lapatinib/icotinib，
  SMILES 来自 PubChem 官方 API）+ AQ4（PDB 4HJO 真实共晶配体）
- **非活性负对照（10 个）**：常见小分子 + 有芳环分子（苯胺/苯甲酰胺/异喹啉等）

### 引擎一：AutoDock Vina 对接打分（kcal/mol，越负越强）

| 排名 | 分子 | 类型 | 对接打分 |
|---|---|---|---|
| 1 | **lapatinib** | **活性**（上市药） | **-9.67** |
| 2 | gefitinib | **活性**（上市药） | -8.68 |
| 3 | afatinib | **活性**（上市药） | -8.49 |
| 4 | osimertinib | **活性**（上市药） | -8.03 |
| 5 | AQ4 | **活性**（共晶配体） | -7.77 |
| 6 | erlotinib | **活性**（上市药） | -7.64 |
| 7 | BIPHENYL | 非活性 | -6.93 |
| 8 | icotinib | **活性**（上市药） | -6.87 |
| ... | 非活性 | ... | 全部垫底（-5.6 ~ -3.6） |

**对接结论**：**7 个真实 EGFR 抑制剂全部排进前 8**，最强 hit（-9.67）是上市药 lapatinib，
共晶配体 AQ4 排第 5。Top5 富集 5/7 活性（富集倍数 x2.43），且打分取 Vina 多构象最低分（最佳结合模式），可稳定复现。

### 引擎二：ML 活性预测（RDKit 描述符 + 随机森林）

- 留一交叉验证 accuracy=1.00（诚实说明：17 个小样本 + 负对照性质差异，区分偏易，1.00 **不代表真实世界泛化**，仅作演示）
- 7 个真实 EGFR 药物活性概率 0.94-1.00，非活性分子 0.00-0.06
- 输出 `ml_scores.csv`，与 Vina 对接互为交叉验证

**综合结论**：Vina 对接（几何结合）+ ML 预测（性质建模）双引擎都能正确指向 EGFR 活性分子，
构成"**ML 初筛 + 对接精筛**"的 AI+VS 混合流水线，是 AIDD/CADD 岗的核心能力。
> 注意：ML 的 1.00 仅在小样本演示集上成立，真实使用需更大、更难区分的 decoy 数据集。

## 验证（QA）

`src/qa_verify.py` 自动断言 5 项，全部 PASS（17 分子版本复现稳定）：
1. 报告文件存在
2. 结果非空
3. 打分可解析
4. 打分在合理范围 [-15, 0]
5. **Top hit 富集活性分子、最强 hit 是活性（方法可靠）**

## ML 活性预测

`src/ml_predictor.py` 用 RDKit 分子描述符 + 随机森林训练活性分类器：
- **留一交叉验证**（LOO-CV）评估，避免"训练即预测"的泄漏
- 全库 17 分子，accuracy=1.00。**诚实说明**：样本小、负对照为性质差异较大的常见小分子，
  模型区分相对容易——此 1.00 仅为演示，**不代表真实世界泛化**。真实使用需更大、
  与活性分子性质匹配的 decoy 数据集（如 DUD-E）做更严格评估。
- 输出每个分子的活性概率，作为对接打分的交叉验证

## 对接 CADD 岗的知识点要点

- **打分函数**：Vina 输出结合自由能(kcal/mol)，越负代表结合越强，是筛选排序的依据
- **对接盒子**：运行时从原始 4HJO 中 AQ4 共晶配体的原子坐标**计算质心**（非硬编码），
  结合口袋用 AQ4 位置精准定义
- **受体/配体准备**：受体需去共晶配体/水；配体由 SMILES 经 RDKit 生成 3D 构象、Meeko 转 PDBQT
- **正负对照验证**：活性分子应整体打分更强——这是虚拟筛选方法可靠性的核心证据
- **AI 融合**：RDKit 描述符 + ML 分类器做活性初筛，与对接互为验证

## 目录结构

```
src/          核心代码(pipeline/dock/build/report/agent/qa)
tools/        AutoDock Vina 官方 Windows exe
data/         PDB 结构 + 分子库(不入 git)
output/       运行产物(top_hits.csv / report.md)
requirements.txt / README.md / config.yaml
```

## 对话式 UI（可选）

项目现在提供两个互补界面：`app.py` 是 Streamlit 结果仪表盘，`src/chainlit_app.py` 是 Chainlit 对话入口。Chainlit 只负责交互层，工具注册、插件和 workflow 仍由现有后端负责。

```bash
.venv/Scripts/python -m streamlit run app.py

```

打开后可使用 `/help`、`/domain cadd|omics|all`、`/tools`、`/run <工具名> <JSON>` 和 `/workflow <路径>`。未配置 LLM Key 时，工具和工作流仍可直接运行；配置 `CADD_API_KEY` 或 `OPENAI_API_KEY` 后启用自然语言 Agent。

Chainlit 当前依赖 MCP 1.x，而项目 MCP 服务使用 MCP 2.x，因此 Chainlit UI 使用独立的 .venv-chainlit；两套环境共享同一份源码和工具契约。


Streamlit Agent chat is integrated in app.py and runs in the core .venv. Chainlit remains an optional adapter in the separate .venv-chainlit environment because its MCP dependency version differs from the project MCP server.

## 安装为 Python 项目

```bash
python -m pip install -e .
```

安装后可以使用：

```bash
bio-agent-domains --domain all
bio-agent-workflow --workflow examples/workflows/bgi_research_demo.yaml --dry-run
bio-agent-mcp --list
```

## mRNA 插件

Sequence 领域默认使用内置确定性后端，保证公开仓库可以直接运行优化、评分、验证、比较、基准和报告工具。若配置了 mRNA-Forge，平台会优先使用外部后端；通过环境变量指定路径：

```bash
set MRNA_FORGE_ROOT=D:\EnornaAgent
```

Linux/macOS 使用：

```bash
export MRNA_FORGE_ROOT=/path/to/EnornaAgent
```

## 科学边界

RNA-seq 当前使用可替换的 SciPy 统计后端，适合作为可复现演示和平台契约验证；生产分析可以替换为 DESeq2 或 edgeR。mRNA 的 CAI、GC、GC3、UpA、UpU 和表达评分来自确定性规则或可选后端，不应被表述为经过大规模实验数据训练的预测模型。CADD 和序列结果都需要结合实验或领域专业判断验证。
