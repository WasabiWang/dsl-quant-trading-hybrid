/* ══════════════════════════════════════════════════════════════
   DSL Dashboard — 公共逻辑模块 (v4.6.9c)
   从 index.html 拆分: state/API/路由/格式化/总览/预测/模型/进度/paper-trader
   黑天鹅页渲染见 blackswan.js
   ══════════════════════════════════════════════════════════════ */
// ── v4.6.x: XSS防护 + 请求优化 ──
function esc(s) { if (s == null) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ── State ──
console.log('[DSL Dashboard] v4.6.7 loaded', new Date().toISOString());
let DATA = {};
let _refreshing = false, _failCount = 0, _versionCached = '', _lastDataTime = 0;

// ── 千分位格式化 ──
function fmt(n, dec=2) {
  if (n == null || isNaN(n)) return '-';
  const num = Number(n);
  const sign = num < 0 ? '-' : '';
  const abs = Math.abs(num);
  const intPart = Math.floor(abs).toLocaleString('en-US');
  if (dec <= 0) return sign + intPart;
  const decPart = abs.toFixed(dec).split('.')[1];
  return sign + intPart + '.' + decPart;
}
function fmtMoney(n, dec=2) {
  if (n == null || isNaN(n)) return '¥-';
  const prefix = n >= 0 ? '¥' : '-¥';
  return prefix + fmt(Math.abs(n), dec);
}
function fmtPnl(n, dec=2) {
  if (n == null || isNaN(n)) return '¥-';
  const prefix = n >= 0 ? '+¥' : '-¥';
  return prefix + fmt(Math.abs(n), dec);
}

// ── v4.6.2: Mini sparkline SVG (#5) ──
function sparkline(values, width=80, height=24, color='var(--accent)') {
  if (!values || values.length < 2) return `${values||''}`;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const pad = 2;
  const w = width - pad*2, h = height - pad*2;
  const pts = values.map((v,i) => {
    const x = pad + (i/(values.length-1))*w;
    const y = pad + (1-(v-min)/range)*h;
    return `${x},${y}`;
  });
  const path = 'M' + pts.join(' L');
  const areaPath = path +
    ' L' + pts[pts.length-1].split(',')[0] + ',' + (height-pad) +
    ' L' + pts[0].split(',')[0] + ',' + (height-pad) + ' Z';
  const isUp = values[values.length-1] >= values[0];
  const colorVal = isUp ? 'var(--success)' : 'var(--danger)';
  return `<svg class="sparkline-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<defs><linearGradient id="spGrad" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0%" stop-color="${isUp ? 'rgba(0,212,170,0.4)' : 'rgba(255,71,87,0.4)'}"/>` +
    `<stop offset="100%" stop-color="${isUp ? 'rgba(0,212,170,0)' : 'rgba(255,71,87,0)'}"/></linearGradient></defs>` +
    `<path class="sp-line" d="${path}" stroke="${colorVal}"/>` +
    `<path class="sp-area" d="${areaPath}"/>` +
    `<circle class="sp-dot" cx="${pts[pts.length-1].split(',')[0]}" cy="${pts[pts.length-1].split(',')[1]}" r="2.5" fill="${colorVal}"/>` +
    `</svg>`;
}

// ── v4.6.2: 数值更新动画触发 (#7) ──
function animateValue(el, newVal) {
  if (!el) return;
  const old = el.textContent;
  if (old !== String(newVal)) {
    el.textContent = newVal;
    el.classList.remove('value-update');
    void el.offsetWidth; // force reflow
    el.classList.add('value-update');
  } else {
    el.textContent = newVal;
  }
}

let POLL_INTERVAL = 30000;
const TAB_NAMES = {overview:'总览',predictions:'预测/筛选',models:'模型',progress:'进度',blackswan:'黑天鹅','paper-trader':'模拟交易'};

// ── v4.6.x: 页面不可见时暂停轮询 ──
document.addEventListener('visibilitychange', function() {
  if (document.hidden) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  else if (!autoRefreshTimer) { autoRefreshTimer = setInterval(refreshAll, POLL_INTERVAL); }
});

// ── Table scroll indicator ──
function checkTableScroll() {
  document.querySelectorAll('.table-wrap').forEach(function(el) {
    if (el.scrollWidth > el.clientWidth + 4) {
      el.classList.add('scrollable');
    } else {
      el.classList.remove('scrollable');
    }
  });
}

// ── Init ──
document.querySelectorAll('.nav-item[data-tab]').forEach(el => {
  el.addEventListener('click', () => switchTab(el.dataset.tab));
});

// v4.6.9d: 模型页 tab 过滤绑定 (修复既有bug: tab有UI但点击无反应)
document.querySelectorAll('#model-tabs .tab').forEach(el => {
  el.addEventListener('click', () => {
    document.querySelectorAll('#model-tabs .tab').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    renderModels(DATA, el.dataset.sub || 'all');
  });
});

refreshAll();
let autoRefreshTimer = setInterval(refreshAll, POLL_INTERVAL);

// ── WebSocket 实时进度推送 ──
(function connectProgressWS() {
  const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/progress';
  let ws = new WebSocket(wsUrl);
  let reconnectDelay = 2000;

  ws.onopen = function() {
    console.log('[WS] 进度流已连接');
    reconnectDelay = 2000;
  };

  ws.onmessage = function(ev) {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'cron_status') { refreshAll(); }
      else if (msg.type && msg.type.indexOf('progress') >= 0) { refreshAll(); }
    } catch(e) {}
  };

  ws.onclose = function() {
    console.log('[WS] 断开，' + (reconnectDelay/1000) + 's后重连');
    setTimeout(connectProgressWS, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };

  ws.onerror = function() { ws.close(); };
})();

// ── API ──
async function refreshAll() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    const fullResp = await fetch('/api/full');
    if (fullResp.status === 401) { window.location.href = '/login'; return; }
    DATA = await fullResp.json();
    // v4.6.x: version从full API缓存
    if (!_versionCached) {
      const v = DATA.status ? (DATA.status.version || '') : '';
      _versionCached = v;
      const badge = document.getElementById('version-badge');
      if (badge && v) badge.textContent = 'v' + v;
    }
    document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
    _lastDataTime = Date.now();
    _failCount = 0; POLL_INTERVAL = 30000;
    try {
      renderAll(); checkTableScroll();
      // v4.6.9h.2: /api/full 已含 paperTrader 字段, 不再需要异步补丁
    } catch(renderErr) {
      console.error('[DSL Dashboard] render failed:', renderErr);
      document.getElementById('header-sub').textContent = '❌ 渲染失败: ' + String(renderErr.message || renderErr).slice(0, 80);
      throw renderErr;
    }
  } catch(e) {
    _failCount++;
    POLL_INTERVAL = Math.min(30000, 1000 * Math.pow(2, Math.min(_failCount, 5)));
    const msg = String(e && e.message ? e.message : e).slice(0, 80);
    console.error('[DSL Dashboard] refreshAll failed:', e);
    document.getElementById('header-sub').textContent = '❌ 连接/渲染失败: ' + msg + ' (重试' + (POLL_INTERVAL/1000) + 's)';
    const failEl = document.getElementById('error-retry-area');
    if (failEl) {
      failEl.innerHTML = '<button onclick="refreshAll()" class="empty-retry" style="display:inline-block">🔄 立即重试</button>';
    }
  } finally {
    _refreshing = false;
  }
}

// v4.6.9h.2: loadPaperTraderBg 已移除 — /api/full 直接含 paperTrader 字段
function getPortfolioCodes() {
  const pt = DATA.paperTrader || {};
  const positions = pt.positions || [];
  if (!positions || !positions.length) return new Set();
  return new Set(positions.map(function(p) { return p.symbol || ''; }).filter(Boolean));
}

// ── Routing with smart refresh ──
function switchTab(tabId) {
  document.querySelectorAll('.nav-item[data-tab]').forEach(e => e.classList.remove('active'));
  const navEl = document.querySelector('.nav-item[data-tab="' + tabId + '"]');
  if (navEl) navEl.classList.add('active');

  document.querySelectorAll('.section').forEach(e => e.classList.remove('active'));
  const section = document.getElementById('section-' + tabId);
  if (section) section.classList.add('active');

  // v4.6.x: 数据超过15秒自动刷新（避免显示过期数据）
  if (_lastDataTime && (Date.now() - _lastDataTime) > 15000 && !_refreshing) {
    refreshAll().then(function() { _renderTab(tabId); });
    return;
  }
  _renderTab(tabId);
}
function _renderTab(tabId) {
  switch(tabId) {
    case 'predictions': renderPredictions(DATA); break;
    case 'models': renderModels(DATA); break;
    case 'progress': renderPipeline(DATA); break;
    case 'blackswan': renderBlackSwan(DATA); break;
    case 'paper-trader': renderPaperTrader(DATA); break;
    default: renderOverview(DATA); break;
  }
  setTimeout(checkTableScroll, 50);
}

// ── Helpers ──
function h5dDisplay(acc) {
  if (acc === 0 || acc === undefined || acc === null) return '-';
  if (Math.abs(acc - 0.5) < 0.001) {
    return '<span style="color:var(--text2);font-size:11px">待训练</span>';
  }
  const clr = acc >= 0.6 ? 'var(--success)' : acc >= 0.45 ? 'var(--accent)' : 'var(--danger)';
  return '<span style="color:' + clr + '">' + (acc*100).toFixed(1) + '%</span>';
}

function h20dRetCell(ret) {
  if (ret === 0 || ret === undefined || ret === null) return '<span style="color:var(--text2)">—</span>';
  const clr = ret >= 0 ? 'var(--signal-buy)' : 'var(--signal-sell)';
  return '<span style="color:' + clr + '">' + (ret*100).toFixed(2) + '%</span>';
}

function h20dDisplay(acc, h5dAcc, h20dRet) {
  if (acc === 0 || acc === undefined || acc === null) return '-';
  if (Math.abs(acc - 0.5) < 0.001) {
    if (h20dRet && Math.abs(h20dRet) > 0) {
      return '<span style="color:var(--accent);font-size:11px">有数据</span>';
    }
    if (h5dAcc && h5dAcc > 0) {
      return '<span style="color:var(--text2);font-size:11px">待评估</span>';
    }
    return '<span style="color:var(--text2)">—</span>';
  }
  const clr = acc >= 0.5 ? 'var(--success)' : 'var(--danger)';
  return '<span style="color:' + clr + '">' + (acc*100).toFixed(1) + '%</span>';
}

// ── Canvas Chart ──
function drawLineChart(canvasId, labels, values, color, opts) {
  opts = opts || {};
  const c = document.getElementById(canvasId);
  if (!c) return;
  const ctx = c.getContext('2d'), dpr = window.devicePixelRatio||1;
  const W = c.clientWidth||400, H = c.clientHeight||160;
  c.width = W*dpr; c.height = H*dpr; ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);
  if (!labels||!labels.length) {
    ctx.fillStyle='#7a7870'; ctx.font='12px sans-serif'; ctx.textAlign='center';
    ctx.fillText('暂无数据',W/2,H/2); return;
  }
  const pad={t:16,b:20,l:40,r:16}, cw=W-pad.l-pad.r, ch=H-pad.t-pad.b;
  const max=(opts.max||Math.max.apply(null,values.map(function(v){return Math.abs(v)}))||0.01)*1.15;
  const min=opts.min||Math.min.apply(null,values.map(function(v){return Math.abs(v)}))||0;
  const range=max-min||0.01;
  ctx.strokeStyle='#1a1d27'; ctx.lineWidth=1;
  for(let i=0;i<=4;i++){ const y=pad.t+ch*i/4; ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke(); }
  ctx.fillStyle='#7a7870'; ctx.font='10px monospace'; ctx.textAlign='right';
  for(let i=0;i<=4;i++){ ctx.fillText((max-(max-min)*i/4).toFixed(1),pad.l-4,pad.t+ch*i/4+3); }
  ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=2;
  for(let i=0;i<labels.length;i++){
    const x=pad.l+cw*i/(labels.length-1||1), y=pad.t+ch-(values[i]-min)/range*ch;
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  }
  ctx.stroke();
  for(let i=0;i<labels.length;i++){
    const x=pad.l+cw*i/(labels.length-1||1), y=pad.t+ch-(values[i]-min)/range*ch;
    ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fillStyle=color; ctx.fill();
  }
  ctx.fillStyle='#7a7870'; ctx.font='9px sans-serif'; ctx.textAlign='center';
  const step=Math.max(1,Math.floor(labels.length/10));
  for(let i=0;i<labels.length;i+=step){
    ctx.fillText(labels[i].slice(-5),pad.l+cw*i/(labels.length-1||1),pad.t+ch+14);
  }
}

// ── Render All ──
function renderAll() {
  const s = DATA.status || {};
  const ts = DATA.timestamp || '';
  const pool = DATA.pool || {};
  const poolTotal = pool.total || (pool.stocks ? pool.stocks.length : 0);
  // v4.6.2 fix: 保留 last-refresh span，避免 textContent 覆盖后 getElementById('last-refresh') 变 null
  document.getElementById('header-sub').innerHTML = '最后更新: ' + (ts ? new Date(ts).toLocaleString() : '-') + ' | 股票池: ' + poolTotal + '只 <span id="last-refresh" style="margin-left:8px;font-size:10px;color:var(--text3)"></span>';

  const staleness = (DATA.status||{}).data_staleness;
  const refreshTime = new Date().toLocaleTimeString();
  let staleSuffix = '';
  if (staleness) {
    const staleItems = [];
    for (const key in staleness) {
      if (!staleness[key].exists) staleItems.push(key + ':❌');
      else if (staleness[key].stale) staleItems.push(key + ':' + staleness[key].age_hours.toFixed(0) + 'h⚠️');
    }
    if (staleItems.length) staleSuffix = ' ⚠️ ' + staleItems.slice(0,3).join(' ') + (staleItems.length>3?'...':'');
  }
  document.getElementById('last-refresh').title = staleSuffix ? refreshTime + staleSuffix : refreshTime;
  document.getElementById('last-refresh').textContent = staleSuffix ? refreshTime + staleSuffix : refreshTime;

  const hdot = document.getElementById('health-dot');
  const hlabel = document.getElementById('health-label');
  if (s.health === 'paused') { hdot.className='status-dot red'; hlabel.textContent='已暂停'; }
  else if (s.health === 'degraded') { hdot.className='status-dot yellow'; hlabel.textContent='降级'; }
  else { hdot.className='status-dot green'; hlabel.textContent='运行中'; }

  const bs = DATA.blackswan || {};
  const badge = document.getElementById('bs-badge');
  if (bs.active && bs.severity >= 4) { badge.style.display='block'; }
  else { badge.style.display='none'; }

  const banner = document.getElementById('bs-banner');
  if (bs.active && bs.severity >= 3) {
    const level = bs.severity >= 5 ? 'critical' : 'high';
    banner.style.display='block'; banner.className='banner ' + level;
    banner.innerHTML = '<h4>🦢 黑天鹅预警 (Severity: ' + bs.severity + '/5)</h4><p>仓位限制: ' + (bs.recommended_position != null ? (bs.recommended_position * 100).toFixed(0) : (bs.position_ratio * 100).toFixed(0)) + '% (融合) | 最后检查: ' + (bs.last_check || '-') + '</p>';
  } else { banner.style.display='none'; }

  renderOverview(DATA);
  renderPipeline(DATA).catch(function(e){});
}

// ── Overview ──
// v4.6.9: 股票池分层摘要 — 按实际tier显示非零分层 (alpha/core/bench等)
function tierSummary(tiers) {
  const labels = {alpha:'α池', core:'核心', bench:'基准', bluechip:'蓝筹', growth:'成长', cyclical:'周期', flex:'灵活'};
  const parts = [];
  Object.keys(tiers||{}).forEach(function(k) {
    if ((tiers[k]||0) > 0) parts.push((labels[k]||k) + (tiers[k]));
  });
  return parts.length ? parts.join(' ') : '—';
}

function renderOverview(d) {
  const s = d.status || {};
  const pool = d.pool || {};
  const tiers = pool.tiers || {};
  const cal = d.calibration || {};
  const sum = cal.summary || {};
  const stks = cal.stocks || [];
  const bs = d.blackswan || {};
  const port = d.portfolio || {};

  const sevClr = bs.active && bs.severity >= 4 ? 'red' : bs.active ? 'yellow' : 'green';
  // v4.6.9: 顶部卡同步使用模拟盘绩效(不再展示回测夏普/回撤, 避免口径混清)
  const paper = s.paper || {};
  const hasPaper = paper.total_value != null && paper.total_value > 0;
  const paperRet = hasPaper ? Number(paper.total_pnl_pct || 0) : null;
  document.getElementById('ov-cards').innerHTML =
    '<div class="card card-sm"><h3>📊 信号</h3><div class="value blue">' + ((s.signal_count_buy||0)+(s.signal_count_sell||0)+(s.signal_count_hold||0)) + '</div><div class="sub-text">🟢' + (s.signal_count_buy||0) + ' 🔴' + (s.signal_count_sell||0) + ' ⚪' + (s.signal_count_hold||0) + '</div></div>' +
    // v4.6.9: tier显示实际分层 (alpha/core/bench), 不再硬编码蓝筹/成长
    '<div class="card card-sm"><h3>🏛️ 股票池</h3><div class="value blue">' + (pool.total||0) + '</div><div class="sub-text">' + tierSummary(tiers) + '</div></div>' +
    '<div class="card card-sm"><h3>📈 持仓</h3><div class="value ' + (port.total_positions > 0 ? 'yellow' : 'blue') + '">' + (port.total_positions||0) + '</div><div class="sub-text">现金 ' + fmtMoney(port.cash||0, 2) + '</div></div>' +
    '<div class="card card-sm"><h3>💹 模拟收益</h3><div class="value ' + (paperRet != null ? (paperRet >= 1 ? 'green' : paperRet >= 0 ? 'yellow' : 'red') : '') + '">' + (paperRet != null ? (paperRet>=0?'+':'')+paperRet.toFixed(2)+'%' : '—') + '</div><div class="sub-text">' + (hasPaper ? '模拟盘累计' : '暂无数据') + '</div></div>' +
    '<div class="card card-sm"><h3>💰 总盈亏</h3><div class="value ' + ((paper.total_pnl||0) >= 0 ? 'green' : 'red') + '">' + (hasPaper ? fmtPnl(paper.total_pnl||0, 0) : '—') + '</div><div class="sub-text">模拟盘</div></div>' +
    '<div class="card card-sm"><h3>' + (bs.active ? '⚠️' : '✅') + ' 黑天鹅</h3><div class="value ' + sevClr + '">' + (bs.active ? bs.severity+'/5' : '正常') + '</div><div class="sub-text">' + (bs.active ? '仓位限制 ' + ((bs.recommended_position != null ? bs.recommended_position : bs.position_ratio) * 100 || 0).toFixed(0) + '%' : '') + '</div></div>';

  const threats = bs.top_threats || [];
  const prices = bs.market_prices || {};
  const oilChg = (prices.oil||{}).change_pct || 0;
  const goldChg = (prices.gold||{}).change_pct || 0;
  const sevStatus = bs.active ? (bs.severity >= 4 ? '🔴 高风险' : bs.severity >= 2 ? '🟡 警惕' : '🟢 正常') : '🟢 正常';
  document.getElementById('ov-risk').innerHTML =
    '<div class="metric-row"><span class="metric-label">黑天鹅等级</span><span class="metric-val" style="color:' + (bs.severity >= 4 ? 'var(--danger)' : bs.severity >= 2 ? 'var(--accent)' : 'var(--success)') + '">' + sevStatus + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">仓位限制</span><span class="metric-val" style="color:var(--accent)">' + (bs.active ? (((bs.recommended_position != null ? bs.recommended_position : bs.position_ratio)||0)*100).toFixed(0)+'%' : '100%') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">活跃威胁</span><span class="metric-val" style="color:' + (threats.length > 2 ? 'var(--danger)' : 'var(--text)') + '">' + threats.length + ' 项</span></div>' +
    '<div class="metric-row"><span class="metric-label">WTI原油</span><span class="metric-val" style="color:' + (oilChg >= 0 ? 'var(--danger)' : 'var(--success)') + '">$' + ((prices.oil||{}).price||'--') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">COMEX黄金</span><span class="metric-val" style="color:' + (goldChg >= 0 ? 'var(--danger)' : 'var(--success)') + '">$' + ((prices.gold||{}).price||'--') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">VIX</span><span class="metric-val">' + ((prices.vix||{}).price||'--') + '</span></div>' +
    (bs.last_check ? '<div class="metric-row"><span class="metric-label" style="font-size:10px">检查时间</span><span class="metric-val" style="font-size:10px;color:var(--text2)">' + bs.last_check.slice(0,10) + '</span></div>' : '');

  const avgAcc = stks.length > 0 ? stks.reduce(function(a,s){return a + (s.accuracy||0)}, 0) / stks.length : 0;
  const avgH20 = stks.length > 0 ? stks.reduce(function(a,s){return a + (s.h20d_accuracy||0)}, 0) / stks.length : 0;
  const trainedCount = stks.filter(function(s) { return s.accuracy > 0 && Math.abs(s.accuracy - 0.5) > 0.001; }).length;
  document.getElementById('ov-model-health').innerHTML =
    '<div class="metric-row"><span class="metric-label">模型总数</span><span class="metric-val" style="color:var(--accent)">' + (sum.total||0) + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">已训练</span><span class="metric-val" style="color:var(--success)">' + trainedCount + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">急迫重训</span><span class="metric-val" style="color:' + (sum.retrain_urgent > 0 ? 'var(--danger)' : 'var(--success)') + '">' + (sum.retrain_urgent||0) + ' 只</span></div>' +
    '<div class="metric-row"><span class="metric-label">计划重训</span><span class="metric-val" style="color:var(--accent)">' + (sum.retrain_planned||0) + ' 只</span></div>' +
    '<div class="metric-row"><span class="metric-label">正常</span><span class="metric-val" style="color:var(--success)">' + (sum.normal||0) + ' 只</span></div>' +
    '<div class="metric-row"><span class="metric-label">均值H5D精度</span><span class="metric-val" style="color:' + (avgAcc >= 0.5 ? 'var(--success)' : 'var(--accent)') + '">' + (avgAcc*100).toFixed(1) + '%</span></div>' +
    '<div class="metric-row"><span class="metric-label">均值H20D精度</span><span class="metric-val">' + (avgH20*100).toFixed(1) + '%</span></div>';

  const hasPerfData = s.total_return != null && s.total_return !== '';
  // v4.6.9: 总览绩效卡显示模拟盘真实绩效（不再显示回测数字, 避免误导）
  // (paper/hasPaper/paperRet 已在函数顶部声明)
  const paperWin = paper.win_rate ? Number(paper.win_rate) : null;
  document.getElementById('ov-perf').innerHTML =
    (hasPaper ?
    '<div class="metric-row"><span class="metric-label">模拟盘累计收益</span><span class="metric-val" style="color:' + (paperRet > 0 ? 'var(--success)' : paperRet < 0 ? 'var(--danger)' : 'var(--text2)') + '">' + (paperRet>=0?'+':'') + paperRet.toFixed(2) + '%</span></div>' +
    '<div class="metric-row"><span class="metric-label">总资产</span><span class="metric-val" style="color:var(--text)">' + fmtMoney(paper.total_value||0, 0) + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">总盈亏</span><span class="metric-val" style="color:' + ((paper.total_pnl||0) >= 0 ? 'var(--success)' : 'var(--danger)') + '">' + fmtPnl(paper.total_pnl||0, 0) + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">持仓数</span><span class="metric-val">' + (paper.position_count||0) + ' 只</span></div>' +
    '<div class="metric-row"><span class="metric-label">现金</span><span class="metric-val" style="color:var(--text2)">' + fmtMoney(paper.cash||0, 0) + '</span></div>' +
    (paperWin ? '<div class="metric-row"><span class="metric-label">胜率</span><span class="metric-val">' + paperWin.toFixed(1) + '%</span></div>' : '')
    :
    '<div class="metric-row"><span class="metric-label">模拟盘累计收益</span><span class="metric-val" style="color:' + (perfRet && perfRet>0 ? 'var(--success)' : 'var(--text2)') + '">' + (perfRet != null ? (perfRet>=0?'+':'')+perfRet.toFixed(1)+'%' : '—') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">年化收益</span><span class="metric-val" style="color:' + (perfAnn && perfAnn>0 ? 'var(--success)' : 'var(--text2)') + '">' + (perfAnn != null ? (perfAnn>=0?'+':'')+perfAnn.toFixed(1)+'%' : '—') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">胜率</span><span class="metric-val">' + (s.win_rate ? s.win_rate.toFixed(1)+'%' : '—') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">盈亏比</span><span class="metric-val">' + (s.profit_factor ? s.profit_factor.toFixed(2) : '—') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">夏普比率</span><span class="metric-val" style="color:' + (s.sharpe_ratio >= 1 ? 'var(--success)' : s.sharpe_ratio >= 0.5 ? 'var(--accent)' : s.sharpe_ratio > 0 ? 'var(--danger)' : 'var(--text2)') + '">' + (s.sharpe_ratio ? s.sharpe_ratio.toFixed(2) : '—') + '</span></div>' +
    '<div class="metric-row"><span class="metric-label">最大回撤</span><span class="metric-val" style="color:' + (s.max_drawdown ? 'var(--danger)' : 'var(--text2)') + '">' + (s.max_drawdown ? '-'+Math.abs(s.max_drawdown).toFixed(1)+'%' : '—') + '</span></div>') +
    '<div class="metric-row" style="border-top:1px solid var(--bg3);padding-top:4px;margin-top:2px"><span class="metric-label" style="font-size:9px;color:var(--text3)">' + (hasPaper ? '数据源: 模拟盘 (paper_trading.db)' : '数据源: 回测') + '</span></div>';
}

// ── Predictions ──
function renderPredictions(d) {
  const activeTab = document.querySelector('#pred-tabs .tab.active');
  const tabFilter = activeTab ? activeTab.dataset.sub : 'all';
  const tierFilter = document.getElementById('scr-tier').value;
  const minScore = parseFloat(document.getElementById('scr-score').value) || 0;
  const minConf = parseFloat(document.getElementById('flt-conf').value) || 0;
  const minH5d = parseFloat(document.getElementById('flt-h5d').value) || 0;
  const minH20d = parseFloat(document.getElementById('flt-h20d').value) || 0;
  const retFilter = document.getElementById('flt-ret').value;
  const h20dRetFilter = document.getElementById('flt-h20d-ret').value;

  const preds = (d.predictions||{}).predictions || [];
  const poolMap = {};
  const pool = (d.pool||{}).stocks || [];
  pool.forEach(function(s) { poolMap[s.symbol||s.code||''] = s; });

  let filtered = preds.filter(function(p) {
    const s = (p.signal||p.direction||'').toLowerCase();
    if (tabFilter !== 'all' && s !== tabFilter) return false;
    const poolInfo = poolMap[p.symbol||p.code||''] || {};
    if (tierFilter && poolInfo.tier !== tierFilter) return false;
    const score = p.score || poolInfo.score || 0;
    if (minScore && score < minScore) return false;
    const conf = (p.confidence || p.conf || 0);
    if (minConf && conf < minConf) return false;
    const h5d = (p.direction_accuracy || p.accuracy || 0);
    if (minH5d && h5d < minH5d) return false;
    const h20d = p.h20d_accuracy || (p.h20d||{}).direction_accuracy || 0;
    if (minH20d && h20d < minH20d) return false;
    const ret = p.predicted_change || p.predicted_return || p.change || 0;
    if (retFilter) { const fv = parseFloat(retFilter); if (fv >= 0 && ret < fv) return false; if (fv < 0 && ret > fv) return false; }
    const h20dRet = p.h20d_predicted_return || (p.h20d||{}).predicted_return || 0;
    if (h20dRetFilter) { const fv = parseFloat(h20dRetFilter); if (fv >= 0 && h20dRet < fv) return false; if (fv < 0 && h20dRet > fv) return false; }
    return true;
  });

  const tbody = document.querySelector('#pred-table tbody');
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--text2);padding:24px"><div class="empty-icon" style="font-size:24px;margin-bottom:8px">🔍</div>无匹配结果<br><span style="font-size:11px;opacity:.6">尝试调整筛选条件</span></td></tr>';
    document.getElementById('pred-count').textContent = '0 只';
    document.getElementById('pred-freshness').textContent = '';
    return;
  }

  const portfolioCodes = getPortfolioCodes();
  const sorted = [].concat(filtered).sort(function(a,b) {
    const aPF = portfolioCodes.has(a.symbol||a.code||'') ? 0 : 1;
    const bPF = portfolioCodes.has(b.symbol||b.code||'') ? 0 : 1;
    if (aPF !== bPF) return aPF - bPF;
    const sigOrder = {buy:0, sell:1, hold:2};
    const sa = sigOrder[(a.signal||'').toLowerCase()] != null ? sigOrder[(a.signal||'').toLowerCase()] : 9;
    const sb = sigOrder[(b.signal||'').toLowerCase()] != null ? sigOrder[(b.signal||'').toLowerCase()] : 9;
    if (sa !== sb) return sa - sb;
    const ca = a.confidence || 0, cb = b.confidence || 0;
    if (ca !== cb) return cb - ca;
    return Math.abs(b.predicted_return||0) - Math.abs(a.predicted_return||0);
  });

  tbody.innerHTML = sorted.map(function(p) {
    const inPortfolio = portfolioCodes.has(p.symbol||p.code||'');
    const dir = (p.signal || p.direction || '').toLowerCase();
    const cls = dir === 'buy' ? 'signal-buy' : dir === 'sell' ? 'signal-sell' : 'signal-hold';
    const conf = (p.confidence || p.conf || 0);
    const change = p.predicted_change || p.predicted_return || p.change || 0;
    const h20dObj = p.h20d || {};
    const h20dRet = p.h20d_predicted_return || h20dObj.predicted_return || 0;
    const h20dAcc = p.h20d_accuracy || h20dObj.direction_accuracy || 0;
    const acc = (p.direction_accuracy || p.accuracy || 0);
    const weight = p.signal_weight || p.weight || 1.0;
    const tier = p.tier || '';
    const tierCN = {alpha:'α池',core:'核心',bench:'基准',bluechip:'蓝筹',growth:'成长',flex:'灵活',cyclical:'周期'};
    const tierName = tierCN[tier] || tier;
    const tierCls = {alpha:'tag-bluechip',core:'tag-core',bench:'tag-growth'}[tier]||'';
    const poolInfo = poolMap[p.symbol||p.code||''] || {};
    let score = p.score || 0;
    if (poolInfo.score && poolInfo.score !== 50) { score = poolInfo.score; }
    const conceptsRaw = (poolInfo.concept || poolInfo.concepts || '');
    const conceptsClean = conceptsRaw
      .replace(/A股-(申万行业|申万二级|热门概念|地域板块)-/g, '')
      .split(',').filter(Boolean)
      .map(function(c) { return c.trim(); })
      .join(' · ');
    const xc = p.cross_confirmed;
    const src = xc ? 'h5d+h20d' : (p.source||'h5d');
    const srcCls = xc ? 'tag-bluechip' : (src==='pool_fallback' ? 'tag-growth' : '');
    const isSuspended = p.confidence_level === 'suspended' || p.source === 'suspended';
    const confCell = isSuspended
      ? '<span style="color:var(--text2);font-size:12px" title="观察池标的，仅监控不产生交易信号">仅监控</span>'
      : (conf*100).toFixed(1) + '%';
    const retCell = isSuspended
      ? '<span style="color:var(--text2)">—</span>'
      : '<span style="color:' + (change >= 0 ? 'var(--signal-buy)' : 'var(--signal-sell)') + '">' + (change*100).toFixed(2) + '%</span>';
    const weightCell = isSuspended ? '<span style="color:var(--text2)">—</span>' : weight.toFixed(2);
    const pfRowStyle = inPortfolio ? 'background:rgba(245,158,11,.08);border-left:3px solid var(--accent)' : '';
    const pfBadge = inPortfolio ? ' <span style="font-size:10px;background:var(--accent);color:#000;padding:1px 5px;border-radius:3px;font-weight:600" title="当前持仓">📦持仓</span>' : '';
    const rowOpacity = (isSuspended && !inPortfolio) ? ' style="opacity:0.6"' : '';
    return '<tr' + rowOpacity + ' style="' + pfRowStyle + '">' +
      '<td>' + esc(p.symbol||p.code||'') + '</td>' +
      '<td>' + esc(p.name||'') + pfBadge + (isSuspended && !inPortfolio ? ' <span style="font-size:10px;color:var(--text2)">[观察]</span>' : '') + '</td>' +
      '<td style="font-size:11px;color:var(--text2);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(conceptsRaw) + '">' + esc(conceptsClean) + '</td>' +
      '<td>' + (tier ? '<span class="tag ' + tierCls + '">' + tierName + '</span>' : '-') + '</td>' +
      '<td>' + weightCell + '</td>' +
      '<td>' + (score ? '<span style="font-weight:500">' + score + '</span>' : '-') + '</td>' +
      '<td><span class="tag ' + cls + '">' + (dir === 'buy' ? '买入' : dir === 'sell' ? '卖出' : '持有') + '</span></td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + retCell + '</td>' +
      '<td>' + h20dRetCell(h20dRet) + '</td>' +
      '<td>' + h5dDisplay(acc) + '</td>' +
      '<td>' + h20dDisplay(h20dAcc, acc, h20dRet) + '</td>' +
      '<td><span class="tag ' + srcCls + '">' + src + '</span></td>' +
      '</tr>';
  }).join('');

  document.getElementById('pred-count').textContent = sorted.length + ' 只';
  document.getElementById('pred-freshness').textContent = '⏱ ' + new Date().toLocaleTimeString();
  setTimeout(checkTableScroll, 50);
}

// ── Models ──
// v4.7.3: 截面Rank IC面板
function renderRankIC() {
  fetch('/api/rank-ic').then(function(r){ return r.json(); }).then(function(ic){
    const body = document.getElementById('model-ic-body');
    const badge = document.getElementById('model-ic-badge');
    if (!body) return;
    const s = ic.summary || {};
    if (!s || !s.n_days) { body.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div>暂无IC数据 (rank_ic_monitor 尚未运行)</div>'; return; }
    const st = s.drift_status || 'healthy';
    const stMap = {healthy:['#0dc9a2','✅ healthy'], degraded:['#ffb020','⚠️ degraded'], critical:['#ff4747','🚨 critical'], drifted:['#ffb020','🟡 drifted'], data_issue:['#ff4747','🔴 data_issue']};
    const conf = stMap[st] || ['#888', st];
    badge.innerHTML = '<span style="color:' + conf[0] + '">' + conf[1] + '</span>';
    const series = (ic.series||[]).slice(-30);
    let spark = '';
    if (series.length >= 5) {
      const vals = series.map(function(x){ return x.rank_ic; });
      const mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals), rng = (mx-mn)||1;
      const w = 300, h = 40, n = vals.length;
      let pts = '';
      vals.forEach(function(v, i){ pts += (i/(n-1)*w).toFixed(1) + ',' + (h - (v-mn)/rng*h).toFixed(1) + ' '; });
      spark = '<svg width="300" height="44" style="background:rgba(255,255,255,.03);border-radius:6px;margin-top:6px">' +
        '<line x1="0" y1="' + (h-(0-mn)/rng*h) + '" x2="' + w + '" y2="' + (h-(0-mn)/rng*h) + '" stroke="#444" stroke-dasharray="3,3"/>' +
        '<polyline points="' + pts.trim() + '" fill="none" stroke="' + conf[0] + '" stroke-width="1.8"/></svg>';
    }
    const reasons = (s.drift_reasons||[]).map(function(x){ return '<div style="color:var(--danger);font-size:11px;margin:2px 0">⚠️ ' + esc(x) + '</div>'; }).join('');
    body.innerHTML =
      '<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">' +
      '<div><div class="value ' + (s.rank_ic_mean>=0?'green':'red') + '">' + (s.rank_ic_mean==null?'—':s.rank_ic_mean.toFixed(4)) + '</div><div class="label">Rank IC 均值(' + s.n_days + '日)</div></div>' +
      '<div><div class="value ' + (s.recent20_mean==null||s.recent20_mean>=0?'green':'red') + '">' + (s.recent20_mean==null?'—':s.recent20_mean.toFixed(4)) + '</div><div class="label">近20日均值</div></div>' +
      '<div><div class="value">' + (s.rank_icir_30d==null?'—':s.rank_icir_30d.toFixed(3)) + '</div><div class="label">ICIR(30日)</div></div>' +
      '<div><div class="value">' + (s.updated_at||'').slice(0,16) + '</div><div class="label">更新时间</div></div>' +
      '</div>' + spark + reasons;
  }).catch(function(e){ console.error('rank-ic fetch failed', e); });
}

function renderModels(d, filter) {
  filter = filter || 'all';
  const cal = d.calibration || {};
  const sum = cal.summary || {};
  // v4.6.9d: 动态阈值 — 与后端同源, 不再硬编码0.45
  const th = cal.thresholds || {retrain_urgent: 0.45, retrain_planned: 0.55};
  document.getElementById('model-summary').innerHTML =
    '<div class="card card-sm"><h3>📊 总标的</h3><div class="value blue">' + (sum.total||0) + '</div></div>' +
    '<div class="card card-sm"><h3>🔴 急迫重训</h3><div class="value red">' + (sum.retrain_urgent||0) + '</div></div>' +
    '<div class="card card-sm"><h3>🟡 计划重训</h3><div class="value yellow">' + (sum.retrain_planned||0) + '</div></div>' +
    '<div class="card card-sm"><h3>🟢 正常</h3><div class="value green">' + (sum.normal||0) + '</div></div>';
  renderRankIC();

  const stocks = cal.stocks || [];
  const tbody = document.querySelector('#model-table tbody');
  let filtered = stocks;
  if (filter === 'urgent') filtered = stocks.filter(function(s) { return s.accuracy < th.retrain_urgent; });
  else if (filter === 'planned') filtered = stocks.filter(function(s) { return s.calibration_status === 'retrain_planned'; });

  if (!filtered.length) { tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--text2);padding:24px">暂无模型数据</td></tr>'; document.getElementById('model-freshness').textContent = ''; return; }

  const preds = (d.predictions||{}).predictions || [];
  const predMap = {};
  preds.forEach(function(p) { predMap[p.symbol||p.code||''] = p; });

  const pfCodes = getPortfolioCodes();
  filtered.sort(function(a,b) {
    const aPF = pfCodes.has(a.symbol) ? 0 : 1;
    const bPF = pfCodes.has(b.symbol) ? 0 : 1;
    if (aPF !== bPF) return aPF - bPF;
    return (a.accuracy||0) - (b.accuracy||0);
  });

  // v4.6.9d: 训练时间新鲜度 — 相对时间 + 陈旧标黄 (>48h)
  function trainTimeHtml(tt) {
    if (!tt) return '-';
    const m = String(tt).match(/^(\d{2})-(\d{2}) (\d{2}):(\d{2})$/);
    if (!m) return tt;
    const d = new Date();
    let y = d.getFullYear();
    let ts = new Date(y, parseInt(m[1],10)-1, parseInt(m[2],10), parseInt(m[3],10), parseInt(m[4],10));
    if (ts > d) ts = new Date(y-1, parseInt(m[1],10)-1, parseInt(m[2],10), parseInt(m[3],10), parseInt(m[4],10));
    const hours = Math.floor((d - ts) / 3600000);
    if (hours < 0) return tt;
    const stale = hours >= 48;
    const label = hours < 1 ? '刚刚' : hours < 24 ? hours + 'h前' : Math.floor(hours/24) + '天前';
    return '<span style="' + (stale ? 'color:#f59e0b;' : 'color:var(--text2);') + '" title="' + tt + '">' + label + '</span>';
  }

  tbody.innerHTML = filtered.map(function(s) {
    const inPF = pfCodes.has(s.symbol||'');
    const acc = s.accuracy || 0;
    const accPct = (acc*100).toFixed(1);
    const isAccDefault = Math.abs(acc - 0.5) < 0.001;
    const h5dColor = isAccDefault ? 'var(--text2)' : (acc >= th.retrain_planned ? 'var(--success)' : acc >= th.retrain_urgent ? 'var(--accent)' : 'var(--danger)');
    const h5dHtml = isAccDefault ? '<span style="color:var(--text2);font-size:11px">待训练</span>' : '<span style="color:' + h5dColor + '">' + accPct + '%</span>';
    const barCls = isAccDefault ? 'skipped' : (acc >= th.retrain_planned ? 'green' : acc >= th.retrain_urgent ? 'yellow' : 'red');
    const status = s.calibration_status || 'normal';
    const statusCls = status === 'retrain_urgent' ? 'failed' : status === 'retrain_planned' ? 'skipped' : 'completed';
    const matched = predMap[s.symbol] || {};
    const h20dAcc = matched.h20d_accuracy || (matched.h20d||{}).direction_accuracy || s.h20d_accuracy || 0;
    const pfRowStyleM = inPF ? 'background:rgba(245,158,11,.08);border-left:3px solid var(--accent)' : '';
    const pfBadgeM = inPF ? ' <span style="font-size:10px;background:var(--accent);color:#000;padding:1px 5px;border-radius:3px;font-weight:600" title="当前持仓">📦持仓</span>' : '';
    const obsBadge = (s.pool_status === 'observation' || s.pool_status === 'orphan') ? ' <span style="font-size:10px;background:rgba(96,165,250,.15);color:#7db8ff;padding:1px 5px;border-radius:3px;font-weight:600" title="观察池标的 — 30天精度≥50%可自动回池">🔭观察</span>' : '';
    // v4.6.9d: P2-1 Correct列 → 显示 H5D方向正确数 (realized_correct), h20d_correct 移到tooltip
    const h5dCorrect = s.realized_correct || 0;
    const h20dInfo = (s.h20d_correct != null && s.h20d_total) ? (s.h20d_correct + '/' + s.h20d_total) : '';
    // v4.6.9d: P3-2 精度走势 sparkline
    const hist = s.accuracy_history || [];
    const sparkHtml = hist.length >= 2 ? sparkline(hist, 80, 22) : '<span style="color:var(--text3);font-size:10px">—</span>';
    // v4.6.9d: P2-3 状态列 — tab过滤激活时用简短图标, 否则用完整标签
    let statusHtml;
    if (filter !== 'all') {
      const ic = status === 'retrain_urgent' ? '🔴' : status === 'retrain_planned' ? '🟡' : '🟢';
      statusHtml = '<span title="' + (status === 'retrain_urgent' ? '急迫重训' : status === 'retrain_planned' ? '计划重训' : '正常') + '">' + ic + '</span>';
    } else {
      statusHtml = '<span class="task-status ' + statusCls + '">' + (status === 'retrain_urgent' ? '🔴 急迫重训' : status === 'retrain_planned' ? '🟡 计划重训' : '🟢 正常') + '</span>';
    }
    return '<tr style="' + pfRowStyleM + '">' +
      '<td>' + esc(s.symbol||'') + '</td>' +
      '<td>' + esc(s.name||'') + pfBadgeM + obsBadge + '</td>' +
      '<td style="color:' + h5dColor + '">' + h5dHtml + '</td>' +
      '<td>' + h20dDisplay(h20dAcc, acc) + '</td>' +
      '<td>' + statusHtml + '</td>' +
      '<td>' + (s.total_predictions || 0) + '</td>' +
      '<td>' + h5dCorrect + (h20dInfo ? ' <span style="font-size:9px;color:var(--text3)" title="H20D正确/总数">(' + h20dInfo + ')</span>' : '') + '</td>' +
      '<td style="font-size:11px;white-space:nowrap">' + trainTimeHtml(s.train_time) + '</td>' +
      '<td style="white-space:nowrap">' + (s.calibration_status === 'retrain_urgent' ? '<button onclick="triggerRetrain(\'' + esc(s.symbol).replace(/'/g,'&#39;') + '\')" style="background:rgba(255,71,87,.12);border:1px solid rgba(255,71,87,.3);color:var(--danger);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px;margin-right:4px">排队</button><button onclick="retrainNow(\'' + esc(s.symbol).replace(/'/g,'&#39;') + '\')" style="background:rgba(0,212,170,.12);border:1px solid rgba(0,212,170,.3);color:var(--success);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:11px">立即</button>' : '-') + '</td>' +
      '<td>' + sparkHtml + '</td>' +
      '</tr>';
  }).join('');
  document.getElementById('model-freshness').textContent = '⏱ ' + new Date().toLocaleTimeString();
}

// ── Pipeline v5.0 ──
const PHASE_ICONS = {night:'🌙',premarket:'🌅',intraday:'☀️',postmarket:'🌤️',evening:'🌆',weekly:'📅'};
const STATUS_LABELS = {completed:'已完成',running:'执行中',failed:'失败',timeout:'超时',idle:'待执行',skipped:'跳过',missed:'⏰跳票',stale:'⚠️已中断'};
const SVG_CIRCUMFERENCE = 2 * Math.PI * 18; // ≈ 113.1 — computed, not hardcoded

function fmtTimeLabel(dtStr) {
  if (!dtStr) return '';
  const d = new Date(dtStr);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const taskDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const daysDiff = Math.floor((today - taskDate) / 86400000);
  const time = d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
  if (daysDiff === 0) return '<span class="tl-tag today">今天 ' + time + '</span>';
  if (daysDiff === 1) return '<span class="tl-tag yesterday">昨天 ' + time + '</span>';
  return '<span class="tl-tag older">' + (d.getMonth()+1) + '/' + d.getDate() + ' ' + time + '</span>';
}

function _matchesDow(date, cronDow) {
  if (!cronDow || cronDow === '*') return true;
  const dow = date.getDay();
  const partsArr = cronDow.split(',');
  for (let i = 0; i < partsArr.length; i++) {
    const part = partsArr[i].trim();
    const idx = part.indexOf('-');
    if (idx !== -1) {
      const lo = parseInt(part.substring(0, idx)), hi = parseInt(part.substring(idx + 1));
      if (lo <= dow && dow <= hi) return true;
    } else if (parseInt(part) === dow) {
      return true;
    }
  }
  return false;
}

function fmtNextTime(t) {
  const now = new Date();
  const parts = (t.time||'00:00').split(':');
  const h = parseInt(parts[0])||0, m = parseInt(parts[1])||0;
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0);
  if (next <= now) next.setDate(next.getDate() + 1);
  while (!_matchesDow(next, t.cron_dow)) {
    next.setDate(next.getDate() + 1);
  }
  return '<span class="tl-tag upcoming">' + (next.getMonth()+1) + '/' + next.getDate() + ' ' +
    next.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}) + '</span>';
}

function msgClass(status, message) {
  if (status==='failed') return 'error';
  if (status==='timeout') return 'error';
  if (status==='stale') return 'presumed';
  if (status==='completed' && message && message.indexOf('推定完成') >= 0) return 'presumed';
  if (status==='completed') return 'done';
  if (status==='idle') return 'warn';
  if (status==='skipped') return 'info';
  if (status==='missed') return 'missed';
  return '';
}

async function renderPipeline(d) {
  let pipeline;
  if (d && d.pipeline && d.pipeline.timeline) {
    pipeline = d.pipeline;
  } else {
    try {
      const r = await fetch('/api/pipeline');
      pipeline = await r.json();
    } catch(e) {
      document.getElementById('pipeline-timeline').innerHTML = '<div class="loading"><div class="spinner"></div>加载中...</div>';
      return;
    }
  }

  const stats = pipeline.stats || {};
  const total = stats.total || 15;
  const done = (stats.completed||0) + (stats.failed||0) + (stats.skipped||0);
  const pct = total > 0 ? Math.round(done/total*100) : 0;
  const remaining = total - done;
  const dashOffset = SVG_CIRCUMFERENCE * (1 - pct / 100);

  document.getElementById('pl-stats').innerHTML =
    '<div class="pl-stat"><div class="pl-stat-val" style="color:var(--success)">' + (stats.completed||0) + '</div><div class="pl-stat-lbl">已完成</div></div>' +
    '<div class="pl-divider"></div>' +
    '<div class="pl-stat"><div class="pl-stat-val" style="color:var(--accent)">' + (stats.running||0) + '</div><div class="pl-stat-lbl">执行中</div></div>' +
    '<div class="pl-divider"></div>' +
    '<div class="pl-stat"><div class="pl-stat-val" style="color:var(--danger)">' + (stats.failed||0) + '</div><div class="pl-stat-lbl">失败</div></div>' +
    '<div class="pl-divider"></div>' +
    '<div class="pl-stat"><div class="pl-stat-val" style="color:var(--accent)">' + (stats.idle||0) + '</div><div class="pl-stat-lbl">待执行</div></div>' +
    '<div class="pl-divider"></div>' +
    '<div class="pl-stat"><div class="pl-stat-val" style="color:var(--text2)">' + (stats.skipped||0) + '</div><div class="pl-stat-lbl">跳过</div></div>' +
    '<div class="pl-progress-ring">' +
      '<svg width="44" height="44"><circle cx="22" cy="22" r="18" fill="none" stroke="var(--bg3)" stroke-width="4"/>' +
      '<circle cx="22" cy="22" r="18" fill="none" stroke="var(--success)" stroke-width="4" ' +
        'stroke-dasharray="' + SVG_CIRCUMFERENCE.toFixed(4) + '" stroke-dashoffset="' + dashOffset.toFixed(4) + '" stroke-linecap="round"/></svg>' +
      '<div style="display:flex;flex-direction:column;align-items:center">' +
        '<div class="pr-text">' + pct + '%</div>' +
        '<div style="font-size:9px;color:var(--text2)">' + (remaining>0?'剩'+remaining+'项':'全部完成') + '</div>' +
      '</div>' +
    '</div>';

  const tl = pipeline.timeline || {};
  const phases = pipeline.phases_order || [];
  if (!phases.length) {
    document.getElementById('pipeline-timeline').innerHTML = '<div class="empty-state"><div class="empty-icon">⏱️</div>暂无时序数据<br><span class="empty-hint">检查 data/task_progress.json 或 pipeline 状态文件</span></div>';
    return;
  }

  let html = '';
  phases.forEach(function(phaseKey) {
    const phase = tl[phaseKey];
    if (!phase || !phase.tasks || !phase.tasks.length) return;
    const doneCount = phase.tasks.filter(function(t) { return t.status==='completed'||t.status==='failed'||t.status==='timeout'||t.status==='skipped'; }).length;
    const hasCurrent = phase.tasks.some(function(t) { return t.is_current; });
    html += '<div class="phase-group">';
    html += '<div class="phase-header' + (hasCurrent?' phase-active':'') + '">' +
      '<span class="phase-icon">' + (PHASE_ICONS[phaseKey]||'') + '</span>' +
      '<span class="phase-label">' + phase.label + '</span>' +
      '<span class="phase-bar"></span>' +
      '<span class="phase-count">' + doneCount + '/' + phase.tasks.length + '</span></div>';

    phase.tasks.forEach(function(t) {
      const status = t.status || 'idle';
      const isActuallyRunning = t.is_current && status === 'running';
      const isScheduled = t.is_current && status !== 'running';
      const extraCls = isActuallyRunning ? 'current' : (t.is_next ? 'next' : (isScheduled ? 'scheduled' : ''));
      const mCls = msgClass(status, t.message);

      let timeInfo = '';
      if (status === 'completed' || status === 'failed' || status === 'timeout') {
        timeInfo = fmtTimeLabel(t.completed_at || t.started_at);
      } else if (status === 'idle') {
        timeInfo = fmtNextTime(t);
      } else if (status === 'running') {
        timeInfo = t.started_at ? '<span class="tl-tag today">' + new Date(t.started_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}) + ' 启动</span>' : '';
      } else {
        timeInfo = t.completed_at ? fmtTimeLabel(t.completed_at) : '';
      }

      const description = t.description || t.desc || '';
      html += '<div class="timeline-row ' + status + ' ' + extraCls + '" ' +
        'onclick="openTaskReports(\'' + esc(t.logical_id||t.task_id) + '\',\'' + esc(t.name||'').replace(/&/g,'&amp;').replace(/'/g,'&#39;') + '\')" ' +
        'title="' + (isActuallyRunning?'⚡ 当前执行中 — ':isScheduled?'⏰ 计划执行时段 — ':t.is_next?'⏩ 下一个执行 — ':'') + '点击查看报告">' +
        '<div class="timeline-time">' + (t.time||'') + '</div>' +
        '<div class="timeline-dot"><div class="dot-circle ' + status + '"></div></div>' +
        '<div class="timeline-content">' +
          '<div class="tl-name">' + (t.icon||'') + ' ' + esc(t.name) +
            (isActuallyRunning ? ' <span style="font-size:10px;animation:pulse-text 2s ease-in-out infinite">⚡执行中</span>' : '') +
            (isScheduled ? ' <span style="font-size:10px;color:var(--text2)">⏰ 计划</span>' : '') +
            (t.is_next ? ' <span style="font-size:10px;color:var(--accent);font-weight:600">▶ 下一个</span>' : '') +
          '</div>' +
          (t.message ? '<div class="tl-msg ' + mCls + '">' + t.message + '</div>' : '') +
          (description ? '<div class="tl-desc">' + description + '</div>' : '') +
          '<div class="tl-time-info">' + timeInfo + '</div>' +
        '</div>' +
        '<div class="timeline-meta">' +
          '<span class="tl-status ' + status + '">' + (STATUS_LABELS[status]||status) + '</span>' +
          '<span class="tl-model">' + (t.model||'') + '</span>' +
        '</div>' +
        '<button class="tl-trigger" onclick="event.stopPropagation();triggerCronJob(\'' + esc(t.logical_id||t.task_id) + '\',this)" title="手动触发此任务" aria-label="触发任务">⚡</button>' +
        '<div class="timeline-expand">▶</div>' +
      '</div>';
    });
    html += '</div>';
  });
  document.getElementById('pipeline-timeline').innerHTML = html;
}

// ── Task Reports Modal ──
let _lastFocusedEl = null;
async function openTaskReports(taskId, taskName) {
  _lastFocusedEl = document.activeElement;
  let modal = document.getElementById('report-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'report-modal';
    modal.className = 'modal-overlay';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'modal-title');
    modal.innerHTML =
      '<div class="modal-panel" role="document">' +
        '<div class="modal-header">' +
          '<h2 id="modal-title">📋 任务报告</h2>' +
          '<button class="modal-close" onclick="closeReportModal()" aria-label="关闭">✕</button>' +
        '</div>' +
        '<div class="modal-body" id="modal-body">' +
          '<div class="loading"><div class="spinner"></div>加载报告...</div>' +
        '</div>' +
      '</div>';
    modal.addEventListener('click', function(e) { if (e.target === modal) closeReportModal(); });
    document.body.appendChild(modal);
  }
  modal.style.display = 'flex';
  modal.querySelector('.modal-close').focus();
  document.getElementById('modal-title').textContent = '📋 ' + esc(taskName) + ' · 历史报告';
  document.getElementById('modal-body').innerHTML = '<div class="loading"><div class="spinner"></div>加载报告...</div>';
  document.addEventListener('keydown', _modalEscHandler);

  try {
    const r = await fetch('/api/task-reports/' + encodeURIComponent(taskId));
    const data = await r.json();
    const reports = data.reports || [];
    const body = document.getElementById('modal-body');
    if (!reports.length) {
      body.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div>该任务尚未生成执行报告<br><span class="empty-hint">此任务将在下次执行后自动生成报告。</span></div>';
      return;
    }
    body.innerHTML = reports.map(function(rp) {
      const status = rp.status || 'unknown';
      const started = rp.started_at ? new Date(rp.started_at).toLocaleString() : '';
      const completed = rp.completed_at ? new Date(rp.completed_at).toLocaleString() : '';
      const raw = rp.raw && Object.keys(rp.raw).length ? JSON.stringify(rp.raw, null, 2).slice(0, 800) : '';
      return '<div class="report-item">' +
        '<div class="ri-header">' +
          '<span class="ri-id">' + esc(rp.id||'?') + '</span>' +
          '<span class="ri-status ' + esc(status) + '">' + esc(status).toUpperCase() + '</span>' +
        '</div>' +
        (rp.summary ? '<div class="ri-msg">' + esc(rp.summary) + '</div>' : rp.message ? '<div class="ri-msg">' + esc(rp.message) + '</div>' : '') +
        '<div class="ri-time">' + esc(started) + (completed ? ' → ' + esc(new Date(rp.completed_at).toLocaleTimeString()) : '') + '</div>' +
        (raw ? '<details style="margin-top:6px"><summary style="font-size:11px;color:var(--text2);cursor:pointer">查看详情</summary><div class="ri-raw">' + esc(raw) + '</div></details>' : '') +
      '</div>';
    }).join('');
  } catch(e) {
    document.getElementById('modal-body').innerHTML = '<div class="empty-state"><div class="empty-icon">❌</div>加载失败: ' + esc(e.message) + '</div>';
  }
}
function _modalEscHandler(e) { if (e.key === 'Escape') { closeReportModal(); } }
function closeReportModal() {
  const modal = document.getElementById('report-modal');
  if (modal) { modal.style.display = 'none'; document.removeEventListener('keydown', _modalEscHandler); }
  if (_lastFocusedEl) { try { _lastFocusedEl.focus(); } catch(e) {} _lastFocusedEl = null; }
}

// ── v4.6.x: Cron触发 ──
async function triggerCronJob(taskId, btn) {
  btn.textContent = '⏳'; btn.disabled = true;
  try {
    const r = await fetch('/api/cron/trigger/' + encodeURIComponent(taskId), {method:'POST'});
    const d = await r.json();
    if (d.triggered) {
      btn.textContent = '✓'; btn.style.color = 'var(--success)';
      btn.title = '已触发: ' + (d.job_name || taskId);
      setTimeout(function() { btn.textContent = '⚡'; btn.style.color = ''; btn.disabled = false; }, 3000);
    } else {
      btn.textContent = '✗'; btn.title = '失败: ' + (d.error || 'unknown');
      setTimeout(function() { btn.textContent = '⚡'; btn.style.color = ''; btn.disabled = false; }, 3000);
    }
  } catch(e) {
    btn.textContent = '✗'; btn.title = '网络错误';
    setTimeout(function() { btn.textContent = '⚡'; btn.style.color = ''; btn.disabled = false; }, 3000);
  }
}

// ── Retrain ──
async function triggerRetrain(code) {
  if (!confirm('确认将 ' + code + ' 加入重训队列？\n(下次batch_train时执行)')) return;
  const btn = event.target;
  btn.textContent = '⏳...'; btn.disabled = true;
  try {
    const r = await fetch('/api/retrain', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({codes: [code]}),
    });
    await r.json();
    btn.textContent = '✅已排队';
    btn.style.background = 'rgba(0,212,170,.12)';
    btn.style.borderColor = 'rgba(0,212,170,.3)';
    btn.style.color = 'var(--success)';
  } catch(e) { btn.textContent = '❌失败'; }
}

async function retrainNow(code) {
  if (!confirm('确认立即重训 ' + code + '？\n(后台执行训练→预测→校准，约2-4分钟)')) return;
  const btn = event.target;
  btn.textContent = '⏳启动中'; btn.disabled = true;
  btn.style.background = 'rgba(234,179,8,.12)';
  btn.style.borderColor = 'rgba(234,179,8,.3)';
  btn.style.color = 'var(--accent)';
  try {
    const r = await fetch('/api/retrain-now', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({code: code}),
    });
    const data = await r.json();
    if (data.ok) {
      btn.textContent = '⏳训练中';
      let polls = 0;
      const maxPolls = 20;
      const poll = setInterval(async function() {
        polls++;
        try {
          const sr = await fetch('/api/retrain-status?code=' + code);
          const sd = await sr.json();
          if (sd.status === 'done') {
            clearInterval(poll);
            btn.textContent = '✅完成';
            btn.style.background = 'rgba(0,212,170,.12)';
            btn.style.borderColor = 'rgba(0,212,170,.3)';
            btn.style.color = 'var(--success)';
            const resp = await fetch('/api/full');
            if (resp.ok) { DATA = await resp.json(); renderModels(DATA); }
          } else if (sd.status === 'error') {
            clearInterval(poll);
            btn.textContent = '❌失败';
            btn.title = sd.error || '训练失败';
          } else if (polls >= maxPolls) {
            clearInterval(poll);
            btn.textContent = '⏰超时';
            btn.title = '训练超时，请查看日志';
          } else {
            btn.textContent = '⏳' + (sd.status||'...') + '(' + (polls*15) + 's)';
          }
        } catch(e) {}
      }, 15000);
    } else {
      btn.textContent = '❌失败';
      btn.title = data.error || '启动失败';
    }
  } catch(e) { btn.textContent = '❌失败'; btn.title = e.message; }
}

// ── v5.0: Accuracy Trend with Tooltips ──
async function renderAccuracyTrend() {
  try {
    const r = await fetch('/api/accuracy-trend');
    const data = await r.json();
    const daily = (data.daily_records || []).filter(function(d) { return d.total > 0 || (d.non_hold && d.non_hold.total > 0); });
    if (!daily.length) {
      document.getElementById('ov-trend').innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div>交易日后将自动积累兑现数据</div>';
      return;
    }
    document.getElementById('ov-trend-card').style.display = 'block';
        document.getElementById('ov-trend-card').style.display = 'block';

    // v4.6.2: sparkline + 统计摘要
    var accSeq = daily.map(function(d) {
      var nh2 = d.non_hold;
      return nh2 && nh2.total > 0 ? nh2.accuracy : d.accuracy;
    });
    var lastAcc2 = accSeq[accSeq.length-1] || 0;
    var bestAcc = Math.max.apply(null, accSeq);
    var worstAcc = Math.min.apply(null, accSeq);
    var trendUp = accSeq.length > 1 && lastAcc2 >= accSeq[0];
    var sparkSvg = sparkline(accSeq, 280, 48);

    document.getElementById('ov-trend').innerHTML =
      '<div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;padding:8px 0">' +
        '<div style="flex:1;min-width:200px">' + sparkSvg + '</div>' +
        '<div style="display:flex;gap:12px;flex-wrap:wrap">' +
          '<div class="card-minor" style="text-align:center;min-width:60px"><div class="' + (trendUp ? 'value green' : 'value red') + '" style="font-size:18px">' + (lastAcc2*100).toFixed(1) + '%</div><div class="sub-text" style="font-size:9px">最新</div></div>' +
          '<div class="card-minor" style="text-align:center;min-width:60px"><div class="value yellow" style="font-size:18px">' + (bestAcc*100).toFixed(1) + '%</div><div class="sub-text" style="font-size:9px">最高</div></div>' +
          '<div class="card-minor" style="text-align:center;min-width:60px"><div class="value" style="font-size:18px;color:var(--danger)">' + (worstAcc*100).toFixed(1) + '%</div><div class="sub-text" style="font-size:9px">最低</div></div>' +
          '<div class="card-minor" style="text-align:center;min-width:60px"><div class="value" style="font-size:18px">' + daily.length + '</div><div class="sub-text" style="font-size:9px">天数</div></div>' +
        '</div>' +
      '</div>' +
      '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' +
        daily.slice(-10).reverse().map(function(d) {
          var nh3 = d.non_hold;
          var hasNh3 = nh3 && nh3.total > 0;
          var acc3 = hasNh3 ? nh3.accuracy : d.accuracy;
          var pct = (acc3*100).toFixed(0);
          var clr3 = acc3 >= 0.5 ? 'var(--success)' : 'var(--danger)';
          return '<div style="display:flex;flex-direction:column;align-items:center;gap:2px;cursor:pointer;padding:4px 6px;border-radius:6px;transition:all.15s" ' +
            'onmouseenter="this.style.background=\'var(--accent-dim)\" showDailyTooltip(event,this)" onmouseleave="hideTrendTooltip()" ' +
            'data-d="' + d.date + '" data-acc="' + pct + '%" data-nh="' + (hasNh3 ? (nh3.accuracy*100).toFixed(1)+'%' : '-') + '" data-n="' + (d.correct||0) + '/' + (d.total||0) + '">' +
            '<div style="width:20px;height:' + Math.max(3, Math.min(40, pct)) + 'px;background:' + clr3 + ';border-radius:2px;opacity:.7;transition:height.3s"></div>' +
            '<span style="font-size:9px;color:var(--text2)">' + d.date.slice(-5) + '</span>' +
            '<span style="font-size:10px;color:' + clr3 + '">' + pct + '%</span>' +
          '</div>';
        }).join('') +
      '</div>';
  } catch(e) {}
}

// ── Trend Tooltip ──
function showTrendTooltip(ev, el) {
  const tooltip = document.getElementById('trend-tooltip');
  // v4.6.2: 兼容新旧两种数据格式
  const date = el.getAttribute('data-d') || el.getAttribute('data-trend-date') || '';
  const acc = el.getAttribute('data-acc') || el.getAttribute('data-trend-val') || '';
  const nhInfo = el.getAttribute('data-nh') || el.getAttribute('data-trend-nonhold-acc') || '';
  const count = el.getAttribute('data-n') || '';
  const ovAcc = el.getAttribute('data-trend-ov-acc') || '';
  const correct = el.getAttribute('data-trend-correct') || el.getAttribute('data-n')?.split('/')[0] || '0';
  const total = el.getAttribute('data-trend-total') || el.getAttribute('data-n')?.split('/')[1] || '0';

  let inner = '<div class="tt-date">' + date + '</div>';
  if (nhInfo && nhInfo !== '-') {
    inner += '<div class="tt-row"><span class="tt-label">非hold兑现</span><span class="tt-val" style="color:#7ec8ff">' + nhInfo + '</span></div>';
  }
  if (ovAcc) {
    inner += '<div class="tt-row"><span class="tt-label">整体含hold</span><span class="tt-val" style="color:var(--success)">' + ovAcc + '</span></div>';
  } else if (acc) {
    inner += '<div class="tt-row"><span class="tt-label">兑现精度</span><span class="tt-val" style="color:var(--success)">' + acc + '</span></div>';
  }
  if (count) {
    inner += '<div class="tt-row"><span class="tt-label">兑现/预测</span><span class="tt-val">' + count + '</span></div>';
  } else {
    inner += '<div class="tt-row"><span class="tt-label">兑现/预测</span><span class="tt-val">' + correct + '/' + total + '</span></div>';
  }

  tooltip.innerHTML = inner;
  tooltip.style.display = 'block';

  const rect = el.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - 70;
  let top = rect.top - tooltip.offsetHeight - 8;
  if (top < 0) top = rect.bottom + 8;
  if (left < 8) left = 8;
  tooltip.style.left = left + 'px';
  tooltip.style.top = top + 'px';
}

function showDailyTooltip(ev, el) {
  showTrendTooltip(ev, el);
}

function hideTrendTooltip() {
  document.getElementById('trend-tooltip').style.display = 'none';
}

// ── v5.0: Attribution ──
async function renderAttribution(d) {
  const attr = d.attribution || {};
  if (!attr.last_update && !attr.total_pnl) {
    document.getElementById('ov-attribution').innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div>积累交易数据...</div>';
    return;
  }
  const hasData = attr.win_rate > 0 || attr.total_pnl !== 0;
  document.getElementById('ov-attribution').innerHTML = hasData ?
    '<div class="attribution-grid">' +
      '<div class="attr-block"><div class="attr-value" style="color:var(--success)">' + ((attr.stock_selection||0)*100).toFixed(1) + '%</div><div class="attr-label">选股α</div></div>' +
      '<div class="attr-block"><div class="attr-value" style="color:' + ((attr.timing||0)>=0?'var(--success)':'var(--danger)') + '">' + ((attr.timing||0)*100).toFixed(1) + '%</div><div class="attr-label">择时</div></div>' +
      '<div class="attr-block"><div class="attr-value" style="color:var(--text)">' + ((attr.win_rate||0)*100).toFixed(0) + '%</div><div class="attr-label">胜率</div></div>' +
      '<div class="attr-block"><div class="attr-value" style="color:var(--text)">' + (attr.avg_hold_days||0) + '天</div><div class="attr-label">均持仓</div></div>' +
    '</div>' +
    (attr.last_update ? '<div style="font-size:10px;color:var(--text2);margin-top:4px">更新: ' + attr.last_update.slice(0,10) + '</div>' : '')
    : '<div class="empty-state"><div class="empty-icon">⏳</div>积累交易数据...</div>';
}

// Patch renderOverview
const _origRenderOverview = renderOverview;
renderOverview = function(d) {
  _origRenderOverview(d);
  renderAccuracyTrend();
  renderAttribution(d);
};



// ── Paper Trader (v4.6.9h 恢复: 拆分时遗漏, 交易页无渲染函数) ──
async function renderPaperTrader(d) {
  const pt = d.paperTrader || {};
  const s = pt.summary || {};
  const pnlCls = (s.total_pnl||0) >= 0 ? 'green' : 'red';
  const posPct = s.total_value > 0 ? ((s.market_value||0) / s.total_value * 100).toFixed(1) : '0.0';
  const cashPct = s.total_value > 0 ? ((s.cash||0) / s.total_value * 100).toFixed(1) : '0.0';
  // v4.6.8: 价格刷新时间标示
  var priceAgeNote = '';
  if (s.price_refreshed_at) {
    var ageH = s.price_refresh_age_hours;
    var ageClr = ageH >= 4 ? 'var(--danger)' : ageH >= 1 ? 'var(--accent)' : 'var(--text3)';
    var ageEmoji = ageH >= 4 ? '⚠️' : ageH >= 1 ? '⏳' : '✅';
    priceAgeNote = '<div class="sub-text" style="color:' + ageClr + '">' + ageEmoji + ' 行情刷新 ' + s.price_refreshed_at + (ageH >= 1 ? ' (' + ageH.toFixed(1) + 'h前)' : '') + '</div>';
  }
  document.getElementById('pt-summary').innerHTML =
    '<div class="card card-sm"><h3>💰 总资产</h3><div class="value blue">' + fmtMoney(s.total_value||0, 2) + '</div>' + priceAgeNote + '</div>' +
    '<div class="card card-sm"><h3>📊 总盈亏</h3><div class="value ' + pnlCls + '">' + fmtPnl(s.total_pnl||0, 2) + ' (' + (s.total_pnl_pct||0).toFixed(1) + '%)</div></div>' +
    '<div class="card card-sm"><h3>📈 持仓市值</h3><div class="value yellow">' + fmtMoney(s.market_value||0, 2) + '</div><div class="sub-text">仓位 ' + posPct + '%</div></div>' +
    '<div class="card card-sm"><h3>💵 现金</h3><div class="value">' + fmtMoney(s.cash||0, 2) + '</div><div class="sub-text">占比 ' + cashPct + '%</div></div>' +
    '<div class="card card-sm"><h3>📦 持仓数</h3><div class="value yellow">' + (s.position_count||0) + '</div></div>';

  const nameMap = {};
  const poolStocks = (d.pool||{}).stocks || [];
  poolStocks.forEach(function(st) { nameMap[st.symbol||st.code||''] = st.name||''; });

  const getStopLine = function(p) {
    const sl = p.stop_loss;
    if (sl !== undefined && sl !== null && sl < 0) return (sl * 100).toFixed(1) + '%';
    const c = String(p.symbol||'');
    if (c.startsWith('688') || c.startsWith('300') || c.startsWith('301')) return '-15%';
    if (c.startsWith('8')) return '-22%';
    return '-8%';
  };
  const getStopLineVal = function(p) {
    const sl = p.stop_loss;
    if (sl !== undefined && sl !== null && sl < 0) return sl;
    const c = String(p.symbol||'');
    if (c.startsWith('688') || c.startsWith('300') || c.startsWith('301')) return -0.15;
    if (c.startsWith('8')) return -0.22;
    return -0.08;
  };
  const getBoard = function(p) {
    const c = String(p.symbol||'');
    if (c.startsWith('688')) return '科创板';
    if (c.startsWith('300')||c.startsWith('301')) return '创业板';
    if (c.startsWith('8')) return '北交所';
    return '主板';
  };

  const stopLossCard = document.getElementById('pt-stop-loss-card');
  const stopLossBody = document.getElementById('pt-stop-loss-body');
  const slBadge = document.getElementById('pt-sl-status-badge');
  const positions = pt.positions || [];
  const triggeredStops = positions.filter(function(p) {
    const pnl = (p.pnl_pct||0) / 100;
    return pnl <= getStopLineVal(p);
  });
  if (triggeredStops.length > 0) {
    stopLossCard.hidden = false;
    slBadge.innerHTML = '<span style="color:var(--danger);font-weight:600">⚠️ ' + triggeredStops.length + '只触发止损</span>';
    stopLossBody.innerHTML = triggeredStops.map(function(p) {
      return '<div style="padding:6px 0;border-bottom:1px solid var(--border)">🔴 <b>' + p.symbol + '</b> ' + (p.name||'') + ' | 盈亏 <span style="color:var(--danger);font-weight:600">' + (p.pnl_pct >= 0 ? '+' : '') + p.pnl_pct + '%</span> | 止损线 ' + getStopLine(p) + ' (' + getBoard(p) + ')</div>';
    }).join('');
  } else if (positions.length > 0) {
    stopLossCard.hidden = false;
    slBadge.innerHTML = '<span style="color:var(--success);font-weight:600">✅ 正常</span>';
    stopLossBody.innerHTML = '<div style="color:var(--text2)">所有持仓均在止损线以内，无止损触发</div>';
  }

  const posBody = document.querySelector('#pt-positions-table tbody');
  if (!positions.length) {
    posBody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--text2);padding:24px"><div class="empty-icon" style="font-size:24px;margin-bottom:8px">📦</div>暂无持仓</td></tr>';
  } else {
    let totalMktVal = 0, totalCostVal = 0, totalPnlVal = 0;
    const rows = positions.map(function(p) {
      totalMktVal += p.market_value||0;
      totalCostVal += p.cost_value||0;
      totalPnlVal += p.pnl||0;
      const pCls = (p.pnl||0) >= 0 ? 'var(--success)' : 'var(--danger)';
      const name = nameMap[p.symbol] || p.name || '';
      const posRatio = s.total_value > 0 ? ((p.market_value||0)/s.total_value*100).toFixed(1) : '-';
      const stopLine = getStopLine(p);
      const stopLineVal = getStopLineVal(p);
      let isNearStop = false;
      if ((p.pnl_pct||0) < 0 && stopLineVal < 0) {
        isNearStop = Math.abs(p.pnl_pct / 100) > Math.abs(stopLineVal) * 0.6;
      }
      const slTag = p.stop_loss !== undefined && p.stop_loss !== null ? ' [ATR]' : ' [固定]';
      return '<tr' + (isNearStop ? ' style="background:rgba(255,71,87,.05)"' : '') + '><td>' + p.symbol + '</td><td style="font-size:12px;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + name + '">' + name + '</td><td>' + p.quantity + '</td><td>¥' + fmt(p.avg_cost, 2) + '</td><td>¥' + fmt(p.current_price, 2) + '</td><td>' + fmtMoney(p.market_value||0, 0) + '</td><td style="color:' + pCls + '">' + fmtPnl(p.pnl||0, 2) + '</td><td style="color:' + pCls + '">' + ((p.pnl_pct||0)>=0?'+':'') + p.pnl_pct + '%</td><td style="font-size:12px;color:var(--text2)">' + posRatio + '%</td><td style="font-size:12px;color:' + (isNearStop ? 'var(--accent)' : 'var(--text2)') + '" title="' + slTag + '">' + stopLine + '</td></tr>';
    });
    const sumPnlCls = totalPnlVal >= 0 ? 'var(--success)' : 'var(--danger)';
    const sumPnlPct = totalCostVal > 0 ? (totalPnlVal / totalCostVal * 100).toFixed(2) : '0.00';
    const sumPosRatio = s.total_value > 0 ? (totalMktVal / s.total_value * 100).toFixed(1) : '0.0';
    rows.push('<tr style="font-weight:700;border-top:2px solid var(--bg3)"><td colspan="5" style="text-align:right">合计</td><td>' + fmtMoney(totalMktVal, 0) + '</td><td style="color:' + sumPnlCls + '">' + fmtPnl(totalPnlVal, 2) + '</td><td style="color:' + sumPnlCls + '">' + (Number(sumPnlPct)>=0?'+':'') + sumPnlPct + '%</td><td style="font-size:12px;font-weight:700">' + sumPosRatio + '%</td><td></td></tr>');
    posBody.innerHTML = rows.join('');
  }

  const histBody = document.querySelector('#pt-history-table tbody');
  const history = pt.history || [];
  if (!history.length) {
    histBody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text2);padding:24px"><div class="empty-icon" style="font-size:24px;margin-bottom:8px">📜</div>暂无交易记录</td></tr>';
  } else {
    histBody.innerHTML = history.map(function(h) {
      const actCls = h.action === 'BUY' ? 'var(--success)' : 'var(--danger)';
      const fee = (h.commission||0) + (h.stamp_tax||0);
      return '<tr><td>' + (h.ts||'') + '</td><td>' + h.symbol + '</td><td style="color:' + actCls + ';font-weight:600">' + h.action + '</td><td>¥' + fmt(h.price, 2) + '</td><td>' + h.qty + '</td><td>' + fmtMoney(h.amount, 0) + '</td><td style="font-size:12px;color:var(--text2)">¥' + fmt(fee, 2) + '</td><td style="font-size:11px;color:var(--text2)">' + (h.reason||'') + '</td></tr>';
    }).join('');
  }
  setTimeout(checkTableScroll, 50);
}
