"""完整洞察报告 PDF 渲染（2026-09-13）。

纯模板填充，不调用 LLM：app-api 组装报告数据 → 本服务渲染 A4 PDF → 原路流式回传。
中文使用 reportlab 内置 CID 字体 STSong-Light，无需字体文件。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

_FONT = "STSong-Light"
_MISSING = "暂缺"
_DISCLAIMER = "本报告由 AI 生成，仅供参考，不构成投资建议"


def _register_font() -> None:
    """注册中文字体（幂等）。"""
    try:
        pdfmetrics.getFont(_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))


def _text(value: Any) -> str:
    """任意值 → 展示文本；空值统一"暂缺"。"""
    if value is None or value == "":
        return _MISSING
    return str(value)


def _escape(text: str) -> str:
    """Paragraph 需要转义 XML 保留字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_report_sections(data: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """报告数据 → 章节列表 (标题, 内容行)。缺失字段输出"暂缺"，不抛错。"""
    event = data.get("event") or {}
    attr = data.get("attribution") or {}

    sections: list[tuple[str, list[str]]] = [
        ("事件事实", [
            f"股票：{_text(event.get('stockName'))}（{_text(event.get('symbol'))}）",
            f"触发时间：{_text(event.get('triggeredAt'))}",
            f"方向：{_text(event.get('direction'))}",
            f"涨跌幅：{_text(event.get('changePct'))}%（阈值 {_text(event.get('thresholdPct'))}%）",
            f"严重度：{_text(event.get('severity'))}",
            f"最新价 / 昨收：{_text(event.get('latestPrice'))} / {_text(event.get('previousClose'))}",
        ]),
        ("主因结论", [
            f"一句话主因：{_text(attr.get('primaryPhrase'))}",
            f"置信度：{_text(attr.get('confidenceLevel'))}",
        ]),
    ]

    candidates = [c for c in (attr.get("candidates") or []) if isinstance(c, dict)]
    sections.append(("五层候选归因", [
        f"[{_text(c.get('layer'))} · {_text(c.get('status'))}] {_text(c.get('verdict'))}"
        for c in candidates
    ] or [_MISSING]))

    chain_lines: list[str] = []
    for chain in attr.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        for node in chain.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            chain_lines.append(
                f"{_text(node.get('stage'))}：{_text(node.get('claim'))}"
                f"（{_text(node.get('epistemicType'))} / {_text(node.get('status'))}）"
            )
    sections.append(("六阶段因果链", chain_lines or [_MISSING]))

    evidence = [e for e in (attr.get("evidenceIndex") or []) if isinstance(e, dict)]
    sections.append(("证据清单", [
        f"{_text(e.get('source_id'))}｜{_text(e.get('kind'))}｜{_text(e.get('title'))}｜{_text(e.get('content_excerpt'))}"
        for e in evidence
    ] or [_MISSING]))

    questions = [str(q) for q in (attr.get("unresolvedQuestions") or []) if q]
    sections.append(("未解问题", questions or [_MISSING]))

    return sections


def render_insight_report(
    sections: list[tuple[str, list[str]]],
    *,
    title: str = "自选股洞察 · 完整归因报告",
) -> bytes:
    """章节列表 → A4 PDF bytes（页眉标题 + 章节 + 页脚免责声明/页码）。"""
    _register_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("cnTitle", parent=styles["Title"], fontName=_FONT, fontSize=18, leading=26)
    heading_style = ParagraphStyle(
        "cnHeading", parent=styles["Heading2"], fontName=_FONT, fontSize=13, leading=19,
        spaceBefore=12, spaceAfter=4, textColor=colors.HexColor("#1B4E8C"),
    )
    body_style = ParagraphStyle("cnBody", parent=styles["BodyText"], fontName=_FONT, fontSize=10.5, leading=17)

    def _decorate(canvas: Any, doc: Any) -> None:  # noqa: ANN401 - reportlab 回调签名
        canvas.saveState()
        canvas.setFont(_FONT, 8)
        canvas.drawString(18 * mm, 12 * mm, _DISCLAIMER)
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=18 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=title,
    )
    story: list[Any] = [Paragraph(_escape(title), title_style), Spacer(1, 8)]
    for heading, lines in sections:
        story.append(Paragraph(_escape(heading), heading_style))
        for line in lines:
            story.append(Paragraph(_escape(line), body_style))
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buffer.getvalue()