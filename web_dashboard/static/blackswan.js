/* ══════════════════════════════════════════════════════════════
   DSL Dashboard — 风险/仓位页渲染模块 (v4.6.9c)
   从 index.html 拆分: renderBlackSwan 全套 + 雷达图
   新信息架构: 风险横幅 → 决策双栏(矩阵+仓位) → LPPL → 持仓 → 时间轴
   依赖: app.js (esc/fmt/fmtMoney/checkTableScroll)
   ══════════════════════════════════════════════════════════════ */

// ── 主入口: 渲染整个风险/仓位页 ──
async function renderBlackSwan(d) {
  const bs = d.blackswan || {};
  renderBSHero(bs);
  renderRiskMatrix(bs);
  renderPositionCard(bs);
  renderLPPLDeep(bs);
  renderRiskTimeline(bs);
  try {
    const ptResp = await fetch('/api/paper-trader');
    const ptData = await ptResp.json();
    renderPositionRisk(bs, ptData);
  } catch (e) {
    const el = document.getElementById('bs-position-risk');
    if (el) el.innerHTML = '<div class="bs-empty"><div class="icon">📋</div>暂无持仓数据</div>';
  }
}

// ── 1. 顶部风险横幅: 等级 + 分数 + 融合仓位 + 检查时间 ──
function renderBSHero(bs) {
  const container = document.getElementById('bs-hero');
  if (!container) return;
  const rm = bs.risk_matrix || {};
  const score = rm.overall_score || 0;
  const level = rm.risk_level || 'NORMAL';
  const pos = bs.recommended_position != null ? bs.recommended_position : (bs.position_ratio || 0);
  const sev = bs.severity || 0;
  const icons = {CRITICAL:'🔴', HIGH:'🟠', ELEVATED:'🟡', NORMAL:'🟢'};
  const colors = {CRITICAL:'var(--danger)', HIGH:'#ff6b35', ELEVATED:'#f59e0b', NORMAL:'var(--success)'};
  const icon = icons[level] || '⚪';
  const color = colors[level] || 'var(--text2)';
  const scoreCls = score >= 80 ? 'crit' : score >= 60 ? 'warn' : score >= 40 ? 'warn' : 'ok';
  const posCls = pos >= 0.5 ? 'ok' : pos >= 0.3 ? 'warn' : 'crit';
  const lastCheck = (bs.last_check || '').slice(0, 10);

  container.innerHTML =
    '<div class="bs-hero">' +
      '<div class="bs-hero-left">' +
        '<div class="bs-hero-icon">' + icon + '</div>' +
        '<div>' +
          '<div class="bs-hero-title" style="color:' + color + '">' + level + ' 风险状态</div>' +
          '<div class="bs-hero-sub">' + (bs.overall || bs.position_rationale || '') + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="bs-hero-right">' +
        '<div class="bs-hero-metric"><div class="v ' + scoreCls + '">' + score.toFixed(0) + '</div><div class="l">融合风险分</div></div>' +
        '<div class="bs-hero-metric"><div class="v ' + (sev >= 4 ? 'crit' : sev >= 2 ? 'warn' : 'ok') + '">' + sev + '/5</div><div class="l">黑天鹅严重度</div></div>' +
        '<div class="bs-hero-metric"><div class="v ' + posCls + '">' + Math.round(pos * 100) + '%</div><div class="l">建议仓位</div></div>' +
        '<div class="bs-hero-metric"><div class="v" style="font-size:13px;color:var(--text3)">' + lastCheck + '</div><div class="l">最后检查</div></div>' +
      '</div>' +
    '</div>';
}

// ── 2a. 左: 8因子风险矩阵 (条形列表, 替代雷达) ──
function renderRiskMatrix(bs) {
  const rm = bs.risk_matrix || {};
  const container = document.getElementById('bs-risk-matrix');
  if (!container) return;
  const factors = rm.factors || {};
  const calib = rm.calibration || {};

  const factorMeta = [
    { key: 'severity',    label: '黑天鹅严重度', weight: 0.10 },
    { key: 'lppl_bubble', label: 'LPPL泡沫',     weight: 0.15 },
    { key: 'volatility',  label: '波动率',       weight: 0.15 },
    { key: 'credit',      label: '信用利差',     weight: 0.15 },
    { key: 'liquidity',   label: '流动性',       weight: 0.15 },
    { key: 'correlation', label: '相关性',       weight: 0.10 },
    { key: 'flow',        label: '资金流向',     weight: 0.10 },
    { key: 'sentiment',   label: '宏观情绪',     weight: 0.10 },
  ];

  const rows = factorMeta.map(function (m) {
    const f = factors[m.key] || {};
    const s = f.score || 0;
    const cls = s >= 0.6 ? 'bs-factor-score-high' : s >= 0.35 ? 'bs-factor-score-mid' : 'bs-factor-score-low';
    const pct = Math.min(100, Math.round(s * 100));
    const detail = f.detail ? '<div class="bs-factor-detail">' + esc(f.detail) + '</div>' : '';
    return '<div class="bs-factor-row">' +
      '<div class="bs-factor-name" title="权重 ' + m.weight + '">' + m.label + '</div>' +
      '<div class="bs-factor-track"><div class="bs-factor-fill ' + cls + '" style="width:' + pct + '%"></div></div>' +
      '<div class="bs-factor-val">' + pct + '</div>' +
      detail +
    '</div>';
  }).join('');

  container.innerHTML =
    '<div class="bs-matrix-card">' +
      '<div class="bs-card-head"><h3>🧮 风险因子矩阵</h3>' +
        (calib.accuracy_rate != null ? '<span class="tag">校准 ' + Math.round(calib.accuracy_rate * 100) + '%</span>' : '') +
      '</div>' +
      '<div class="bs-factor-list">' + rows + '</div>' +
    '</div>';
}

// ── 2b. 右: 融合仓位决策卡 ──
function renderPositionCard(bs) {
  const rm = bs.risk_matrix || {};
  const container = document.getElementById('bs-pos-card');
  if (!container) return;
  const pbd = rm.position_ratio_breakdown || {};
  const recs = rm.recommendations || [];
  const pos = rm.position_ratio != null ? rm.position_ratio : 1.0;
  const posPct = Math.round(pos * 100);
  const posClr = pos >= 0.5 ? 'var(--success)' : pos >= 0.3 ? '#ff6b35' : 'var(--danger)';

  const bdItems = [
    { key: 'base',       name: '基础仓位', val: pbd.base,       clr: 'var(--text3)' },
    { key: 'black_swan', name: '黑天鹅',   val: pbd.black_swan, clr: '#ff6b35' },
    { key: 'lppl',       name: 'LPPL',     val: pbd.lppl,       clr: 'var(--danger)' },
    { key: 'fused',      name: '融合',     val: pbd.fused,      clr: 'var(--success)' },
  ];
  const bdRows = bdItems.filter(function (i) { return i.val != null; }).map(function (i) {
    const v = Math.round(i.val * 100);
    return '<div class="bs-bd-row">' +
      '<div class="bs-bd-name">' + i.name + '</div>' +
      '<div class="bs-bd-track"><div class="bs-bd-fill" style="width:' + Math.min(100, v) + '%;background:' + i.clr + '"></div></div>' +
      '<div class="bs-bd-val" style="color:' + i.clr + '">' + v + '%</div>' +
    '</div>';
  }).join('');

  const recHtml = recs.length
    ? '<div class="bs-recs">' + recs.map(function (r) { return '<div class="bs-rec">' + esc(r) + '</div>'; }).join('') + '</div>'
    : '';

  container.innerHTML =
    '<div class="bs-pos-card">' +
      '<div class="bs-card-head"><h3>🎯 融合仓位决策</h3></div>' +
      '<div class="bs-pos-main">' +
        '<div class="bs-pos-big" style="color:' + posClr + '">' + posPct + '<span class="pct">%</span></div>' +
        '<div class="bs-pos-label">总仓位上限<br><span style="font-size:9px;color:var(--text3)">与执行层同源</span></div>' +
      '</div>' +
      '<div class="bs-pos-bar-wrap"><div class="bs-pos-bar-fill" style="width:' + posPct + '%;background:' + posClr + '"></div></div>' +
      '<div class="bs-formula-note">融合 = min(黑天鹅, 60%×黑天鹅 + 40%×LPPL)</div>' +
      '<div class="bs-breakdown">' + bdRows + '</div>' +
      (bs.position_rationale ? '<div class="bs-rationale">📝 ' + esc(bs.position_rationale) + '</div>' : '') +
      recHtml +
    '</div>';
}

// ── 3. LPPL 深度诊断 ──
function renderLPPLDeep(bs) {
  const ld = bs.lppl_detailed || {};
  const targets = ld.targets || {};
  const signals = ld.sector_signals || [];
  const overall = ld.overall || {};
  const container = document.getElementById('bs-lppl-deep');
  if (!container) return;
  const keys = Object.keys(targets);
  if (keys.length === 0) { container.style.display = 'none'; return; }
  container.style.display = 'block';

  const targetHtml = keys.map(function (n) {
    const t = targets[n] || {};
    const reg = t.regime || 'normal';
    const cls = reg === 'bubble_mature' ? 'crit' : reg === 'bubble_growth' ? 'high' : 'normal';
    const ic = reg === 'bubble_mature' ? '🔴' : reg === 'bubble_growth' ? '🟠' : '🟢';
    const s = Math.round((t.strength || 0) * 100);
    const cp = Math.round((t.crash_probability || 0) * 100);
    return '<div class="bs-lppl-item ' + cls + '">' +
      '<h4>' + esc(n) + ' ' + ic + ' ' + reg + '</h4>' +
      '<div class="bs-lppl-metrics">' +
        '<span>强度 <b>' + s + '%</b></span>' +
        '<span>崩溃 <b>' + cp + '%</b></span>' +
        (t.days_to_critical ? '<span>临界 <b>' + t.days_to_critical + 'd</b></span>' : '') +
      '</div>' +
      (t.r_squared > 0 ? '<div class="bs-lppl-extra">R²=' + t.r_squared.toFixed(4) + '</div>' : '') +
      (t.recommendation ? '<div class="bs-lppl-rec">' + esc(t.recommendation) + '</div>' : '') +
    '</div>';
  }).join('');

  const signalHtml = signals.length
    ? '<div class="bs-sector-signals"><h4>📊 板块级信号</h4>' + signals.map(function (s) {
        const sd = (s.direction || '').toLowerCase();
        const dirCls = sd === 'down' ? 'down' : sd === 'up' ? 'up' : '';
        const dirLabel = (s.direction || '?').toUpperCase();
        const conf = Math.round((s.confidence || 0) * 100);
        const stocks = (s.stocks || []).slice(0, 4).join(', ');
        return '<div class="bs-sector-item">' +
          '<span class="bs-sector-name">' + esc(s.sector || '?') + '</span>' +
          '<span class="bs-sector-dir ' + dirCls + '">' + dirLabel + ' conf=' + conf + '%</span>' +
          (stocks ? '<span class="bs-sector-stocks">' + esc(stocks) + '</span>' : '') +
        '</div>';
      }).join('') + '</div>'
    : '';

  container.innerHTML =
    '<div class="bs-lppl-card">' +
      '<div class="bs-card-head"><h3>🔮 LPPL 泡沫深度诊断</h3>' +
        (overall.level ? '<span class="tag" style="color:' + (overall.level === 'CRITICAL' ? 'var(--danger)' : 'var(--accent)') + '">' + overall.level + (overall.score ? ' · score ' + overall.score.toFixed(0) : '') + '</span>' : '') +
      '</div>' +
      '<div class="bs-lppl-grid">' + targetHtml + '</div>' +
      signalHtml +
    '</div>';
}

// ── 4. 仓位风险分布 (持仓) ──
function renderPositionRisk(bs, ptData) {
  const container = document.getElementById('bs-position-risk');
  if (!container) return;
  const positions = (ptData.positions || []).slice(0, 20);
  if (!positions.length) {
    container.innerHTML = '<div class="bs-positions-card"><h3 style="margin:0 0 8px;font-size:14px">📦 仓位风险分布</h3><div class="bs-empty"><div class="icon">📋</div>暂无持仓数据</div></div>';
    return;
  }
  const nameMap = {};
  const poolStocks = (window.DATA && DATA.pool) ? (DATA.pool.stocks || []) : [];
  poolStocks.forEach(function (s) { nameMap[s.symbol || s.code || ''] = s.name || ''; });
  const highRiskKeywords = ['半导体', 'AI', '消费电子', 'NVDA'];

  const rows = positions.map(function (p) {
    const name = nameMap[p.symbol] || p.name || '';
    const pc = (p.pnl_pct || 0) >= 0 ? 'var(--success)' : 'var(--danger)';
    let riskTag = '⚪ 正常', sug = '正常持仓';
    const slv = p.stop_loss != null ? p.stop_loss : -0.08;
    const sd = ((p.pnl_pct || 0) / 100) - slv;
    if (sd <= 0) { riskTag = '🔴 止损触发'; sug = '立即执行止损'; }
    else if (sd < 0.02) { riskTag = '🟠 逼近止损'; sug = '密切监控'; }
    for (let i = 0; i < highRiskKeywords.length; i++) {
      if (name.indexOf(highRiskKeywords[i]) >= 0) {
        riskTag = '🔴 LPPL关联';
        const pr = (bs.risk_matrix || {}).position_ratio || 0.5;
        sug = pr < 0.3 ? '注意观察' : '建议减仓';
        break;
      }
    }
    return '<tr>' +
      '<td>' + esc(p.symbol) + '</td>' +
      '<td>' + esc(name) + '</td>' +
      '<td>' + fmtMoney(p.market_value || 0, 0) + '</td>' +
      '<td style="color:' + pc + '">' + ((p.pnl_pct || 0) >= 0 ? '+' : '') + (p.pnl_pct || 0) + '%</td>' +
      '<td style="font-size:11px">' + (p.stop_loss != null ? (p.stop_loss * 100).toFixed(0) + '%' : '-') + '</td>' +
      '<td style="font-size:11px">' + riskTag + '</td>' +
      '<td style="font-size:10px;color:var(--accent)">' + esc(sug) + '</td>' +
    '</tr>';
  }).join('');

  container.innerHTML =
    '<div class="bs-positions-card">' +
      '<div class="bs-card-head"><h3>📦 仓位风险分布</h3><span class="tag">Top ' + positions.length + '</span></div>' +
      '<div class="table-wrap"><table class="bs-positions-table">' +
        '<thead><tr><th>代码</th><th>名称</th><th>市值</th><th>盈亏%</th><th>止损线</th><th>板块风险</th><th>建议</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table></div>' +
    '</div>';
  setTimeout(checkTableScroll, 50);
}

// ── 5. 风险时间轴 ──
function renderRiskTimeline(bs) {
  const container = document.getElementById('bs-risk-timeline');
  if (!container) return;
  const hist = bs.risk_history || [];
  if (!hist.length) {
    container.innerHTML = '<div class="bs-timeline-card"><h3 style="margin:0 0 8px;font-size:14px">📈 风险历史趋势</h3><div class="bs-empty"><div class="icon">⏳</div>暂无历史数据</div></div>';
    return;
  }
  const W = 720, H = 200;
  const pad = { top: 14, right: 16, bottom: 34, left: 40 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const xs = function (i) { return pad.left + (hist.length > 1 ? (i / (hist.length - 1)) * plotW : plotW / 2); };
  const yrs = function (v) { return pad.top + (1 - Math.min(1, Math.max(0, v / 100))) * plotH; };
  const yps = function (v) { return pad.top + (1 - Math.min(1, Math.max(0, v))) * plotH; };

  let html = '<svg class="bs-timeline-svg" viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg">';
  // 网格线
  for (let g = 0; g <= 4; g++) {
    const y = pad.top + (g / 4) * plotH;
    html += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (W - pad.right) + '" y2="' + y + '" stroke="rgba(128,128,128,0.12)" stroke-width="1"/>';
  }
  // 面积填充 (风险分)
  const riskPts = hist.map(function (h, i) { return xs(i) + ',' + yrs(h.risk_score); }).join(' ');
  html += '<polygon points="' + pad.left + ',' + (pad.top + plotH) + ' ' + riskPts + ' ' + xs(hist.length - 1) + ',' + (pad.top + plotH) + '" fill="rgba(255,71,87,0.08)"/>';
  // 风险线
  html += '<polyline points="' + riskPts + '" fill="none" stroke="var(--danger)" stroke-width="2"/>';
  // 仓位线
  const posPts = hist.map(function (h, i) { return xs(i) + ',' + yps(h.position_ratio); }).join(' ');
  html += '<polyline points="' + posPts + '" fill="none" stroke="var(--success)" stroke-width="2" stroke-dasharray="4 3"/>';
  // 点 + 标签
  hist.forEach(function (h, i) {
    const x = xs(i);
    const ry = yrs(h.risk_score);
    const py = yps(h.position_ratio);
    html += '<circle cx="' + x + '" cy="' + ry + '" r="3" fill="var(--danger)" opacity="0.85"><title>' + h.date + ' 风险:' + h.risk_score.toFixed(0) + ' 仓位:' + Math.round(h.position_ratio * 100) + '%' + (h.event ? ' ' + esc(h.event) : '') + '</title></circle>';
    html += '<circle cx="' + x + '" cy="' + py + '" r="3" fill="var(--success)" opacity="0.85"><title>' + h.date + ' 仓位:' + Math.round(h.position_ratio * 100) + '%</title></circle>';
    const lbl = h.date.slice(-5);
    html += '<text x="' + x + '" y="' + (pad.top + plotH + 14) + '" text-anchor="end" fill="var(--text3)" font-size="9" transform="rotate(-30,' + x + ',' + (pad.top + plotH + 14) + ')">' + lbl + '</text>';
    if (h.event) {
      html += '<polygon points="' + (x - 4) + ',' + (ry - 10) + ' ' + x + ',' + (ry - 16) + ' ' + (x + 4) + ',' + (ry - 10) + '" fill="#f59e0b"><title>' + esc(h.event) + '</title></polygon>';
    }
  });
  // Y 轴标签
  html += '<text x="8" y="' + yrs(100) + '" fill="var(--text3)" font-size="9">100</text>';
  html += '<text x="8" y="' + yrs(50) + '" fill="var(--text3)" font-size="9">50</text>';
  html += '<text x="8" y="' + yrs(0) + '" fill="var(--text3)" font-size="9">0</text>';
  html += '</svg>';

  container.innerHTML =
    '<div class="bs-timeline-card">' +
      '<div class="bs-card-head"><h3>📈 风险历史趋势</h3></div>' +
      html +
      '<div class="bs-timeline-legend">' +
        '<span><span class="dot" style="background:var(--danger)"></span>风险分 (0-100)</span>' +
        '<span><span class="dot" style="background:var(--success)"></span>仓位比例</span>' +
        '<span><span class="dot" style="background:#f59e0b"></span>事件标记</span>' +
      '</div>' +
    '</div>';
}
