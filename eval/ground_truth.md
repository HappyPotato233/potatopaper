# 文献雷达 · 多 agent 编排问答离线评测 —— Ground Truth（人读版）

> 数据来源：本地 MySQL 库 `literature_radar` 的 `papers` 表（只读）。
> 库内共 **102 篇**，全部属唯一订阅 `[id=1] 检索增强生成`（关键词 retrieval-augmented generation / RAG；来源 arXiv 98 篇 + Semantic Scholar 4 篇）；标题均为英文。
> **本文件所有 ground truth 与量化数字均以库内 `papers.abstract` 原文为准**，禁止引用库外知识、禁止编造数字。
> 会话参数：`user_id=1, mode=phrase, rewrite=true, enable_planner=true`。
> `expect_complexity` / `expect_routes` 由路由器规则离线验证，固定照抄，不得修改。

---

## 预期引用论文速查表（按 id 精确区分，勿串号）

| id | source | journal | title |
|----|--------|---------|-------|
| 1 | arXiv | — | Evidence-Aligned Entity Verification for Hallucination Detection in Retrieval-Augmented Generation |
| 4 | arXiv | — | RAGMark: A Comprehensive Framework for Benchmarking Retrieval-Augmented Generation Systems |
| 6 | arXiv | — | Compression Beyond the Uncompressed: A Two-Stage Training Recipe for Soft Context Compression in RAG |
| 33 | Semantic Scholar | Applied Sciences | Retrieval-Augmented Generation (RAG) and Large Language Models (LLMs) for Enterprise Knowledge Management and Document Automation: A Systematic Literature Review |
| 39 | arXiv | — | VikingRAG: Accurate and Token-efficient Retrieval-augmented Generation over Structured Documents |
| 41 | arXiv | — | ToxicRAG: Compromising Retrieval-Augmented Generation Systems via Single-Shot Knowledge Poisoning Attacks |
| 42 | arXiv | — | MOSAIC: Query-Aware Exploration Policy Adaptation for GraphRAG |
| 43 | arXiv | — | LiteRAG: Cost-Efficient Graph-Based Retrieval-Augmented Generation |
| 45 | arXiv | — | In RAG We Trust? Measuring Robustness of Retrieval-Augmented Generation Under Document Poisoning |
| 58 | arXiv | — | Beyond Vector Similarity: Hierarchical Context-Aware Graph RAG vs Standard RAG in Enterprise Code Migration |
| 74 | arXiv | — | CiteGuard-RAG: A Validation-Centered AI System for Evidence-Grounded Question Answering |
| 76 | arXiv | — | CiteShade: Citation Laundering in Multi-Source Retrieval-Augmented Generation and Its Counterfactual Defense |
| 81 | arXiv | — | CIG-MIA: Context-Induced Information Gain Membership Inference Attacks against Retrieval-Augmented Generation |
| 87 | arXiv | — | Beyond Benchmark Scores: How Synthetic and Authentic Query Distributions Diverge in RAG Evaluation |
| 88 | arXiv | — | The Attribution-Compression Frontier in Retrieval-Augmented Generation |
| 102 | Semantic Scholar | arXiv.org | SoK: Agentic Retrieval-Augmented Generation (RAG): Taxonomy, Architectures, Evaluation, and Research Directions |

**易混提醒**：`#58/#87` 都以 "Beyond" 开头；`#74/#76` 都以 "Cite" 开头（74=CiteGuard-RAG，76=CiteShade）；`#33/#102` 都属 RAG 综述（33=企业知识管理 SLR，102=Agentic RAG SoK）。

---

## Q1　简单 · quick · [library]
**问句**：论文里说的 RAG 是什么？
**预期引用**：`[33, 102]`

**核心答案（≤120 字）**：RAG（检索增强生成）是在推理/生成时检索外部知识证据来增强大模型的技术：先检索相关文档作为上下文，再据此生成答案，弥补参数化知识的时效性与覆盖局限，已用于企业知识管理与文档自动化。

**判分要点**
- RAG 将检索组件与生成式大模型结合，生成前先检索外部证据（库内 #1 亦给出同一定义：推理时检索证据、使输出超越参数化知识）。
- 目的是弥补预训练知识时效性差与覆盖有限的问题，让输出有据可依。
- 综述指出 RAG 与 LLM 的集成正在改变企业知识管理（#33，期刊 Applied Sciences）。
- RAG 正演进为智能体式架构，由 LLM 自主编排多步推理、动态记忆与迭代检索（#102）。

**量化锚点**：无（定义题）。

---

## Q2　简单 · quick · [library]
**问句**：论文中检索增强生成有哪些核心组件？
**预期引用**：`[4, 102]`

**核心答案（≤120 字）**：核心组件包括检索器、向量数据库、提示/上下文处理方法与生成器模型（#4）；智能体式 RAG 还按规划机制、检索编排、记忆范式与工具调用行为做模块化分解（#102）。

**判分要点**
- 检索器（retriever）与向量数据库（vector database）构成检索侧核心组件（#4）。
- 提示处理方法（prompt-processing）与生成器模型（generator）构成生成侧组件（#4）。
- 评测框架将 RAG 各阶段、计时与资源监控分离为模块化组件（#4）。
- 智能体式 RAG 按规划机制、检索编排、记忆范式与工具调用行为四类做模块化分解（#102）。

**量化锚点**：无。

---

## Q3　简单 · quick · [library]
**问句**：什么是 GraphRAG 方法？
**预期引用**：`[42, 58]`

**核心答案（≤120 字）**：GraphRAG 是检索增强生成的一种变体：在语料/知识图谱上做图检索，把分散在整张图中、需经多跳路径相连的证据连接起来，而非仅凭向量相似度取回孤立文本块。

**判分要点**
- GraphRAG 通过语料图谱（corpus graph）连接分布在整个语料、需多跳相连的证据（#42）。
- 可将检索形式化为逐查询的控制问题：按需在种子选择、图遍历、停止与证据选择上施加有界策略（#42）。
- 相较标准向量 RAG 只取孤立 chunk、切断继承链，Graph RAG 保留拓扑/父子等结构关系（#58）。

**量化锚点**：无。

---

## Q4　普通 · normal · [library]
**问句**：该方向 arXiv 文献中，GraphRAG 与标准 RAG 在企业代码迁移场景下的区别是什么？
**预期引用**：`[58]`

**核心答案（≤120 字）**：企业代码迁移中标准（向量）RAG 只取回孤立代码块、切断继承链，导致高编译失败率；Graph RAG 以层级上下文图保留拓扑/父子结构做父先序翻译，显著降低 API 幻觉、提升依赖解析与父子一致性，但会引入过度工程化等代价。

**判分要点**
- 标准 RAG 基于向量相似度取回孤立 chunk，切断继承链，造成高编译失败率。
- Graph RAG 采用 HCRG（层级上下文常驻图）：tree-sitter 抽 AST、映射为属性图、序列化进 Gemini Context Cache 做拓扑父先序翻译。
- Graph RAG 使 API 幻觉率 56.4%→16.2%、依赖解析质量 34.8%→65.9%、父子一致性 26.7%→45.5%。
- 代价：密集全局上下文引发防御性过度工程，圈复杂度一致性 71.6%→46.7%、文档字符串保留率 67.0%→61.0%。
- CodeBLEU 对两种方法都给出 91%，掩盖了标准 RAG 的结构性缺陷。

**量化锚点（均可在 #58 abstract 逐字查到）**
- API 幻觉率 56.4%→16.2%
- 依赖解析质量 34.8%→65.9%
- 父子一致性 26.7%→45.5%
- 圈复杂度一致性 71.6%→46.7%
- 文档字符串保留率 67.0%→61.0%
- CodeBLEU 均 91%

---

## Q5　普通 · normal · [library]
**问句**：该方向论文中，为什么结构化文档检索的证据压缩能同时降低延迟与成本？
**预期引用**：`[39, 6, 43]`

**核心答案（≤120 字）**：结构化文档检索为获取足够证据会带来大量结构上下文 token；证据压缩把检索上下文编码成更短表示、或复用检索轨迹，直接缩短生成输入并降低检索开销，从而同时降延迟与成本，且精度可保持。

**判分要点**
- 结构化 RAG 为拿到足够证据常需大量 token，压缩结构上下文可在不牺牲准确率下降低 token 开销（#39 VikingRAG）。
- 软上下文压缩把每篇文档编码为更短的嵌入序列，两阶段训练使其可比或超越未压缩 RAG 基线（#6 DEX-Comp）。
- 用查询条件化的算法式探索 + 推理链上下文构建替代昂贵的检索期 LLM 控制，可大幅降延迟与成本（#43 LiteRAG）。
- 将多轮检索轨迹物化为可复用经验边、并采用自适应升级策略，进一步压缩 token 成本（#39）。

**量化锚点**
- VikingRAG token 消耗仅为对手的 11.6%-51.9%，成本降至 5.1%-32.5%（#39）
- DEX-Comp 压缩 16×、推理加速 4×-24×（#6）
- LiteRAG 质量 0.798、延迟降低 >100×、成本降低 >99%（#43）

---

## Q6　普通 · normal · [library]
**问句**：该方向 arXiv 文献中，投毒攻击的成功率主要与哪些因素相关？
**预期引用**：`[41, 45]`

**核心答案（≤120 字）**：投毒成功率与投毒文档的形式与构造、数据集与受害者模型/检索器组合、被投毒检索通道的数量与占比、以及投毒策略类型（实体替换/数字替换/否定）等因素相关。

**判分要点**
- 以连贯的「知识更新叙事」表达虚假信息、每目标仅一篇投毒文档即可奏效，攻击力随数据-模型组合变化（#41 ToxicRAG）。
- 在 Natural Questions/HotpotQA/MS-MARCO × 4 模型 × 4 检索器的 12 组组合上，ASR 介于 0.61 与 0.91 之间（#41）。
- 三篇检索通道全部被污染时准确率 77.9%→43.5%，说明被污染通道数量/占比是关键因素（#45）。
- 实体替换策略翻转原本正确答案的比例最大；数字类污染在污染通道占少数时影响平稳、占多数时骤升（#45）。

**量化锚点**
- ToxicRAG ASR 0.61-0.91（原文 twelve dataset--model combinations）
- 准确率 77.9%→43.5%（588 次实验）

---

## Q7　普通 · normal · [library]
**问句**：该方向 arXiv 论文里，检索系统幻觉与引用溯源问题的原理差异是什么？
**预期引用**：`[1, 88, 74]`

**核心答案（≤120 字）**：幻觉问题指向生成内容与证据/事实不符，可用实体-证据对齐与反事实稳定性来检测；引用溯源问题指向答案所标注的引用能否真正回溯并支撑该答案（归因精度）。二者失真的环节不同：前者在生成内容，后者在引用标注的可验证性。

**判分要点**
- 幻觉检测关注生成实体是否与检索证据对齐，可用三维度对齐 + 反事实稳定性分析检出实体级幻觉（#1 EAEV）。
- EAEV 不依赖模型内部不确定性/自洽信号，而改用检索证据对齐（#1）。
- 引用溯源（归因）关注引用能否追溯到源片段：摘要式压缩下引用对摘要精度为 0.86、但对源 span 仅 0.12（#88）。
- 同一 RECOMP 设置下，按摘要核查的无支撑率 0.17、恢复源片段后升至 0.88，说明「答案正确」不等于「引用有据」（#88）。
- 检索到证据并不保证答案 grounded、citation-valid：#74 将 grounded-answer 准确率与 citation validity 分开评估。

**量化锚点**
- 引用精度 0.86（摘要）vs 0.12（源片段）（#88）
- 无支撑率 0.88 vs 0.17（#88）
- CiteGuard-RAG：检索 99.1% / grounded 98.3% / citation 98.3%（400 问）（#74）

---

## Q8　困难 · deep · [memory]
**问句**：上文说过 GraphRAG，请系统梳理 arXiv 近三年 GraphRAG、标准 RAG 与证据压缩三条技术路线，做对比分析，并限定在期刊与预印本来源范围内给出选型建议。
**预期引用**：`[33, 58, 6]`

**核心答案（≤120 字）**：三条路线：标准 RAG 依赖 FAISS/Elasticsearch 等检索框架（#33）；GraphRAG 保留图谱拓扑/多跳结构但成本高（#58）；证据压缩（#6）以更短上下文提速省费。选型取决于是否需要结构关系与成本约束。

**判分要点**
- 标准 RAG：企业实现中 80.5% 依赖 FAISS/Elasticsearch 等标准检索框架、GPT 系模型占 63.6%（#33，期刊 Applied Sciences，63 篇研究）。
- GraphRAG：以层级上下文图保留拓扑/父子关系，企业代码迁移中显著降低 API 幻觉并提升依赖解析（#58）。
- 证据压缩：软上下文压缩把文档编码为更短嵌入序列，压缩 16×、推理加速 4×-24×（#6）。
- 选型建议：结构化/多跳/需结构关系场景宜 GraphRAG（#58）；延迟与成本敏感、上下文过长场景宜证据压缩（#6）；通用企业场景可沿用标准检索框架（#33）。
- 来源范围：期刊侧为 #33（Applied Sciences），预印本侧为 #58/#6（arXiv）。

**量化锚点**
- 标准检索框架占 80.5%、GPT 系占 63.6%（#33）
- API 幻觉率 56.4%→16.2%（#58）
- 压缩 16×、推理加速 4×-24×（#6）

---

## Q9　困难 · deep · [memory]
**问句**：上文提到投毒攻击，请多视角批判地分析 arXiv 近三年论文中投毒、引用洗白与成员推断三类攻击的机理、防御手段与来源可信度。
**预期引用**：`[41, 76, 81]`

**核心答案（≤120 字）**：投毒以恶意文档污染检索（#41，ASR 0.61-0.91）；引用洗白把错答归因到不可信来源（#76，错答率 0.01→0.68）；成员推断判定文档是否在库（#81，AUC 0.99 灰盒/0.93 黑盒）。防御须核验驱动答案的真正来源。

**判分要点**
- 投毒机理：以连贯「知识更新叙事」表达虚假信息，每目标仅需一篇文档，可选答案导向的自验证循环（#41）。
- 引用洗白机理：攻击者控制单一来源，诱导模型输出错误答案并归因到不支持该答案的可信来源，同时正确答案证据仍留在上下文中（#76）。
- 成员推断机理：基于上下文诱导的信息增益——注入候选文档对库内/库外文档提供的支持量不同（#81 CIG-MIA）。
- 防御：困惑度过滤与引用支持校验均不足，需反事实防御核验真正驱动答案的来源（#76）；投毒研究呼吁关注事实一致性与来源溯源（#41）。
- 来源可信度：三类攻击均说明检索/引用通道是新的攻击面，来源可信度不能仅凭「已被检索」推定。

**量化锚点**
- ToxicRAG ASR 0.61-0.91（#41）
- CiteShade 错答率 0.01→0.68；CLR 0.84（有指令）/0.64（无指令）（#76）
- CIG-MIA AUC 0.99（灰盒）/0.93（黑盒）（#81）

---

## Q10　困难 · deep · [memory]
**问句**：上文中我们讨论过 RAG 评测，请深度分析并系统梳理 arXiv 近一两年中合成查询与真实查询两类评测在数据规模、任务难度与结论可靠性上的差异，并限定在期刊与预印本来源范围内。
**预期引用**：`[87, 4]`

**核心答案（≤120 字）**：合成查询规模更大、更长更详细（1,851 条、均 15.7 词）；真实查询更短更集中（322 条、均 6.8 词）。合成集表现好不代表真实可用：优化合成集会选中高延迟检索器，延迟最多为最快配置的 8×。二者应视为查询质量谱两端的互补。

**判分要点**
- 数据规模：高校教职工信息系统上 1,851 条合成查询 vs 322 条真实查询（#87）。
- 任务难度：合成长查询均 15.7 词、真实短查询均 6.8 词；合成来自 165 个来源、真实仅 53 个（#87）。
- 结论可靠性：在合成集上高效的配置在真实查询上显著掉点，且优化合成集会选中高延迟混合检索器，延迟最多为最快配置的 8×（#87）。
- 评测框架需分离各组件并采集分阶段指标（延迟、GPU、内存、能耗、答案质量），以便区分不同查询集下的表现（#4 RAGMark）。
- 结论：合成查询验证最大检索能力，真实查询检验系统对短而模糊输入的鲁棒性，二者互补（#87）。

**量化锚点**
- 1,851（合成）vs 322（真实）（#87）
- 均长 15.7 词 vs 6.8 词（#87）
- 165 个来源 vs 53 个来源（#87）
- 延迟最多 8×（#87）

---

## 附：未纳入的候选与理由
- **Q4 仅引 #58**：题干限定「企业代码迁移场景」，库内仅 #58 同时覆盖「GraphRAG vs 标准 RAG + 代码迁移」，#42/#43 虽属 GraphRAG 但不涉及代码迁移场景。
- **Q10 未引 #4 之外的 #34/#68**：#34（Deepchecks）、#68（TraceJudgeBench）虽涉评测/可靠性，但与「合成 vs 真实查询分布」主题相关度较弱；#34/#102 等亦被标注为无量化锚点。
- **#8/#14/#34/#92/#102 无量化数字**：不作为量化引用来源；#102 仅用于 Q1/Q2 的定义性引用（不设 gt_numbers）。
