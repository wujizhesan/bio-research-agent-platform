# EGFR 虚拟筛选流水线 + LLM Agent

一个在 Windows 上真实跑通的 **CADD（计算机辅助药物设计）虚拟筛选项目**：
用 AutoDock Vina + RDKit 对 EGFR 靶点做分子对接筛选，外加一层 Agent 编排。
可作为简历里"生物 + AI/Agent"方向的作品，用于投递 CADD/生信/医药数字化岗。

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

## 快速运行（一条命令）

```bash
# 环境(第一次)
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 跑完整虚拟筛选 → 出 top_hits.csv + report.md
.venv/Scripts/python src/pipeline.py --exhaustiveness 6

# 离线查看报告(不依赖 LLM key)
.venv/Scripts/python src/agent.py --report

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
