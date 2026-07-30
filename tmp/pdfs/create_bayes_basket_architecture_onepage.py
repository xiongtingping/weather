from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit
import os


OUT = "/Users/xiong/weather/data/reports/polymarket_wallet_0x8fbd7cf5_20260721/doctrine/bayes_basket_target_architecture_v1_onepage.pdf"
W, H = landscape(A3)
FONT_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT = "STHeitiMedium"
pdfmetrics.registerFont(TTFont(FONT, FONT_PATH))


BG = HexColor("#F5F7FA")
NAVY = HexColor("#102A43")
BLUE = HexColor("#DCEEFF")
BLUE_DARK = HexColor("#2166A5")
GREEN = HexColor("#DFF3E4")
GREEN_DARK = HexColor("#247A45")
AMBER = HexColor("#FFF0C2")
AMBER_DARK = HexColor("#A56700")
RED = HexColor("#FCE0E0")
RED_DARK = HexColor("#A62B2B")
PURPLE = HexColor("#ECE3FF")
PURPLE_DARK = HexColor("#6546A3")
TEXT = HexColor("#1F2933")
MUTED = HexColor("#52606D")
LINE = HexColor("#9FB3C8")


def wrap(text, size, width):
    out = []
    for para in str(text).split("\n"):
        if para == "":
            out.append("")
        else:
            out.extend(simpleSplit(para, FONT, size, width))
    return out


def draw_rich_lines(c, x, y_top, lines, size, max_width, color=TEXT, leading=None):
    leading = leading or size * 1.28
    y = y_top
    for item in lines:
        if isinstance(item, tuple):
            text, col = item
        else:
            text, col = item, color
        for line in wrap(text, size, max_width):
            c.setFont(FONT, size)
            c.setFillColor(col)
            c.drawString(x, y, line)
            y -= leading
    return y


def box(c, x, y, w, h, title, body, fill=BLUE, accent=BLUE_DARK,
        title_size=16, body_size=11.4):
    c.setFillColor(fill)
    c.setStrokeColor(accent)
    c.setLineWidth(1.4)
    c.roundRect(x, y, w, h, 10, fill=1, stroke=1)
    c.setFillColor(accent)
    c.roundRect(x, y + h - 34, w, 34, 10, fill=1, stroke=0)
    c.rect(x, y + h - 34, w, 18, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont(FONT, title_size)
    c.drawString(x + 12, y + h - 24, title)
    body_x = x + 13
    body_y = y + h - 53
    body_w = w - 26
    if isinstance(body, str):
        body = body.split("\n")
    draw_rich_lines(c, body_x, body_y, body, body_size, body_w, TEXT, body_size * 1.28)


def label(c, x, y, text, size=11, color=MUTED):
    c.setFont(FONT, size)
    c.setFillColor(color)
    c.drawString(x, y, text)


def arrow(c, x1, y1, x2, y2, color=BLUE_DARK, width=3, dashed=False):
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    if dashed:
        c.setDash(8, 6)
    c.line(x1, y1, x2, y2)
    import math
    angle = math.atan2(y2 - y1, x2 - x1)
    ah = 11
    pts = [
        (x2, y2),
        (x2 - ah * math.cos(angle - 0.45), y2 - ah * math.sin(angle - 0.45)),
        (x2 - ah * math.cos(angle + 0.45), y2 - ah * math.sin(angle + 0.45)),
    ]
    p = c.beginPath()
    p.moveTo(*pts[0])
    p.lineTo(*pts[1])
    p.lineTo(*pts[2])
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.restoreState()


def pill(c, x, y, w, text, fill, color=TEXT, size=11):
    c.setFillColor(fill)
    c.setStrokeColor(fill)
    c.roundRect(x, y, w, 25, 12, fill=1, stroke=1)
    c.setFillColor(color)
    c.setFont(FONT, size)
    c.drawCentredString(x + w / 2, y + 8, text)


c = canvas.Canvas(OUT, pagesize=(W, H))
c.setTitle("Bayes概率核心 × 自适应最高温篮子系统架构总图")
c.setAuthor("Codex / 熊先生")
c.setSubject("Target Architecture v1.13.0; TARGET_PRODUCTION_NO_LIVE")
c.setFillColor(BG)
c.rect(0, 0, W, H, fill=1, stroke=0)

M = 30

# Header
c.setFillColor(NAVY)
c.rect(0, H - 60, W, 60, fill=1, stroke=0)
c.setFillColor(white)
c.setFont(FONT, 17)
c.drawString(M, H - 22, "Bayes 概率核心 × 自适应最高温篮子")
c.setFont(FONT, 9)
c.drawString(M, H - 37, "系统架构层级总图 | 数据 -> 概率 -> 篮子 -> 决策 -> 执行/对账")
pill(c, W - 315, H - 31, 95, "PRODUCTION_NO_LIVE", GREEN_DARK, white, 6.2)
pill(c, W - 215, H - 31, 70, "授权=false", RED_DARK, white, 6.7)
pill(c, W - 150, H - 31, 130, "v1.13.0 | 2026-07-25", BLUE_DARK, white, 6.7)
label(c, M, H - 52, "target architecture v1.13.0 | 单页架构图 | 不代表已打开 live", 6.5, HexColor("#D9E2EC"))

# Top charter
charter_y = 715
charter_h = 65
box(c, M, charter_y, W - 2 * M, charter_h, "顶层设计宪章（唯一目标）", [
    "概率驱动库存管理 -> 自适应篮子 -> 组合收益优化",
    "统一决策入口：L4 UnifiedBasketOptimizer",
], fill=PURPLE, accent=PURPLE_DARK, title_size=9.5, body_size=8.3)

# Main architecture row
main_y = 410
main_h = 240
cols = [
    (20, 185),
    (213, 185),
    (406, 185),
    (599, 185),
    (792, 185),
    (985, 185),
]

box(c, cols[0][0], main_y, cols[0][1], main_h, "L0/L1 宪章 + 合同事实", [
    "授权、资源、生产隔离",
    "官方事实、ET、站点和桶合同",
    "AMSC/AWC 整点正式来源",
], fill=RED, accent=RED_DARK, title_size=8.8, body_size=8)

box(c, cols[1][0], main_y, cols[1][1], main_h, "L2 Bayes 编译平面", [
    "三模型校准",
    "模型组合",
    "概率版本输出",
], fill=BLUE, accent=BLUE_DARK, title_size=8.8, body_size=8)

box(c, cols[2][0], main_y, cols[2][1], main_h, "L3 物化概率边界", [
    "P0-M：早期概率",
    "P0-F：过程概率",
    "P1：整点实况概率",
    "有效桶与排除掩码",
], fill=GREEN, accent=GREEN_DARK, title_size=8.8, body_size=8)

box(c, cols[3][0], main_y, cols[3][1], main_h, "L4 统一篮子决策核心", [
    "概率 -> 目标仓位",
    "目标仓位 -> 动态库存",
    "库存 -> 补仓动作",
    "统一输出篮子计划",
], fill=AMBER, accent=AMBER_DARK, title_size=8.8, body_size=8)

box(c, cols[4][0], main_y, cols[4][1], main_h, "L5 原子决策 + L6 执行", [
    "原子决策",
    "官方 WS 盘口",
    "订单、成交、库存、资本",
    "执行与对账",
], fill=BLUE, accent=BLUE_DARK, title_size=8.8, body_size=8)

box(c, cols[5][0], main_y, cols[5][1], main_h, "L7 审计、结算反馈与晋级", [
    "结算",
    "审计",
    "反馈 L2/L3/L4",
], fill=PURPLE, accent=PURPLE_DARK, title_size=8.8, body_size=8)

# Arrows between main boxes
mid_y = main_y + main_h / 2
for i in range(5):
    x1 = cols[i][0] + cols[i][1] + 2
    x2 = cols[i + 1][0] - 2
    arrow(c, x1, mid_y, x2, mid_y, BLUE_DARK, 3)

# L0 safety boundary around central architecture
c.saveState()
c.setDash(12, 8)
c.setStrokeColor(RED_DARK)
c.setLineWidth(2)
c.roundRect(30, main_y - 18, W - 60, main_h + 36, 14, fill=0, stroke=1)
c.restoreState()
label(c, 25, main_y + main_h + 3, "系统级安全边界：授权 / 资源 / 生产隔离", 6.5, RED_DARK)

# Bottom panels
bottom_y = 55
bottom_h = 310
b1_x, b1_w = 20, 270
b2_x, b2_w = 298, 365
b3_x, b3_w = 671, 190
b4_x, b4_w = 869, 290

box(c, b1_x, bottom_y, b1_w, bottom_h, "主流程与状态流转", [
    "事实 -> 概率 -> 篮子 -> 决策 -> 执行 -> 对账 -> 审计",
    "P0-M -> P0-F -> P1",
    "更新即重算",
], fill=GREEN, accent=GREEN_DARK, title_size=8.8, body_size=8)

box(c, b2_x, bottom_y, b2_w, bottom_h, "层级接口与输入输出", [
    "L0/L1 -> L2 -> L3 -> L4 -> L5/L6 -> L7",
    "上游提供事实与概率",
    "中游形成篮子与决策",
    "下游执行并反馈",
], fill=BLUE, accent=BLUE_DARK, title_size=8.8, body_size=8)

box(c, b3_x, bottom_y, b3_w, bottom_h, "独立 HARD NO 泳道", [
    "AMSC/AWC 整点事实",
    "硬下界 -> NO",
    "独立于概率与篮子",
], fill=RED, accent=RED_DARK, title_size=8.8, body_size=8)

box(c, b4_x, bottom_y, b4_w, bottom_h, "系统运行边界与服务组成", [
    "production_no_live",
    "三服务边界",
    "受保护生产线隔离",
    "资源有界、数据最小留存",
], fill=AMBER, accent=AMBER_DARK, title_size=8.8, body_size=8)

# Footer and legend
footer_y = 12
c.setStrokeColor(LINE)
c.setLineWidth(1)
c.line(M, footer_y + 8, W - M, footer_y + 8)
label(c, M, footer_y - 4, "蓝=数据/执行 | 绿=事实/主链 | 黄=决策核心 | 红=隔离 | 紫=目标/审计", 6.5, MUTED)
label(c, W - 300, footer_y - 4, "target architecture v1.13.0 | 单页 A3", 6.5, MUTED)

c.showPage()
c.save()
print(OUT)
print(f"page_size_pt={W:.0f}x{H:.0f}")
