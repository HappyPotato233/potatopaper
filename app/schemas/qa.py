# 问答（多 Agent 编排）相关模型
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """POST /api/qa/ask 请求体（对齐详细设计 §3.5）"""
    query: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="phrase", pattern="^(phrase|paragraph|strategy)$")
    rewrite: bool = True          # 是否走 LLM 查询改写（失败回退原始 query）
    enable_planner: bool = True   # 是否启用 v4 Planner 主-子编排（false 强制 v3 固定流水线）


class AskResponse(BaseModel):
    session_id: str
    status: str = "queued"


class StepInfo(BaseModel):
    """单步状态（前端步骤条轮询依据；8 节点，对齐前端设计文档 v2 §6.3）"""
    key: str
    label: str
    status: str  # pending / running / done / failed / skipped（快速通道/回退时跳过）
    detail: str = ""
    thinking: str = ""   # 该步骤的模型思考/输出文本（前端打字机+折叠展示）


class ToolCall(BaseModel):
    """工具调用记录（思考过程面板数据源，编排方案 v4 §3）"""
    tool: str
    args: dict = {}
    result_brief: str = ""


class ReviewNotes(BaseModel):
    """复审意见（Reviewer 固定 JSON 的 DTO 形态，v4 §2.2）"""
    passed: bool
    defects: list[str] = []
    suggestions: list[str] = []


class StepsResponse(BaseModel):
    session_id: str
    query: str
    mode: str
    complexity: str = "normal"     # 编排档位：quick / normal / deep（v4 §4.3）
    steps: list[StepInfo]
    finished: bool = False
    loop_counter: int = 0          # 迭代轮次（编排模式下新增字段）
    tool_calls: list[ToolCall] = []          # 工具调用记录
    review_notes: ReviewNotes | None = None  # 复审意见


class Citation(BaseModel):
    """引用来源（可回溯）"""
    title: str
    source: str
    url: str = ""
    year: str = ""
    paper_id: int | None = None   # 库内文献主键（前端跳内置阅读页；网络/记忆来源为空）


class OutlineNode(BaseModel):
    """综述大纲节点（可编辑大纲 / 导出入参，F-REV-04）"""
    id: str = ""
    depth: int = 1                  # 章节层级 1~3
    title: str = ""
    content: str = ""
    citations: list[int] = []       # 引用序号（指向 citations[] 下标 +1）


class ReviewExportRequest(BaseModel):
    """POST /api/qa/review/export 请求体：用户编辑后的大纲（仅用于生成文档，不落库）"""
    title: str = "文献综述"
    outline: list[OutlineNode] = []
    citations: list[Citation] = []


class QAResult(BaseModel):
    session_id: str
    query: str
    rewritten_query: str = ""
    rewritten: bool = False
    points: list[str] = []          # 要点答案
    citations: list[Citation] = []  # 引用来源
    answer: str = ""                # 汇总正文（综述式完整回答）
    planner_notes: list[str] = []   # 主 Agent 决策记录（编排模式下新增字段）
    iteration_count: int = 0        # 迭代次数（编排模式下新增字段）
    outline: list[OutlineNode] = []  # 综述可编辑大纲（F-REV-04）


class ReviewRequest(BaseModel):
    """多视角综述请求：对指定方向的已入库文献生成综述"""
    subscription_id: int


class ReviewSection(BaseModel):
    title: str
    content: str


class ReviewResult(BaseModel):
    subscription_id: int
    direction_name: str
    paper_count: int
    sections: list[ReviewSection] = []
    summary: str = ""
    outline: list[OutlineNode] = []  # 可编辑大纲（F-REV-04）
