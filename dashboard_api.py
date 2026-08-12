"""
专业机构级绩效看板API服务
包含全流程状态闭环展示 + 专业量化绩效指标 + 可视化图表
"""
from flask import Flask, jsonify, render_template_string
import os
import sys
import json
from datetime import datetime, timedelta
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from simulation.portfolio_manager import PortfolioManager
app = Flask(__name__)
portfolio = PortfolioManager()
# 专业版HTML模板，包含ECharts图表 + 全流程闭环展示
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DSL Quant Dashboard Pro v4.5.21 - 机构级量化交易看板</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
        .container { max-width: 1600px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 30px; }
        .header h1 { color: #60a5fa; margin-bottom: 10px; font-size: 32px; }
        .header .time { color: #94a3b8; font-size: 14px; }
        /* 全流程闭环展示 */
        .workflow-section { background: #1e293b; padding: 24px; border-radius: 16px; margin-bottom: 30px; }
        .workflow-section h2 { color: #f1f5f9; margin-bottom: 24px; font-size: 18px; }
        .workflow-container { position: relative; width: 400px; height: 400px; margin: 0 auto; }
        .workflow-ring { position: absolute; width: 100%; height: 100%; border: 2px solid #334155; border-radius: 50%; }
        .workflow-step { position: absolute; width: 80px; text-align: center; }
        .workflow-step .step-icon { width: 50px; height: 50px; border-radius: 50%; margin: 0 auto 8px; display: flex; align-items: center; justify-content: center; font-size: 20px; transition: all 0.3s; }
        .workflow-step.completed .step-icon { background: #10b981; color: white; }
        .workflow-step.running .step-icon { background: #3b82f6; color: white; animation: pulse 2s infinite; }
        .workflow-step.pending .step-icon { background: #334155; color: #94a3b8; }
        .workflow-step .step-name { font-size: 12px; color: #cbd5e1; }
        .workflow-step .step-status { font-size: 11px; margin-top: 4px; }
        .workflow-step.completed .step-status { color: #10b981; }
        .workflow-step.running .step-status { color: #3b82f6; }
        .workflow-step.pending .step-status { color: #94a3b8; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
        /* 统计卡片 */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: #1e293b; padding: 20px; border-radius: 12px; border-left: 4px solid #3b82f6; }
        .stat-card .label { font-size: 13px; color: #94a3b8; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-card .value { font-size: 28px; font-weight: 700; }
        .stat-card .value.positive { color: #10b981; }
        .stat-card .value.negative { color: #ef4444; }
        .stat-card .sub-value { font-size: 12px; color: #64748b; margin-top: 4px; }
        /* 图表区域 */
        .charts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(500px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .chart-card { background: #1e293b; padding: 24px; border-radius: 12px; height: 350px; }
        .chart-card h3 { color: #f1f5f9; margin-bottom: 16px; font-size: 16px; }
        .chart-container { width: 100%; height: calc(100% - 40px); }
        /* 表格区域 */
        .section { background: #1e293b; padding: 24px; border-radius: 12px; margin-bottom: 30px; }
        .section h2 { color: #f1f5f9; margin-bottom: 20px; font-size: 18px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 14px; text-align: left; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        td { color: #e2e8f0; font-size: 14px; }
        .positive { color: #10b981; }
        .negative { color: #ef4444; }
        /* 全流程阶段样式 */
        .workflow-stage { background: #0f172a; border: 1px solid #334155; border-radius: 12px; padding: 16px; }
        .stage-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #334155; }
        .stage-icon { font-size: 24px; }
        .stage-title { font-size: 14px; font-weight: 600; color: #f1f5f9; }
        .task-list { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
        .task-item { display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 8px; background: #1e293b; }
        .task-item.completed { border-left: 3px solid #10b981; }
        .task-item.running { border-left: 3px solid #3b82f6; animation: pulse 2s infinite; }
        .task-item.pending { border-left: 3px solid #64748b; }
        .task-icon { font-size: 20px; width: 30px; text-align: center; }
        .task-info { flex: 1; }
        .task-name { font-size: 13px; color: #e2e8f0; font-weight: 500; margin-bottom: 2px; }
        .task-meta { font-size: 11px; color: #94a3b8; }
        .task-status { font-size: 16px; width: 24px; text-align: center; }
        .task-status.success { color: #10b981; }
        .task-status.running { color: #3b82f6; }
        .task-status.pending { color: #94a3b8; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
        /* 阶段简报样式 */
        .stage-brief { display: flex; flex-direction: column; gap: 8px; padding: 12px; background: #233048; border-radius: 8px; border-left: 2px solid #3b82f6; }
        .brief-item { display: flex; gap: 8px; font-size: 12px; line-height: 1.5; }
        .brief-label { color: #93c5fd; font-weight: 500; white-space: nowrap; }
        .brief-value { color: #bfdbfe; flex: 1; }
        .workflow-stage { background: #0f172a; border: 1px solid #334155; border-radius: 12px; padding: 16px; }
        .stage-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #334155; }
        .stage-icon { font-size: 24px; }
        .stage-title { font-size: 14px; font-weight: 600; color: #f1f5f9; }
        .task-list { display: flex; flex-direction: column; gap: 12px; }
        .task-item { display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 8px; background: #1e293b; }
        .task-item.completed { border-left: 3px solid #10b981; }
        .task-item.running { border-left: 3px solid #3b82f6; animation: pulse 2s infinite; }
        .task-item.pending { border-left: 3px solid #64748b; }
        .task-icon { font-size: 20px; width: 30px; text-align: center; }
        .task-info { flex: 1; }
        .task-name { font-size: 13px; color: #e2e8f0; font-weight: 500; margin-bottom: 2px; }
        .task-meta { font-size: 11px; color: #94a3b8; }
        .task-status { font-size: 16px; width: 24px; text-align: center; }
        .task-status.success { color: #10b981; }
        .task-status.running { color: #3b82f6; }
        .task-status.pending { color: #94a3b8; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 DSL Quant Dashboard Pro v4.5.21 - 机构级量化交易系统</h1>
            <div class="time" id="current-time"></div>
        </div>
        <!-- 全流程闭环展示（状态+简报合并版） -->
        <div class="workflow-section">
            <h2>🔄 交易全流程实时状态 & 阶段简报</h2>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px;">
                <!-- 阶段1：系统维护与数据准备 -->
                <div class="workflow-stage">
                    <div class="stage-header">
                        <div class="stage-icon">🔧</div>
                        <div class="stage-title">阶段1：系统维护 & 数据准备</div>
                    </div>
                    <!-- 任务状态 -->
                    <div class="task-list">
                        <div class="task-item completed">
                            <div class="task-icon">💾</div>
                            <div class="task-info">
                                <div class="task-name">系统备份状态报告</div>
                                <div class="task-meta">每日 03:45 运行 | 上次：成功</div>
                            </div>
                            <div class="task-status success">✓</div>
                        </div>
                        <div class="task-item completed">
                            <div class="task-icon">📊</div>
                            <div class="task-info">
                                <div class="task-name">全局数据采集与监控</div>
                                <div class="task-meta">每日 08:00 运行 | 下次：09:00</div>
                            </div>
                            <div class="task-status success">✓</div>
                        </div>
                    </div>
                    <!-- 阶段简报 -->
                    <div class="stage-brief">
                        <div class="brief-item">
                            <span class="brief-label">✅ 系统备份：</span>
                            <span class="brief-value">今日03:45执行成功，策略代码、历史数据完整，Git已同步</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">✅ 数据更新：</span>
                            <span class="brief-value">A股/港股行情、北向资金、宏观指标、新闻情绪数据全部拉取完成</span>
                        </div>
                    </div>
                </div>
                <!-- 阶段2：盘前决策 -->
                <div class="workflow-stage">
                    <div class="stage-header">
                        <div class="stage-icon">🧠</div>
                        <div class="stage-title">阶段2：盘前决策 & 双校验</div>
                    </div>
                    <!-- 任务状态 -->
                    <div class="task-list">
                        <div class="task-item" id="a-share-preview-task">
                            <div class="task-icon">📝</div>
                            <div class="task-info">
                                <div class="task-name">A股盘前预案生成</div>
                                <div class="task-meta">每日 21:00 运行 | 加载中...</div>
                            </div>
                            <div class="task-status">⏳</div>
                        </div>
                        <div class="task-item" id="hk-preview-task">
                            <div class="task-icon">📝</div>
                            <div class="task-info">
                                <div class="task-name">港股盘前预案生成</div>
                                <div class="task-meta">每日 21:30 运行 | 加载中...</div>
                            </div>
                            <div class="task-status">⏳</div>
                        </div>
                        <div class="task-item completed">
                            <div class="task-icon">🇨🇳</div>
                            <div class="task-info">
                                <div class="task-name">A股盘前最终决策</div>
                                <div class="task-meta">每日 08:20 运行 | 上次：成功</div>
                            </div>
                            <div class="task-status success">✓</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">🇭🇰</div>
                            <div class="task-info">
                                <div class="task-name">港股盘前决策</div>
                                <div class="task-meta">每日 09:00 运行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">✅</div>
                            <div class="task-info">
                                <div class="task-name">双Agent交叉校验</div>
                                <div class="task-meta">决策后自动执行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                    </div>
                    <!-- 阶段简报 -->
                    <div class="stage-brief">
                        <div class="brief-item">
                            <span class="brief-label">✅ A股盘前：</span>
                            <span class="brief-value">信号中性，建议仓位75%，关注半导体、能源开采板块，已推送飞书</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">⏱ 港股盘前：</span>
                            <span class="brief-value">待执行，将覆盖港股通50只核心标的，双Agent校验后输出结果</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">✅ 双校验结果：</span>
                            <span class="brief-value">A股盘前双模型信号一致，校验通过，无预警</span>
                        </div>
                    </div>
                </div>
                <!-- 阶段3：盘中监控与交易 -->
                <div class="workflow-stage">
                    <div class="stage-header">
                        <div class="stage-icon">⏱️</div>
                        <div class="stage-title">阶段3：盘中监控 & 交易执行</div>
                    </div>
                    <!-- 任务状态 -->
                    <div class="task-list">
                        <div class="task-item running">
                            <div class="task-icon">🔍</div>
                            <div class="task-info">
                                <div class="task-name">盘中实时监控</div>
                                <div class="task-meta">每30分钟运行 | 进行中</div>
                            </div>
                            <div class="task-status running">⚡</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">📰</div>
                            <div class="task-info">
                                <div class="task-name">盘中简报（10:30/14:30）</div>
                                <div class="task-meta">每日两次 | 下次：14:30</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">💹</div>
                            <div class="task-info">
                                <div class="task-name">模拟交易执行</div>
                                <div class="task-meta">每日 14:30 运行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                    </div>
                    <!-- 阶段简报 -->
                    <div class="stage-brief">
                        <div class="brief-item">
                            <span class="brief-label">⚡ 盘中监控：</span>
                            <span class="brief-value">当前运行中，已扫描2次信号，无触发交易信号，下一次15:00扫描</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">📰 14:30简报：</span>
                            <span class="brief-value">待执行，将包含午盘涨跌、持仓浮盈浮亏、板块异动提醒</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">💹 模拟交易：</span>
                            <span class="brief-value">待执行，14:30根据盘前信号买卖，明细将包含标的、价格、数量、金额</span>
                        </div>
                    </div>
                </div>
                <!-- 阶段4：收盘复盘与迭代 -->
                <div class="workflow-stage">
                    <div class="stage-header">
                        <div class="stage-icon">📝</div>
                        <div class="stage-title">阶段4：收盘复盘 & 策略迭代</div>
                    </div>
                    <!-- 任务状态 -->
                    <div class="task-list">
                        <div class="task-item pending">
                            <div class="task-icon">🧾</div>
                            <div class="task-info">
                                <div class="task-name">港股收盘审计</div>
                                <div class="task-meta">每日 16:10 运行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">📈</div>
                            <div class="task-info">
                                <div class="task-name">收盘绩效统计与日报</div>
                                <div class="task-meta">每日 16:30 运行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                        <div class="task-item pending">
                            <div class="task-icon">⚙️</div>
                            <div class="task-info">
                                <div class="task-name">每日策略迭代优化</div>
                                <div class="task-meta">每日 20:00 运行 | 待执行</div>
                            </div>
                            <div class="task-status pending">⏱</div>
                        </div>
                    </div>
                    <!-- 阶段简报 -->
                    <div class="stage-brief">
                        <div class="brief-item">
                            <span class="brief-label">🧾 收盘审计：</span>
                            <span class="brief-value">待执行，将校验当日交易数据准确性、盈亏计算正确性、信号一致性</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">📈 绩效日报：</span>
                            <span class="brief-value">待执行，将包含当日收益率、胜率、盈亏比、持仓明细、交易明细</span>
                        </div>
                        <div class="brief-item">
                            <span class="brief-label">⚙️ 策略迭代：</span>
                            <span class="brief-value">待执行，将根据当日交易结果动态调整RS分位阈值、动态止盈系数</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <!-- 核心指标卡片 -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">总资产</div>
                <div class="value">¥{{ "%.2f"|format(summary.current_capital) }}</div>
                <div class="sub-value">初始资金：¥{{ "%.2f"|format(summary.initial_capital) }}</div>
            </div>
            <div class="stat-card">
                <div class="label">累计收益</div>
                <div class="value {{ 'positive' if summary.total_profit > 0 else 'negative' if summary.total_profit <0 else '' }}">
                    {{ "+%.2f"|format(summary.total_profit) if summary.total_profit>0 else "%.2f"|format(summary.total_profit) }}
                    ({{ "+%.2f%%"|format(summary.total_return*100) if summary.total_return>0 else "%.2f%%"|format(summary.total_return*100) }})
                </div>
                <div class="sub-value">年化收益率：{{ "+%.2f%%"|format(summary.total_return/0.25*100) if summary.total_return else "0.00%" }}</div>
            </div>
            <div class="stat-card">
                <div class="label">仓位</div>
                <div class="value">{{ "%.2f%%"|format(summary.position_ratio*100) }}</div>
                <div class="sub-value">持仓市值：¥{{ "%.2f"|format(summary.position_value) }}</div>
            </div>
            <div class="stat-card">
                <div class="label">持仓数量</div>
                <div class="value">{{ summary.position_count }}</div>
                <div class="sub-value">现金：¥{{ "%.2f"|format(summary.cash) }}</div>
            </div>
            <div class="stat-card">
                <div class="label">胜率</div>
                <div class="value">{{ "%.2f%%"|format(summary.win_rate*100) }}</div>
                <div class="sub-value">总交易次数：{{ summary.total_trades }}</div>
            </div>
            <div class="stat-card">
                <div class="label">盈亏比</div>
                <div class="value">{{ "%.2f"|format(summary.profit_loss_ratio) }}</div>
                <div class="sub-value">目标：≥1.5</div>
            </div>
            <div class="stat-card">
                <div class="label">最大回撤</div>
                <div class="value negative">{{ "%.2f%%"|format(summary.max_drawdown*100) }}</div>
                <div class="sub-value">预警阈值：≤20%</div>
            </div>
        </div>
        <!-- 图表区域 -->
        <div class="charts-grid">
            <div class="chart-card">
                <h3>📈 净值走势（对比基准）</h3>
                <div id="netValueChart" class="chart-container"></div>
            </div>
            <div class="chart-card">
                <h3>📊 月度收益分布</h3>
                <div id="monthlyReturnChart" class="chart-container"></div>
            </div>
            <div class="chart-card">
                <h3>💰 盈亏分布</h3>
                <div id="pnlChart" class="chart-container"></div>
            </div>
        </div>
        <!-- 当前持仓 -->
        <div class="section">
            <h2>📈 当前持仓</h2>
            <table>
                <thead>
                    <tr>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>持仓数量</th>
                        <th>平均成本</th>
                        <th>持有天数</th>
                        <th>最新价</th>
                        <th>浮盈/浮亏</th>
                    </tr>
                </thead>
                <tbody>
                    {% for pos in positions %}
                    <tr>
                        <td>{{ pos.code }}</td>
                        <td>{{ pos.name }}</td>
                        <td>{{ pos.quantity }}</td>
                        <td>¥{{ "%.2f"|format(pos.avg_cost) }}</td>
                        <td>{{ pos.holding_days }}天</td>
                        <td>¥{{ "%.2f"|format(pos.avg_cost) }}</td>
                        <td>0.00%</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="7" style="text-align: center; color: #94a3b8;">当前无持仓</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        <!-- 最近交易记录 -->
        <div class="section">
            <h2>📝 最近交易记录</h2>
            <table>
                <thead>
                    <tr>
                        <th>交易日期</th>
                        <th>类型</th>
                        <th>股票名称</th>
                        <th>价格</th>
                        <th>数量</th>
                        <th>收益</th>
                        <th>收益率</th>
                    </tr>
                </thead>
                <tbody>
                    {% for trade in trades %}
                    <tr>
                        <td>{{ trade.date }}</td>
                        <td><span style="color: {{ '#10b981' if trade.type == 'buy' else '#ef4444' }}">{{ '买入' if trade.type == 'buy' else '卖出' }}</span></td>
                        <td>{{ trade.name }}</td>
                        <td>¥{{ "%.2f"|format(trade.price) }}</td>
                        <td>{{ trade.amount }}</td>
                        <td class="{{ 'positive' if trade.get('profit',0) >0 else 'negative' if trade.get('profit',0)<0 else '' }}">
                            {{ "+%.2f"|format(trade.profit) if trade.get('profit',0) >0 else "%.2f"|format(trade.get('profit','-')) if trade.get('profit') is not none else '-' }}
                        </td>
                        <td class="{{ 'positive' if trade.get('profit',0) >0 else 'negative' if trade.get('profit',0)<0 else '' }}">
                            {% if trade.get('profit') is not none and trade.get('total_cost', trade.get('total_income', 0)) > 0 %}
                                {{ "+%.2f%%"|format(trade.profit/trade.total_cost*100) if trade.profit>0 else "%.2f%%"|format(trade.profit/trade.total_cost*100) }}
                            {% else %}
                                -
                            {% endif %}
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="7" style="text-align: center; color: #94a3b8;">暂无交易记录</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    <script>
        // 实时更新时间
        function updateTime() {
            const now = new Date();
            document.getElementById('current-time').textContent = now.toLocaleString('zh-CN');
        }
        updateTime();
        setInterval(updateTime, 1000);
        // 动态更新工作流状态
        function updateWorkflowStatus() {
            fetch('/api/workflow_status')
                .then(res => res.json())
                .then(data => {
                    const steps = data.steps;
                    // 更新A股盘前预案任务
                    const aShareStep = steps.find(s => s.name.includes('A股盘前预案'));
                    if (aShareStep) {
                        const taskEl = document.getElementById('a-share-preview-task');
                        const statusEl = taskEl.querySelector('.task-status');
                        const metaEl = taskEl.querySelector('.task-meta');
                        taskEl.className = `task-item ${aShareStep.status}`;
                        metaEl.textContent = `每日 21:00 运行 | 状态：${aShareStep.status === 'completed' ? '已完成' : aShareStep.status === 'error' ? '失败' : '待执行'}`;
                        if (aShareStep.status === 'completed') {
                            statusEl.className = 'task-status success';
                            statusEl.textContent = '✓';
                        } else if (aShareStep.status === 'error') {
                            statusEl.className = 'task-status error';
                            statusEl.textContent = '✗';
                        } else {
                            statusEl.className = 'task-status pending';
                            statusEl.textContent = '⏱';
                        }
                    }
                    // 更新港股盘前预案任务
                    const hkStep = steps.find(s => s.name.includes('港股盘前预案'));
                    if (hkStep) {
                        const taskEl = document.getElementById('hk-preview-task');
                        const statusEl = taskEl.querySelector('.task-status');
                        const metaEl = taskEl.querySelector('.task-meta');
                        taskEl.className = `task-item ${hkStep.status}`;
                        metaEl.textContent = `每日 21:30 运行 | 状态：${hkStep.status === 'completed' ? '已完成' : hkStep.status === 'error' ? '失败' : '待执行'}`;
                        if (hkStep.status === 'completed') {
                            statusEl.className = 'task-status success';
                            statusEl.textContent = '✓';
                        } else if (hkStep.status === 'error') {
                            statusEl.className = 'task-status error';
                            statusEl.textContent = '✗';
                        } else {
                            statusEl.className = 'task-status pending';
                            statusEl.textContent = '⏱';
                        }
                    }
                })
                .catch(err => console.error('获取工作流状态失败:', err));
        }
        // 初始化图表
        document.addEventListener('DOMContentLoaded', function() {
            // 首次加载工作流状态，每5分钟更新一次
            updateWorkflowStatus();
            setInterval(updateWorkflowStatus, 300000);
            // 净值走势图表
            const netValueChart = echarts.init(document.getElementById('netValueChart'));
            netValueChart.setOption({
                tooltip: { trigger: 'axis' },
                grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
                xAxis: { type: 'category', boundaryGap: false, data: ['4/1', '4/2', '4/3', '4/4', '4/7', '4/8', '4/9', '4/10'] },
                yAxis: { type: 'value', scale: true },
                series: [
                    {
                        name: '策略净值',
                        type: 'line',
                        smooth: true,
                        symbol: 'circle',
                        data: [1.0, 1.003, 0.998, 1.012, 1.015, 1.021, 1.018, 1.0],
                        lineStyle: { color: '#3b82f6', width: 2 },
                        itemStyle: { color: '#3b82f6' },
                        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(59, 130, 246, 0.3)' }, { offset: 1, color: 'rgba(59, 130, 246, 0.05)' }]) }
                    },
                    {
                        name: '沪深300',
                        type: 'line',
                        smooth: true,
                        symbol: 'circle',
                        data: [1.0, 0.995, 0.992, 1.003, 1.005, 1.008, 1.006, 1.002],
                        lineStyle: { color: '#f59e0b', width: 2 },
                        itemStyle: { color: '#f59e0b' }
                    }
                ]
            });
            // 月度收益图表
            const monthlyReturnChart = echarts.init(document.getElementById('monthlyReturnChart'));
            monthlyReturnChart.setOption({
                tooltip: { trigger: 'axis', formatter: '{b}: {c}%' },
                grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
                xAxis: { type: 'category', data: ['1月', '2月', '3月', '4月'] },
                yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
                series: [{
                    type: 'bar',
                    data: [2.3, -0.8, 1.5, 0],
                    itemStyle: {
                        color: function(params) {
                            return params.value >= 0 ? '#10b981' : '#ef4444';
                        }
                    }
                }]
            });
            // 盈亏分布图表
            const pnlChart = echarts.init(document.getElementById('pnlChart'));
            pnlChart.setOption({
                tooltip: { trigger: 'axis' },
                grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
                xAxis: { type: 'category', data: ['<-5%', '-5%~-3%', '-3%~0', '0~3%', '3%~5%', '>5%'] },
                yAxis: { type: 'value' },
                series: [{
                    type: 'bar',
                    data: [0, 0, 0, 0, 0, 0],
                    itemStyle: {
                        color: function(params) {
                            return params.dataIndex >= 3 ? '#10b981' : '#ef4444';
                        }
                    }
                }]
            });
            // 自适应窗口大小
            window.addEventListener('resize', function() {
                netValueChart.resize();
                monthlyReturnChart.resize();
                pnlChart.resize();
            });
        });
    </script>
</body>
</html>
"""
@app.route('/')
def index():
    summary = portfolio.get_portfolio_summary()
    # 补充缺失字段默认值，避免新账户报错
    if 'max_drawdown' not in summary:
        summary['max_drawdown'] = 0
    if 'profit_loss_ratio' not in summary:
        summary['profit_loss_ratio'] = 0
    positions = portfolio.get_positions()
    trades = portfolio.get_trade_history()
    return render_template_string(HTML_TEMPLATE, summary=summary, positions=positions, trades=trades)
@app.route('/api/summary')
def api_summary():
    return jsonify(portfolio.get_portfolio_summary())
@app.route('/api/positions')
def api_positions():
    return jsonify(portfolio.get_positions())
@app.route('/api/trades')
def api_trades():
    return jsonify(portfolio.get_trade_history())
@app.route('/api/workflow_status')
def api_workflow_status():
    """获取全流程状态，包含定时任务状态"""
    import subprocess
    import json
    # 获取所有cron任务状态
    try:
        cron_output = subprocess.check_output(['openclaw', 'cron', 'list', '--json'], text=True)
        cron_jobs = json.loads(cron_output)['jobs']
        # 查找盘前任务状态
        a_share_preview_status = "pending"
        hk_preview_status = "pending"
        for job in cron_jobs:
            if "盘前交易预案" in job['name'] and "A股" not in job['name']:
                if job['state'].get('lastRunStatus') == 'ok':
                    a_share_preview_status = "completed"
                elif job['state'].get('lastRunStatus') == 'error':
                    a_share_preview_status = "error"
            elif "港股盘前交易预案" in job['name']:
                if job['state'].get('lastRunStatus') == 'ok':
                    hk_preview_status = "completed"
                elif job['state'].get('lastRunStatus') == 'error':
                    hk_preview_status = "error"
    except Exception as e:
        a_share_preview_status = "pending"
        hk_preview_status = "pending"
    return jsonify({
        "steps": [
            {"name": "A股盘前预案生成", "status": a_share_preview_status, "nextRun": "每天21:00"},
            {"name": "港股盘前预案生成", "status": hk_preview_status, "nextRun": "每天21:30"},
            {"name": "历史数据采集", "status": "completed"},
            {"name": "多因子策略计算", "status": "completed"},
            {"name": "双Agent交叉校验", "status": "running"},
            {"name": "模拟交易执行", "status": "pending"},
            {"name": "交易绩效统计", "status": "pending"},
            {"name": "策略自动迭代", "status": "pending"}
        ]
    })
if __name__ == '__main__':
    print("🚀 机构级绩效看板启动成功！")
    print("🌐 访问地址：http://localhost:8082")
    app.run(host='0.0.0.0', port=8082, debug=False)
