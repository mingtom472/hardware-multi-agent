"""Deterministic offline responses for exercising orchestration, never engineering evidence."""

import asyncio
import json
import math
from decimal import Decimal

from .base import ModelRequest, ModelResponse, ProviderError


class MockProvider:
    """All usage/cost figures are estimates for testing, not provider billing."""

    def __init__(self, provider_id: str, latency_seconds: float = 0.01) -> None:
        if not math.isfinite(latency_seconds) or latency_seconds < 0:
            raise ValueError("latency_seconds must be finite and nonnegative")
        self.provider_id = provider_id
        self.latency_seconds = latency_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self.latency_seconds)
        stage = request.stage.lower()
        if stage in {"classification", "classify", "task_classification"}:
            text = self._classify(request.prompt)
        elif stage in {"vision", "image_recognition", "image_analysis"}:
            images = sum(
                str(a.get("content_type", "")).startswith("image/") for a in request.attachments
            )
            text = (
                f"[模拟图片识别] 已接收 {images} 份图片附件元数据。"
                "尚未读取像素或识别真实电路；拓扑、器件标识及连接关系均需后续视觉适配器验证。"
            )
        elif stage in {"datasheet", "datasheet_analysis", "document_analysis"}:
            text = (
                "[模拟数据手册分析] 当前只接收文件元数据，尚未读取或解析 PDF 原文。"
                "请通过后续文档检索工具提取可追溯的器件型号、参数条件、文档版本及页码；"
                "未知值保持未知，不编造绝对最大额定值或推荐工作条件。"
            )
        elif stage in {"hardware_analysis", "analysis", "review", "analyze"}:
            focus = {
                "openai": "检查输入输出规格、功率器件额定值及降额裕量",
                "claude": "检查假设、保护机制、隔离与安全边界",
                "deepseek": "核对公式、量纲、控制环路及极端工况",
                "doubao": "整理拓扑候选、元器件清单与待补充信息",
                "glm": "交叉核对 BOM、数据手册依据和测试步骤",
                "qwen": "整理原理图待识别器件、型号和连线的不确定区域",
                "moonshot": "整理待提取的数据手册参数、适用条件及来源页码",
            }.get(self.provider_id, "检查硬件规格与待验证假设")
            text = (
                f"[模拟硬件分析 · {self.provider_id}] {focus}。"
                "请提供输入范围、输出电压/电流、纹波、温升和开关频率。"
                "此为流程演示，不代表已完成拓扑识别、参数校验或硬件安全认证。"
            )
        elif stage in {"validation", "validate", "verification", "verify"}:
            text = (
                "[模拟验证] 已执行交叉评审阶段；模型结论尚无真实测量或数据手册证据。"
                "确认单位和边界条件，逐项核对额定值、热设计与保护策略，再安排仿真和台架测试。"
            )
        elif stage in {"summary", "summarize", "aggregation", "aggregate"}:
            text = (
                "[模拟汇总] 分类、并行硬件评审和交叉验证已完成；"
                "有图片附件时另行演示图片识别分支。"
                "建议下一步补齐电气规格与原理图，复核 BOM、器件裕量及关键公式，"
                "并规划纹波、效率、温升和瞬态测试。所有模型输出与 Token/成本均为模拟数据；"
                "嵌入式软件 Agent、真实视觉识别及数据手册解析仍为接口预留。"
            )
        else:
            raise ProviderError(
                f"Unsupported mock workflow stage: {request.stage}", retryable=False
            )
        # A deterministic approximation, explicitly distinguished from billed API usage.
        serialized = json.dumps(
            {
                "prompt": request.prompt,
                "context": request.context,
                "attachments": request.attachments,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        input_tokens = max(1, (len(serialized.encode("utf-8")) + 3) // 4)
        output_tokens = max(1, (len(text.encode("utf-8")) + 3) // 4)
        cost = Decimal(input_tokens + output_tokens) * Decimal("0.000001")
        return ModelResponse(
            text,
            input_tokens,
            output_tokens,
            cost,
            simulated=True,
            usage_source="estimate",
            cost_source="estimate",
            estimated_cost_usd=cost,
        )

    @staticmethod
    def _classify(prompt: str) -> str:
        lower = prompt.lower()
        if any(word in lower for word in ("固件", "驱动", "firmware", "rtos", "嵌入式软件")):
            category = "embedded_software_interface_only"
        elif any(word in lower for word in ("bom", "物料", "元器件清单")):
            category = "bom_analysis"
        elif any(word in lower for word in ("电源", "buck", "boost", "llc", "power", "flyback")):
            category = "power_electronics"
        else:
            category = "hardware_analysis"
        return json.dumps(
            {"category": category, "simulated": True, "reason": "离线关键词分类，仅用于验证工作流"},
            ensure_ascii=False,
        )
