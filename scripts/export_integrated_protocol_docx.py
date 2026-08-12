#!/usr/bin/env python3
"""
将 INTEGRATED_QUALITY_PROTOCOL.md (v1.1) 转换为 Word (.docx) 格式
输出: ~/Downloads/DSL_集成质量检测协议_v1.1.docx
"""
import os, sys
from docx import Document
from docx.shared import Inches, Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn

OUTPUT_PATH = os.path.expanduser("~/Downloads/DSL_集成质量检测协议_v1.1.docx")

doc = Document()

# ── Page setup ──
for section in doc.sections:
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

style = doc.styles['Normal']
font = style.font
font.name = 'Calibri'
font.size = Pt(10)

# ── Helpers ──
def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

BLUE = hex_to_rgb("1F4E79")
DARK = hex_to_rgb("2D3436")
GRAY = hex_to_rgb("636E72")
RED = hex_to_rgb("C0392B")
ORANGE = hex_to_rgb("E67E22")
GREEN = hex_to_rgb("27AE60")

def add_formatted_paragraph(doc, text, bold=False, size=10, color=None, alignment=None, space_after=6, space_before=0):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(space_before)
    if alignment is not None:
        p.alignment = alignment
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = 'Calibri'
    if color:
        run.font.color.rgb = RGBColor(*color)
    return p

def shade_cell(cell, color_hex):
    shading = cell._element.get_or_add_tcPr()
    shd = shading.makeelement(qn('w:shd'), {
        qn('w:fill'): color_hex,
        qn('w:val'): 'clear'
    })
    shading.append(shd)

def set_cell_text(cell, text, bold=False, size=8.5, color=None):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(1)
    run = p.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = 'Calibri'
    if color:
        run.font.color.rgb = RGBColor(*color)

def add_table_from_data(doc, headers, rows, col_widths=None):
    """Add a formatted table with header shading and alternating row colors."""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # Header row
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        set_cell_text(cell, h, bold=True, size=8, color=(255, 255, 255))
        shade_cell(cell, "1F4E79")
    # Data rows
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = table.rows[ri + 1].cells[ci]
            set_cell_text(cell, val, size=8)
            if ri % 2 == 1:
                shade_cell(cell, "EBF5FB")
    # Column widths
    if col_widths:
        for ri in range(len(rows) + 1):
            for ci, w in enumerate(col_widths):
                if ci < len(table.rows[ri].cells):
                    table.rows[ri].cells[ci].width = Cm(w)
    doc.add_paragraph()  # spacer
    return table

def add_vibe_trap(doc, text):
    """Add a Vibe Coding trap warning box."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)
    # Emoji
    run = p.add_run("🤖 Vibe Coding陷阱: ")
    run.bold = True; run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(*RED); run.font.name = 'Calibri'
    # Text
    run2 = p.add_run(text)
    run2.font.size = Pt(8.5); run2.font.italic = True
    run2.font.color.rgb = RGBColor(*RED); run2.font.name = 'Calibri'

def add_domain_section(doc, domain_id, domain_name, domain_en, levels_data):
    """Add a complete domain section with L0-L3 tables."""
    add_formatted_paragraph(doc, f"领域 {domain_id}: {domain_name}", bold=True, size=14, color=BLUE, space_after=4)
    add_formatted_paragraph(doc, domain_en, size=8, color=GRAY, space_after=8)

    for level_code, level_name, check_items, trap_text in levels_data:
        add_formatted_paragraph(doc, f"{level_code} — {level_name}", bold=True, size=10, color=DARK, space_after=4)
        headers = ["ID", "检查项", "方法", "量化阈值", "来源"]
        rows = []
        for cid, cname, cmethod, cthreshold, csource in check_items:
            rows.append([cid, cname, cmethod, cthreshold, csource])
        add_table_from_data(doc, headers, rows, col_widths=[1.8, 3.0, 4.5, 3.5, 1.8])
        if trap_text:
            add_vibe_trap(doc, trap_text)

# ══════════════════════════════════════
# TITLE PAGE
# ══════════════════════════════════════
doc.add_paragraph()
add_formatted_paragraph(doc, "DSL 量化交易系统", bold=True, size=24, color=BLUE, alignment=WD_ALIGN_PARAGRAPH.CENTER, space_after=4)
add_formatted_paragraph(doc, "集成质量检测协议 v1.1", bold=True, size=18, color=DARK, alignment=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)
add_formatted_paragraph(doc, "10大领域 × 4层深度 × 量化阈值 × 加权评分", size=11, color=GRAY, alignment=WD_ALIGN_PARAGRAPH.CENTER, space_after=24)

# Changelog
add_formatted_paragraph(doc, "v1.1 变更 (2026-05-23)", bold=True, size=10, color=BLUE, space_after=4)
changes = [
    "+ D3回测保真度补充L0/L1基础检查 (填补结构缺陷)",
    "+ 新增 D10 安全领域 (注入风险/密钥暴露/公网暴露)",
    "+ 所有检查项新增「量化阈值」列, 明确PASS/WARN/FAIL判定条件",
    "+ 评分模型改为加权: PASS=1.0, WARN=0.5, FAIL=0.0",
    "+ 新增基准对比检查 D3.L2.5",
    "+ 权重再平衡 (D5恢复至6%, 新增D10=3%)",
    "+ 新增: 检查项依赖拓扑 / 误报管理机制 / 协议治理 / FAIL处理SOP",
]
for c in changes:
    add_formatted_paragraph(doc, c, size=8.5, color=DARK, space_after=1)

doc.add_page_break()

# ══════════════════════════════════════
# SECTION 1: OVERVIEW
# ══════════════════════════════════════
add_formatted_paragraph(doc, "一、总览矩阵与运行策略", bold=True, size=16, color=BLUE, space_after=12)

matrix_headers = ["领域", "L0 自动化", "L1 快速", "L2 标准", "L3 深度"]
matrix_rows = [
    ["D1 代码与配置", "████░░░░", "██░░░░░░", "░░░░░░░░", "░░░░░░░░"],
    ["D2 数据管线", "██░░░░░░", "███░░░░░", "████░░░░", "████████"],
    ["D3 回测保真度", "██░░░░░░", "███░░░░░", "█████░░░", "████████"],
    ["D4 模型质量", "░░░░░░░░", "██░░░░░░", "█████░░░", "████████"],
    ["D5 DSL设计", "█░░░░░░░", "░░░░░░░░", "████░░░░", "████████"],
    ["D6 风控", "██░░░░░░", "████░░░░", "██████░░", "████████"],
    ["D7 执行层", "░░░░░░░░", "███░░░░░", "█████░░░", "████████"],
    ["D8 系统工程与运维", "██░░░░░░", "██████░░", "██████░░", "██████░░"],
    ["D9 跨领域集成", "█░░░░░░░", "████░░░░", "██████░░", "█████░░░"],
    ["D10 安全", "███░░░░░", "██░░░░░░", "████░░░░", "███░░░░░"],
]
add_table_from_data(doc, matrix_headers, matrix_rows, col_widths=[3.0, 2.5, 2.5, 2.5, 2.5])
add_formatted_paragraph(doc, "█ = 该层级有覆盖    ░ = 该层级不适用", size=8, color=GRAY, space_after=12)

# Run strategy
add_formatted_paragraph(doc, "运行策略总览", bold=True, size=12, color=BLUE, space_after=6)
run_levels = [
    ("L0 自动化层", "触发: git push / 每次文件修改 | 工具: smoke_test.py --level L0 | 输出: ✅/❌ 二进制结果, 失败阻断push | 耗时: < 10秒"),
    ("L1 快速层", "触发: 每日cron (建议 04:00) | 工具: smoke_test.py --level L1 | 输出: 飞书通知 (仅告警) | 耗时: < 60秒"),
    ("L2 标准层", "触发: 每周六 10:00 | 工具: quality_audit.py --level L2 + 人工审查清单 | 输出: 周度质量报告 (飞书Bitable) | 耗时: ~10分钟"),
    ("L3 深层", "触发: 版本发布前 / 每月第一周 | 工具: quant_full_audit.py + 人工专家审查 | 输出: 完整审查报告 (含P0/P1/P2) | 耗时: 1-3小时"),
]
for level_name, desc in run_levels:
    p = doc.add_paragraph()
    run = p.add_run(f"{level_name}: ")
    run.bold = True; run.font.size = Pt(9); run.font.name = 'Calibri'
    run2 = p.add_run(desc)
    run2.font.size = Pt(9); run2.font.name = 'Calibri'

doc.add_page_break()

# ══════════════════════════════════════
# SECTION 2: DOMAIN DETAILS
# ══════════════════════════════════════
add_formatted_paragraph(doc, "二、领域详情", bold=True, size=16, color=BLUE, space_after=12)

# ── D1 ──
add_domain_section(doc, "D1", "代码与配置健康", "Code & Config Health", [
    ("L0", "自动化 (每次git push)", [
        ("D1.L0.1", "Python语法检查", "find . -name \"*.py\" -not -path \"*/_archive/*\" | xargs py_compile", "PASS: 0 syntax error; FAIL: ≥1 error", "docx L0.1"),
        ("D1.L0.2", "导入完整性验证", "python3 -c \"import core, config, ...\"", "PASS: 0 ImportError; FAIL: ≥1 import失败", "docx L0.3"),
        ("D1.L0.3", "VERSION文件一致性", "扫描所有文件中的版本号 vs VERSION", "PASS: 全部一致; WARN: ≤2处; FAIL: >2处", "docx L0.6"),
        ("D1.L0.4", "硬编码Token/密码检测", "grep -rn 'token|secret|password' | grep -v 'os.getenv' | grep -v '^#'", "PASS: 0命中; FAIL: ≥1命中", "skill P0"),
    ], "AI生成的代码经常把API key硬编码在注释里，grep正则需排除注释行。"),
    ("L1", "快速 (每日cron)", [
        ("D1.L1.1", "配置文件YAML有效性", "逐文件 yaml.safe_load()", "PASS: 全部有效; FAIL: ≥1解析失败", "docx L2.5"),
        ("D1.L1.2", "feature_flags与实际一致", "cron provider标注 vs 实际调度器", "PASS: 一致; WARN: 过期; FAIL: 矛盾", "docx L2.3"),
        ("D1.L1.3", "死代码增量扫描", "vulture --min-confidence 80 仅扫描新增函数", "PASS: 0新增; WARN: 1-3; FAIL: >3", "docx L0.2"),
    ], None),
    ("L2", "标准 (每周审查)", [
        ("D1.L2.1", "依赖锁定对比", "pip freeze vs requirements.txt", "PASS: ≤3差异; WARN: 4-10; FAIL: >10", "skill 维度7"),
        ("D1.L2.2", "测试覆盖率", "pytest --cov", "PASS: ≥60%; WARN: 30-59%; FAIL: <30%", "docx L0.5"),
    ], None),
    ("L3", "深度 (版本发布前)", [
        ("D1.L3.1", "全量死代码审计", "vulture + 人工确认后清理", "PASS: 清理完毕; WARN: ≤5遗留; FAIL: >5", "skill"),
        ("D1.L3.2", "依赖漏洞扫描", "pip-audit 或 safety check", "PASS: 0高危; WARN: 仅低危; FAIL: ≥1高危CVE", "skill"),
    ], None),
])

# ── D2 ──
add_domain_section(doc, "D2", "数据管线完整性", "Data Pipeline Integrity", [
    ("L0", "自动化", [
        ("D2.L0.1", "数据源Token环境变量检测", "os.getenv('TUSHARE_TOKEN'), MAIRUI_LICENCE", "PASS: 主源Token存在; WARN: 仅备用; FAIL: 无Token", "skill P0"),
        ("D2.L0.2", "节假日数据覆盖检查", "当前年份±1 是否有节假日数据", "PASS: ±1年均有; WARN: 仅当年; FAIL: 当年也无", "docx+skill"),
    ], "AI喜欢写 return None 或 # TODO: 后续实现 作为数据源fallback。"),
    ("L1", "快速", [
        ("D2.L1.1", "数据源可用性探测", "每个数据源发1次真实请求 (超时5s)", "PASS: 主源<5s; WARN: 仅备用; FAIL: 全超时", "docx L2.4"),
        ("D2.L1.2", "ST股票池与模型一致性", "ST标记的股票是否从候选池排除", "PASS: 0只ST混入; FAIL: ≥1只", "skill 维度1"),
        ("D2.L1.3", "数据新鲜度检查", "最近K线日期 vs 最近交易日", "PASS: ≤1交易日; WARN: 2-3; FAIL: >3", "docx L1.2"),
    ], None),
    ("L2", "标准", [
        ("D2.L2.1", "复权一致性验证", "训练特征 vs 预测时的复权方式", "PASS: 一致; FAIL: 不一致", "skill 维度1"),
        ("D2.L2.2", "数据源优先级实际生效", "模拟主源故障 → 验证fallback触发", "PASS: fallback触发+完整; FAIL: 未触发/缺失", "skill 维度1"),
        ("D2.L2.3", "停牌/退市股数据质量", "抽查停牌期间OHLCV填充策略", "PASS: 策略一致; WARN: 部分不一致; FAIL: NaN", "skill 维度1"),
    ], None),
    ("L3", "深度", [
        ("D2.L3.1", "前视偏差全量扫描", "搜索shift(-1)/rolling(center=True)/pct_change()无.shift(1)", "PASS: 0泄漏; WARN: 已豁免; FAIL: ≥1未豁免", "skill 维度1"),
        ("D2.L3.2", "涨跌停日数据失真系数", "涨停日OHLCV特殊标记是否生效", "PASS: 有标记; WARN: 未降权; FAIL: 无标记", "skill 维度1"),
        ("D2.L3.3", "幸存者偏差检测", "当前股票池 vs 历史全量 → 退市股是否缺失", "PASS: 退市股保留; FAIL: 被删除", "skill 维度1"),
    ], "df['ret_5d'] = df['close'].pct_change(5) 当天收盘价包含了当天涨幅→未来信息泄露。正确: .pct_change(5).shift(1)"),
])

# ── D3 ──
add_domain_section(doc, "D3", "回测保真度", "Backtest Fidelity", [
    ("L0", "自动化 (v1.1新增)", [
        ("D3.L0.1", "回测代码可运行性", "给定10只股票×100天样本数据, 运行不崩溃", "PASS: 正常结束+非空; FAIL: 崩溃/空DataFrame", "新增 v1.1"),
        ("D3.L0.2", "回测输出完整性", "回测结果包含: timestamp/参数快照/交易记录/绩效", "PASS: 4项齐全; WARN: 缺1项; FAIL: 缺≥2项", "新增 v1.1"),
    ], None),
    ("L1", "快速 (v1.1新增)", [
        ("D3.L1.1", "回测基本指标合理性", "年化收益率/夏普比率/最大回撤在sane范围", "PASS: -99%≤年化≤500%, |夏普|≤10; FAIL: 超出", "新增 v1.1"),
        ("D3.L1.2", "回测交易记录非空", "有信号的日子是否产生了交易记录", "PASS: 交易记录>0; WARN: =0但有信号; FAIL: 空", "新增 v1.1"),
    ], None),
    ("L2", "标准", [
        ("D3.L2.1", "T+1买卖约束验证", "回测记录中买入日与卖出日是否同一天", "PASS: 0同日买卖; FAIL: ≥1", "skill 维度2"),
        ("D3.L2.2", "涨跌停不可交易验证", "回测日志中涨停日是否有买入记录", "PASS: 0涨停买入; FAIL: ≥1", "skill 维度2"),
        ("D3.L2.3", "最小交易单位验证", "回测买入数量是否为100整数倍(科创板200)", "PASS: 0非整手; FAIL: ≥1", "skill 维度2"),
        ("D3.L2.4", "交易成本核算", "佣金+印花税+最低佣金+滑点四者是否全部计入", "PASS: 4项全有; WARN: 缺滑点; FAIL: 缺佣金/印花税", "skill 维度2"),
        ("D3.L2.5", "基准对比 (v1.1新增)", "策略收益 vs 沪深300/中证500同期买入持有收益", "PASS: 信息比率>0; WARN: 超额<0但波动低; FAIL: <无风险利率", "新增 v1.1"),
    ], None),
    ("L3", "深度", [
        ("D3.L3.1", "回测引擎非空壳验证", "run_strategy()方法体行数 vs 注释行数", "PASS: 代码行>注释行; FAIL: 注释行≥代码行", "skill 维度2"),
        ("D3.L3.2", "Walk-forward vs K-Fold对比", "两种分割方式跑同一策略, 收益率差异", "PASS: ≤20%; WARN: 20-50%; FAIL: >50%", "skill 维度3"),
        ("D3.L3.3", "滑点模型合理性", "小盘股固定3bp vs 实盘价差", "PASS: 模型≥实盘均值; FAIL: 模型<实盘", "skill 维度2"),
        ("D3.L3.4", "成交量约束验证", "买入金额>日均成交额×5%→是否被拦截", "PASS: 被拦截; FAIL: 通过", "skill 维度2"),
        ("D3.L3.5", "模拟vs实盘成交一致性 (v1.1)", "选取N日实盘挂单, 对比模拟器同条件成交判断", "PASS: 一致率≥90%; WARN: 80-90%; FAIL: <80%", "新增 v1.1"),
    ], "DSLExecutor定义了完整的方法签名和docstring，但run_strategy()返回全零数据。验证: 看核心方法体是否比注释短。"),
])

# ── D4 ──
add_domain_section(doc, "D4", "模型质量", "Model Quality", [
    ("L1", "快速", [
        ("D4.L1.1", "模型文件完整性", "每只股票是否有 .pkl/.npy 文件", "PASS: 覆盖率≥90%; WARN: 70-89%; FAIL: <70%", "docx L2.2"),
        ("D4.L1.2", "训练时效性", "最近模型文件修改时间 vs 当前日期", "PASS: ≤7天; WARN: 8-14天; FAIL: >14天", "skill 维度3"),
    ], None),
    ("L2", "标准", [
        ("D4.L2.1", "逐股方向准确率统计", "从calibration/feedback计算每只股票accuracy", "PASS: 均值≥0.55; WARN: 0.50-0.54; FAIL: <0.50", "docx L3.1"),
        ("D4.L2.2", "低精度模型识别", "accuracy < 0.45 的股票列表", "PASS: 0只; WARN: 1-3只; FAIL: ≥4只或>10%", "skill 维度3"),
        ("D4.L2.3", "过拟合差距", "训练集accuracy vs 验证集accuracy", "PASS: ≤10%; WARN: 10-20%; FAIL: >20%", "skill 维度3"),
        ("D4.L2.4", "PSI特征漂移检测", "从psi_monitor获取最新PSI值", "PASS: PSI<0.1; WARN: 0.1-0.25; FAIL: >0.25", "skill 维度3"),
    ], None),
    ("L3", "深度", [
        ("D4.L3.1", "标签构造前视泄漏", "检查shift(-1)/用t+1数据算t日标签", "PASS: 0泄漏; FAIL: ≥1泄漏", "skill 维度3"),
        ("D4.L3.2", "止损与预测期望值一致性", "止损-8% vs 预测涨3%胜率55% → 期望值<0?", "PASS: 期望值>0; WARN: -0.5%~0%; FAIL: <-0.5%", "skill 维度3"),
        ("D4.L3.3", "模型衰减曲线", "训练后N天的预测精度变化趋势", "PASS: 30天衰减<10%; WARN: 10-20%; FAIL: >20%", "skill 维度3"),
        ("D4.L3.4", "特征重要性稳定性", "最近3次训练的特征重要性排名相关系数", "PASS: ≥0.7; WARN: 0.5-0.69; FAIL: <0.5", "skill 维度3"),
    ], "\"用当天收盘价算当天标签\"——收盘价在盘后才确定，等于在预测时用了未来信息。"),
])

# ── D5 ──
add_domain_section(doc, "D5", "DSL设计质量", "DSL Design Quality", [
    ("L0", "自动化", [
        ("D5.L0.1", "DSL Schema验证", "JSON Schema校验是否能捕获常见错误", "PASS: 捕获≥5类; WARN: 3-4类; FAIL: <3类", "skill 维度4"),
    ], None),
    ("L2", "标准", [
        ("D5.L2.1", "DSL原语完备性", "策略模式覆盖率: 均线/布林带/MACD/RSI/量价背离/连板", "PASS: ≥5种; WARN: 3-4种; FAIL: <3种", "skill 维度4"),
        ("D5.L2.2", "DSL→执行可翻译性", "随机3个策略JSON→验证引擎真实计算", "PASS: 3/3真实计算; FAIL: ≥1占位", "skill 维度4"),
    ], None),
    ("L3", "深度", [
        ("D5.L3.1", "DSL执行引擎非空壳", "run_strategy()是否真的运行回测循环", "PASS: 产生≥1笔交易; FAIL: 0笔", "skill 维度4"),
        ("D5.L3.2", "错误反馈质量", "传入非法参数→错误信息含字段名+原因+建议", "PASS: 3项齐全; WARN: 缺1项; FAIL: 仅\"error\"", "skill 维度4"),
        ("D5.L3.3", "策略表达力边界", "能否表达\"涨5%回撤3%卖出\"等复杂逻辑", "PASS: 可表达; FAIL: 不可", "skill 维度4"),
    ], "DSL是AI的\"舒适区\"——文档、Schema、接口定义都很漂亮，但核心执行引擎可能是空的。"),
])

# ── D6 ──
add_domain_section(doc, "D6", "风控", "Risk Control", [
    ("L0", "自动化 (v1.1新增)", [
        ("D6.L0.1", "风控模块可导入", "from core.risk_manager import RiskManager", "PASS: 导入成功; FAIL: ImportError", "新增 v1.1"),
        ("D6.L0.2", "风控配置文件有效性", "circuit_breaker.json, black_swan_status.json可解析", "PASS: 2文件均可解析; FAIL: ≥1失败", "新增 v1.1"),
    ], None),
    ("L1", "快速", [
        ("D6.L1.1", "熔断器状态检查", "circuit_breaker.json → trading_paused", "PASS: not paused; FAIL: paused (阻断交易)", "docx L4.1"),
        ("D6.L1.2", "黑天鹅仓位比例", "black_swan_status.json → position_ratio", "PASS: ≥0.5; WARN: 0.25-0.49; FAIL: <0.25", "docx L4.2"),
        ("D6.L1.3", "持仓集中度", "单行业/板块占比(阈值40%)", "PASS: ≤40%; WARN: 40-60%; FAIL: >60%", "docx L4.4"),
    ], None),
    ("L2", "标准", [
        ("D6.L2.1", "板块差异化止损验证", "主板-8%/创业板-15%/科创板-15%/ST-5%", "PASS: 4板块正确; WARN: 1偏差; FAIL: ≥2偏差", "skill 维度5"),
        ("D6.L2.2", "涨跌停逻辑一致性", "circuit_breaker/risk_manager/paper_trader的LIMIT_RULES", "PASS: 三处一致; FAIL: 不一致", "skill 维度5"),
        ("D6.L2.3", "止盈连接验证", "止盈触发→实际卖出的代码路径", "PASS: 路径完整; FAIL: 断开", "skill 维度5"),
        ("D6.L2.4", "LPPL风险评分合理性", "risk_score=95→仓位应自动降至20%", "PASS: ≤20%; WARN: 20-30%; FAIL: >30%", "skill 维度5"),
    ], None),
    ("L3", "深度", [
        ("D6.L3.1", "止损调用链完整性", "check_single_position_stop在pre_execution_check中被调用?", "PASS: 有调用链; FAIL: 函数孤立", "skill 维度5"),
        ("D6.L3.2", "回撤计算分母验证", "用peak_equity而非initial_capital?", "PASS: 用peak; FAIL: 用initial", "skill 维度5"),
        ("D6.L3.3", "熔断恢复机制", "熔断触发后是否有手动恢复的UI或命令", "PASS: 有恢复机制; FAIL: 无", "skill 维度5"),
        ("D6.L3.4", "流动性风险", "持仓市值 vs 日均成交额 → 平仓需要多少天", "PASS: ≤3天; WARN: 3-7天; FAIL: >7天", "skill 维度5"),
    ], "风控是AI的盲区——AI倾向写\"进攻代码\"而忽略\"防守代码\"。写了check_single_position_stop()但没有在pre_execution_check()中调用=形同虚设。"),
])

# ── D7 ──
add_domain_section(doc, "D7", "执行层", "Execution Layer", [
    ("L1", "快速", [
        ("D7.L1.1", "订单幂等性检查", "idempotency.db中是否有重复订单ID", "PASS: 0重复; FAIL: ≥1重复", "skill 维度6"),
        ("D7.L1.2", "信号时效性", "预测生成时间 vs 开盘时间(9:30)", "PASS: <9:20; WARN: 9:20-9:29; FAIL: ≥9:30", "skill 维度6"),
    ], None),
    ("L2", "标准", [
        ("D7.L2.1", "决策链路完整性", "morning_decision→risk_manager→execute_trade每步错误处理", "PASS: 3步全有; WARN: 缺1步; FAIL: 缺≥2步", "skill 维度6"),
        ("D7.L2.2", "部分成交处理", "partial_fill/fill_ratio在paper_trader中调用?", "PASS: 有调用+完整; FAIL: 无/空函数", "skill 维度6"),
    ], None),
    ("L3", "深度", [
        ("D7.L3.1", "崩溃恢复一致性", "SQLite vs JSON持仓在模拟崩溃后是否一致", "PASS: 完全一致; WARN: ≤2字段差异; FAIL: 数量不同", "skill 维度6"),
        ("D7.L3.2", "午盘休市处理", "11:30-13:00是否有订单执行→应被拦截", "PASS: 0休市订单; FAIL: ≥1", "skill 维度6"),
        ("D7.L3.3", "API限流熔断", "数据源连续3次失败后是否有退避(指数退避≥1s)", "PASS: 有退避+恢复; FAIL: 无限重试/无退避", "skill 维度6"),
    ], "AI倾向于完美假设——网络不会断、API不会挂、不会crash。真实世界需要处理重试、幂等、崩溃恢复。"),
])

# ── D8 ──
add_domain_section(doc, "D8", "系统工程与Dashboard", "System Engineering & Dashboard", [
    ("L0", "自动化", [
        ("D8.L0.1", "Git状态检查", "git status --short 未提交文件数", "PASS: ≤5; WARN: 6-15; FAIL: >15", "skill 维度7"),
        ("D8.L0.2", "Git提交时效性", "最后一次提交时间 vs 当前时间", "PASS: ≤3天; WARN: 4-7天; FAIL: >7天", "skill 维度7"),
        ("D8.L0.3", "前端版本号 vs VERSION文件", "index.html/server.py硬编码版本 vs VERSION", "PASS: 三方一致; WARN: 仅fallback; FAIL: 前后端均不同", "新增"),
    ], "AI在HTML<title>、FastAPI version=、version_data fallback等多处硬编码版本号。改VERSION时不会自动更新。"),
    ("L1", "快速", [
        ("D8.L1.1", "Cron任务健康扫描", "连续错误数/状态未知任务/超时任务", "PASS: 0错误; WARN: 1-3错误; FAIL: >3错误", "docx L5"),
        ("D8.L1.2", "Dashboard运行状态", "进程存活+API可用性(HTTP 200 on /api/status)", "PASS: 200+<2s; FAIL: 无响应/超时", "docx L6.1"),
        ("D8.L1.3", "备份完整性", "最近备份时间 vs 预期时间", "PASS: ≤26h; WARN: 26-50h; FAIL: >50h", "docx L8.1"),
        ("D8.L1.4", "磁盘空间", "/tmp/dslcache大小, 项目目录大小", "PASS: <80%; WARN: 80-95%; FAIL: >95%", "skill 维度7"),
        ("D8.L1.5", "API响应字段完整性", "抽样3个核心API, 前端JS引用字段 vs 实际响应", "PASS: 100%存在; FAIL: ≥1缺失", "新增"),
        ("D8.L1.6", "前后端版本号三方一致", "VERSION vs /api/version vs index.html初始值", "PASS: 三方一致; FAIL: 不一致", "新增"),
        ("D8.L1.7", "双API数据源一致性", "/api/full中portfolio vs /api/paper-trader中positions", "PASS: 股票+数量一致; FAIL: 不一致", "新增"),
    ], "Dashboard双数据源: /api/full走JSON→SQLite fallback; /api/paper-trader直读SQLite。两条路径字段名、来源、口径都可能不同。"),
    ("L2", "标准", [
        ("D8.L2.1", "crontab vs 调度器同步", "crontab.conf任务数 vs OpenClaw调度器任务数", "PASS: 覆盖全部; WARN: ≤3仅crontab; FAIL: >3", "docx L2.1"),
        ("D8.L2.2", "告警渠道可用性", "飞书Webhook发送测试消息", "PASS: 200响应; FAIL: 无响应/4xx/5xx", "skill 维度7"),
        ("D8.L2.3", "API错误响应前端展示", "模拟后端500/超时/空数组→前端展示错误还是空白?", "PASS: 展示错误; FAIL: 空白/旧数据", "新增"),
        ("D8.L2.4", "WebSocket vs REST状态一致", "/ws/progress推送 vs /api/pipeline返回同任务状态", "PASS: 一致; FAIL: 冲突", "新增"),
        ("D8.L2.5", "重训状态机前后端一致", "/api/retrain-status→_retrain_status内存→retrain_queue.json", "PASS: 三方一致; FAIL: 不一致(尤其重启后)", "新增"),
        ("D8.L2.6", "筛选器参数前后端映射", "前端筛选器参数 vs /api/screener query params", "PASS: 一一对应; FAIL: 断裂", "新增"),
    ], "_retrain_status是内存字典, 服务重启后消失。前端轮询返回unknown, 但retrain_queue.json已记录done。"),
    ("L3", "深度", [
        ("D8.L3.1", "配置集中管理审计", "参数散落在几个文件", "PASS: ≤3配置源; WARN: 4-5; FAIL: >5", "skill 维度7"),
        ("D8.L3.2", "单点故障分析", "列出所有\"挂了就全停\"的依赖", "PASS: 每个SPOF有降级; FAIL: ≥1无降级", "skill 维度7"),
        ("D8.L3.3", "Dashboard字段映射矩阵", "逐API端点: 后端返回字段×前端渲染字段", "PASS: 0断裂; FAIL: ≥1断裂", "新增"),
        ("D8.L3.4", "前端崩溃恢复完整性", "后端重启后: WS重连+REST重试+轮询恢复", "PASS: 30s内恢复; FAIL: 需手动刷新", "新增"),
        ("D8.L3.5", "Dashboard数值口径一致性", "同一指标在不同卡片/tab中的数值来源", "PASS: 同指标同源; FAIL: 同指标不同值", "新增"),
        ("D8.L3.6", "环境可复现性 (v1.1)", "git clone→pip install→smoke_test.py全流程通过", "PASS: 全流程通过; FAIL: 任一步失败", "新增 v1.1"),
    ], "口径分裂: \"总收益率\"在总览卡片来自get_system_status(), 模拟交易tab来自/api/paper-trader, 精度趋势tab从calibration推算。三个地方三个数。"),
])

# ── D9 ──
add_domain_section(doc, "D9", "跨领域集成", "Cross-cutting Integration", [
    ("L0", "自动化", [
        ("D9.L0.1", "E2E模块导入链", "constants→data_loader→signal_generator→risk_manager→dsl_engine", "PASS: 全链导入成功; FAIL: ≥1环断裂", "docx L9.1"),
    ], None),
    ("L1", "快速", [
        ("D9.L1.1", "SQLite↔JSON持仓一致性", "paper_trading.db vs paper_trading_ledger.json", "PASS: 完全一致; FAIL: 不一致", "docx L7.1"),
        ("D9.L1.2", "股票池↔模型目录一致性", "master_stock_pool.yaml symbols vs models/子目录", "PASS: ≤5%偏差; WARN: 5-15%; FAIL: >15%", "docx L2.2"),
        ("D9.L1.3", "Smoke Test通过", "scripts/smoke_test.py全部通过", "PASS: exit=0; FAIL: exit≠0", "docx L9.3"),
    ], None),
    ("L2", "标准", [
        ("D9.L2.1", "信号→持仓闭环验证", "最近N交易日信号生成 vs 实际持仓变化", "PASS: ≥90%; WARN: 70-89%; FAIL: <70%", "skill"),
        ("D9.L2.2", "反馈闭环", "calibration_feedback→retrain_queue→batch_train→accuracy", "PASS: 4步完整; WARN: 缺1步; FAIL: 缺≥2步", "skill 维度3"),
        ("D9.L2.3", "Golden Test回归", "固定输入(10股×100天)→固定输出(预期信号)", "PASS: 完全匹配; FAIL: 不匹配", "docx L9.2"),
    ], None),
    ("L3", "深度", [
        ("D9.L3.1", "全链路追踪", "一笔交易: 预测→信号→风控→下单→成交→复盘, 每步时间戳", "PASS: 6步均有; WARN: 缺1-2步; FAIL: 缺≥3步", "skill 维度6"),
        ("D9.L3.2", "双系统对比", "paper_trading.db vs paper_trader.db行为差异", "PASS: 100%一致; WARN: 95-99%; FAIL: <95%", "skill"),
    ], None),
])

# ── D10 (v1.1 NEW) ──
add_domain_section(doc, "D10", "安全 (v1.1 新增)", "Security", [
    ("L0", "自动化", [
        ("D10.L0.1", "敏感文件Git追踪检查", "git ls-files | grep -E '.env$|.pem$|credentials|web_auth'", "PASS: 0敏感文件被追踪; FAIL: ≥1", "新增 v1.1"),
        ("D10.L0.2", "密钥文件权限检查", "stat web_auth.json验证权限=600", "PASS: 权限≤600; FAIL: >600", "新增 v1.1"),
        ("D10.L0.3", "依赖已知漏洞扫描", "pip-audit快速模式(Critical/High)", "PASS: 0高危; FAIL: ≥1高危CVE", "新增 v1.1"),
    ], None),
    ("L1", "快速", [
        ("D10.L1.1", "公网暴露面检测", "Dashboard是否通过Cloudflare Tunnel暴露到公网", "PASS: HTTPS+认证; WARN: 认证无HTTPS; FAIL: 无认证", "新增 v1.1"),
        ("D10.L1.2", "日志中密钥泄漏扫描", "grep -rn 'sk-|token=|eyJ' logs/ | grep -v 'os.getenv|REDACTED'", "PASS: 0明文; FAIL: ≥1明文", "新增 v1.1"),
    ], None),
    ("L2", "标准", [
        ("D10.L2.1", "API端点注入风险审计", "/api/retrain-now等接收用户输入的端点是否有校验", "PASS: 全部有校验; FAIL: ≥1无校验", "新增 v1.1"),
        ("D10.L2.2", "Session安全配置", "cookie: HttpOnly/SameSite/Secure(公网时)是否设置", "PASS: 三项齐全; WARN: 缺Secure(内网); FAIL: 缺HttpOnly", "新增 v1.1"),
        ("D10.L2.3", "飞书Webhook URL安全", "Webhook URL是否从环境变量读取(非硬编码)", "PASS: 从env读取; FAIL: 硬编码", "新增 v1.1"),
    ], None),
    ("L3", "深度", [
        ("D10.L3.1", "SQL注入风险审计", "所有SQLite查询是否使用参数化查询(非字符串拼接)", "PASS: 100%参数化; FAIL: ≥1拼接", "新增 v1.1"),
        ("D10.L3.2", "全量密钥扫描(git历史)", "git log -p | grep -E 'sk-[a-zA-Z0-9]{20,}'", "PASS: 0历史泄漏; FAIL: 发现泄漏→轮换密钥", "新增 v1.1"),
        ("D10.L3.3", "依赖供应链安全", "pip-audit --full + 检查依赖是否来自可信源", "PASS: 0已知漏洞+PyPI官方; FAIL: ≥1高危/非官方源", "新增 v1.1"),
    ], "AI生成Dashboard时默认CORS allow_origins=[\"*\"]、无认证中间件、命令行注入无校验。本地运行没问题, 暴露到公网就是严重漏洞。"),
])

doc.add_page_break()

# ══════════════════════════════════════
# SECTION 3: SCORING
# ══════════════════════════════════════
add_formatted_paragraph(doc, "三、评分体系", bold=True, size=16, color=BLUE, space_after=12)

add_formatted_paragraph(doc, "单项评分", bold=True, size=12, color=DARK, space_after=4)
add_formatted_paragraph(doc, "NA: 不适用 | PASS: 通过 (权重1.0) | WARN: 警告 (权重0.5) | FAIL: 失败 (权重0.0)", size=9, space_after=8)

add_formatted_paragraph(doc, "领域评分 (v1.1 加权公式)", bold=True, size=12, color=DARK, space_after=4)
add_formatted_paragraph(doc, "领域评分 = (PASS×1.0 + WARN×0.5 + FAIL×0.0) / (PASS+WARN+FAIL) × 100", size=10, color=BLUE, space_after=4)
add_formatted_paragraph(doc, "v1.1改进: WARN(0.5)与FAIL(0.0)区分严重程度。1个FAIL比3个WARN扣分更多。", size=8, color=GRAY, space_after=10)

add_formatted_paragraph(doc, "系统总评分 = Σ(领域评分 × 领域权重)", bold=True, size=10, color=DARK, space_after=8)

weight_headers = ["领域", "权重", "理由"]
weight_rows = [
    ("D2 数据管线", "20%", "垃圾进垃圾出 — 数据质量是系统基石"),
    ("D3 回测保真度", "20%", "回测不可信则一切无意义"),
    ("D4 模型质量", "15%", "模型是信号源头"),
    ("D6 风控", "15%", "个人投资者的最后防线"),
    ("D7 执行层", "10%", "策略收益能否真正兑现"),
    ("D5 DSL设计", "6%", "DSL是系统的核心抽象层 (v1.1恢复)"),
    ("D8 系统工程与Dashboard", "6%", "运维可靠性+前后端一致性"),
    ("D10 安全", "3%", "公网暴露/注入/密钥管理 (v1.1新增)"),
    ("D1 代码配置", "3%", "基础的代码卫生"),
    ("D9 跨领域集成", "2%", "端到端一致性"),
]
add_table_from_data(doc, weight_headers, weight_rows, col_widths=[4.0, 1.5, 10.0])

doc.add_page_break()

# ══════════════════════════════════════
# SECTION 4: DEPENDENCY TOPOLOGY (v1.1 NEW)
# ══════════════════════════════════════
add_formatted_paragraph(doc, "四、检查项依赖拓扑 (v1.1 新增)", bold=True, size=16, color=BLUE, space_after=12)

add_formatted_paragraph(doc, "当上游领域严重异常时，下游检查结果自动标记为 UNRELIABLE。", size=9, color=GRAY, space_after=8)

dep_text = """D2(数据管线) ──→ D3(回测保真度) ──→ D4(模型质量)
    │              │
    │              └──→ D7(执行层)
    │
    └──→ D9(跨领域集成)

D5(DSL设计) ────→ D3(回测保真度)

D10(安全) ──────→ D8(系统工程)   [公网暴露影响运维安全]"""

add_formatted_paragraph(doc, dep_text, size=9, color=DARK, space_after=8)

dep_headers = ["条件", "影响"]
dep_rows = [
    ("D2 评分 < 50", "D3, D4, D7, D9 检查结果标记为 UNRELIABLE"),
    ("D5 评分 < 40", "D3 检查结果标记为 UNRELIABLE"),
    ("D3 评分 < 50", "D4 检查结果标记为 UNRELIABLE"),
    ("D10 发现 ≥1 FAIL (L0/L1级)", "阻断所有公网暴露, L3发布冻结"),
]
add_table_from_data(doc, dep_headers, dep_rows, col_widths=[5.0, 10.5])

# ══════════════════════════════════════
# SECTION 5: FALSE POSITIVE MANAGEMENT (v1.1 NEW)
# ══════════════════════════════════════
add_formatted_paragraph(doc, "五、误报管理机制 (v1.1 新增)", bold=True, size=16, color=BLUE, space_after=12)

add_formatted_paragraph(doc, "持续运行的质量系统必然产生噪音。以下机制防止告警疲劳：", size=9, color=GRAY, space_after=8)

add_formatted_paragraph(doc, "豁免清单 (Known-Issue Suppression)", bold=True, size=11, color=DARK, space_after=4)
add_formatted_paragraph(doc, "在 config/quality_suppressions.yaml 维护已知问题豁免。可将特定检查项的FAIL降级为WARN, 设定过期时间。", size=9, space_after=8)

add_formatted_paragraph(doc, "连续失败升级 (Escalation)", bold=True, size=11, color=DARK, space_after=4)
esc_rows = [
    ("连续1次FAIL", "报告为 WARN (可能是瞬时故障)"),
    ("连续3次FAIL", "升级为 FAIL (确认是持续问题)"),
    ("连续7次FAIL", "升级为 CRITICAL (需人工介入)"),
]
add_table_from_data(doc, ["触发条件", "行为"], esc_rows, col_widths=[5.0, 10.5])

add_formatted_paragraph(doc, "Flaky检查隔离", bold=True, size=11, color=DARK, space_after=4)
add_formatted_paragraph(doc, "某检查项30天内PASS/FAIL交替≥5次→标记为flaky, L0/L1中降级为WARN。每季度审查flaky列表, 修复或替换不稳定检查。", size=9, space_after=10)

# ══════════════════════════════════════
# SECTION 6: FAIL SOP (v1.1 NEW)
# ══════════════════════════════════════
add_formatted_paragraph(doc, "六、FAIL处理SOP索引 (v1.1 新增)", bold=True, size=16, color=BLUE, space_after=12)

sop_headers = ["常见FAIL", "处理方式"]
sop_rows = [
    ("D1.L0.2 导入完整性验证", "检查最近修改的import语句, 确认依赖已安装"),
    ("D2.L0.1 Token环境变量缺失", "运行 scripts/setup_env.sh 或手动 export"),
    ("D2.L1.3 数据新鲜度过期", "手动触发 batch_predict.py 或等待下一个cron周期"),
    ("D3.L0.1 回测代码不可运行", "查看最近git diff, 回滚或修复回测相关修改"),
    ("D4.L2.2 低精度模型过多", "Dashboard一键重训, 或触发 batch_train.py --retrain_urgent"),
    ("D6.L1.1 熔断器触发", "检查circuit_breaker.json的pause_reason, 确认后可手动解除"),
    ("D8.L1.2 Dashboard不可用", "检查进程: cat .dashboard.pid, 重启: web_dashboard/start.sh"),
    ("D10.L0.1 敏感文件被追踪", "立即 git rm --cached 并添加到 .gitignore; 如已push则轮换密钥"),
    ("D10.L3.2 Git历史密钥泄漏", "立即轮换所有相关密钥/Token; 使用BFG清理历史"),
]
add_table_from_data(doc, sop_headers, sop_rows, col_widths=[5.0, 10.5])

# ══════════════════════════════════════
# SECTION 7: PROTOCOL GOVERNANCE (v1.1 NEW)
# ══════════════════════════════════════
add_formatted_paragraph(doc, "七、协议自身治理 (v1.1 新增)", bold=True, size=16, color=BLUE, space_after=12)

add_formatted_paragraph(doc, "版本管理: 主版本(v1→v2)=架构变更; 次版本(v1.0→v1.1)=检查项增删/阈值调整; 修订号=措辞修正", size=9, space_after=4)
add_formatted_paragraph(doc, "修改流程: 提出建议→实现新检查项→更新协议文档+版本号+变更日志→重新导出Word→飞书通知", size=9, space_after=4)
add_formatted_paragraph(doc, "审查节奏: 每季度审查检查项有效性; 每次重大系统变更后审查新增领域; 每年全面审视权重分配", size=9, space_after=10)

doc.add_page_break()

# ══════════════════════════════════════
# SECTION 8: ROADMAP
# ══════════════════════════════════════
add_formatted_paragraph(doc, "八、实现路线图", bold=True, size=16, color=BLUE, space_after=12)

phases = [
    ("Phase 1: 立即可用 (今天)", "548235", [
        ("✅", "scripts/smoke_test.py", "L0+L1 自动化"),
        ("⏳", "smoke_test.py --level 模式", "扩展为 L0|L1 分级运行"),
        ("⏳", "D3.L0 回测可运行性检查", "集成到smoke_test"),
        ("⏳", "D6.L0 风控模块检查", "导入+配置文件有效性"),
        ("⏳", "D10.L0 安全检查", "敏感文件+密钥权限 集成到pre-commit hook"),
    ]),
    ("Phase 2: 一周内", "2E75B6", [
        ("📋", "scripts/quality_audit.py", "L2 标准检测脚本 (含量化阈值)"),
        ("📋", "L1每日cron集成", "OpenClaw调度器新增任务"),
        ("📋", "飞书通知集成", "L1告警 → 飞书消息"),
        ("📋", "豁免清单机制", "config/quality_suppressions.yaml"),
        ("📋", "连续失败升级逻辑", "1次→WARN, 3次→FAIL, 7次→CRITICAL"),
    ]),
    ("Phase 3: 两周内", "BF8F00", [
        ("📋", "scripts/quant_full_audit.py", "L3 深度审查脚本"),
        ("📋", "Golden Test 数据集", "D9.L2.3 准备"),
        ("📋", "前视偏差扫描工具", "D2.L3.1"),
        ("📋", "回测保真度验证套件", "D3.*"),
        ("📋", "Dashboard字段映射矩阵脚本", "D8.L3.3"),
        ("📋", "API字段完整性自动检测", "D8.L1.5"),
        ("📋", "安全检查套件 (D10.*)", "密钥扫描/注入检测/公网暴露检测"),
    ]),
    ("Phase 4: 一月内", "C00000", [
        ("📋", "完整L0-L3 CI/CD流水线", "含依赖阻断规则"),
        ("📋", "历史趋势Dashboard", "审计结果时间序列"),
        ("📋", "自动修复建议生成", "L2-L3发现 → 自动任务"),
        ("📋", "Dashboard崩溃恢复测试套件", "D8.L3.4"),
        ("📋", "前后端一致性E2E测试", "D8.L2.3-L2.6 Headless browser"),
        ("📋", "环境可复现性测试", "D8.L3.6 干净clone→install→test"),
        ("📋", "Flaky检查自动识别和隔离", "30天PASS/FAIL交替≥5次"),
    ]),
]

for phase_name, color, items in phases:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    run = p.add_run(phase_name)
    run.bold = True; run.font.size = Pt(11); run.font.color.rgb = RGBColor(*hex_to_rgb(color))
    run.font.name = 'Calibri'

    for icon, item, desc in items:
        p = doc.add_paragraph()
        run = p.add_run(f"  {icon} {item}")
        run.bold = True; run.font.size = Pt(9.5); run.font.name = 'Calibri'
        if desc:
            run2 = p.add_run(f" — {desc}")
            run2.font.size = Pt(9); run2.font.name = 'Calibri'; run2.font.color.rgb = RGBColor(*GRAY)

# ── Save ──
doc.save(OUTPUT_PATH)
print(f"✅ Word文档已生成: {OUTPUT_PATH}")
print(f"   文件大小: {os.path.getsize(OUTPUT_PATH) / 1024:.1f} KB")
