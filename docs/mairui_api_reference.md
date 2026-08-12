# 麦蕊智数 API 完整参考 (2026-05-11 抓取)

> 来源: https://mairui.club/hsdata
> Licence: `<your-mairui-licence>` (从环境变量 MAIRUI_LICENCE 读取)
> 基础URL: `https://api.mairuiapi.com`
> 限流: 体验版 1000次/分钟, 建议 REQUEST_INTERVAL=0.07s

## 已配置 (mairui_api_config.py 中有函数)

| # | API名称 | 端点 | 函数名 | 说明 |
|---|---------|------|--------|------|
| 1 | 股票列表 | `/hslt/list/{licence}` | `get_all_stock_list()` | 全量A股代码+名称 |
| 2 | 单股实时(网络源) | `/hsrl/ssjy/{code}/{licence}` | `get_stock_real()` | 网络数据源, 字段丰富 |
| 3 | 单股实时(券商源) | `/hsstock/real/time/{code}/{licence}` | `get_stock_real(use_broker=True)` | 券商数据源, 更快 |
| 4 | 多股实时(≤20) | `/hsrl/ssjy_more/{licence}?stock_codes=xxx` | `get_multi_stock_real()` | 批量≤20只, **盘中监控首选** |
| 5 | 全量实时(网络) | `/hsrl/real/all/{licence}` | `request_api("all_stock_real_network")` | 包年/钻石版, 1次/分钟 |
| 6 | 全量实时(券商) | `/hsrl/ssjy/all/{licence}` | `request_api("all_stock_real_broker")` | 包年/钻石版, 1次/分钟 |
| 7 | 五档盘口 | `/hsstock/real/five/{code}/{licence}` | `get_stock_five_level()` | 买卖五档 |
| 8 | 逐笔交易 | `/hsrl/zbjy/{code}/{licence}` | `request_api("stock_transaction_detail")` | 当天逐笔 |
| 9 | 资金流向 | `/hsstock/history/transaction/{code}/{licence}` | `request_api("stock_capital_flow")` | 特大/大/中/小单 |
| 10 | 行业概念树 | `/hszg/list/{licence}` | `get_industry_concept_tree()` | 指数/行业/概念层级 |
| 11 | 概念查成分股 | `/hszg/gg/{cat_code}/{licence}` | `get_category_stocks()` | 按概念代码查成分 |
| 12 | 股票查概念 | `/hszg/zg/{code}/{licence}` | `get_stock_concepts()` | 按股票查所属概念 |
| 13 | 资产负债表 | `/hsstock/financial/balance/{code}/{licence}` | `get_balance_sheet()` | st/et/lt参数 |
| 14 | 利润表 | `/hsstock/financial/income/{code}/{licence}` | `get_income_statement()` | st/et/lt参数 |
| 15 | 现金流量表 | `/hsstock/financial/cashflow/{code}/{licence}` | `get_cashflow_statement()` | st/et/lt参数 |
| 16 | 财务主要指标 | `/hsstock/financial/pershareindex/{code}/{licence}` | `get_financial_indicators()` | EPS/ROE/毛利率等 |
| 17 | 股本结构 | `/hsstock/financial/capital/{code}/{licence}` | `get_capital_structure()` | st/et/lt参数 |
| 18 | 十大股东 | `/hsstock/financial/topholder/{code}/{licence}` | `get_top_holders()` | st/et/lt参数 |
| 19 | 十大流通股东 | `/hsstock/financial/flowholder/{code}/{licence}` | `get_top_flow_holders()` | st/et/lt参数 |
| 20 | 股东户数 | `/hsstock/financial/hm/{code}/{licence}` | `get_holder_count()` | st/et/lt参数 |
| 21 | MA均线 | `/hsstock/history/ma/{code}/{period}/{adj}/{licence}` | `get_ma()` | MA3~MA250 |
| 22 | MACD | `/hsstock/history/macd/{code}/{period}/{adj}/{licence}` | `get_macd()` | DIFF/DEA/MACD |
| 23 | BOLL布林 | `/hsstock/history/boll/{code}/{period}/{adj}/{licence}` | `get_boll()` | 上/中/下轨 |
| 24 | KDJ | `/hsstock/history/kdj/{code}/{period}/{adj}/{licence}` | `get_kdj()` | K/D/J值 |
| 25 | K线历史 | `/hsstock/history/{code}/{period}/{adj}/{licence}` | `get_kline_history()` | OHLCV |
| 26 | 涨停股池 | `/hslt/ztgc/{date}/{licence}` | `get_limit_up_list()` | 按日期 |
| 27 | 跌停股池 | `/hslt/dtgc/{date}/{licence}` | `get_limit_down_list()` | 按日期 |

## 尚未配置但可用 (文档有, config中无)

| # | API名称 | 端点 | 建议函数名 | 用途 |
|---|---------|------|-----------|------|
| 28 | 新股日历 | `/hslt/new/{licence}` | `get_new_stock_calendar()` | IPO追踪 |
| 29 | 概念指数列表(券商) | `/hslt/sectorslist/{licence}` | `get_sectors_list()` | 券商级概念列表 |
| 30 | 一级板块列表(券商) | `/hslt/primarylist/{licence}` | `get_primary_list()` | 券商级板块 |
| 31 | 板块明细(券商) | `/hslt/sectors/{name}/{licence}` | `get_sectors_detail()` | 按板块名查成分 |
| 32 | 强势股池 | `/hslt/qsgc/{date}/{licence}` | `get_strong_stock_pool()` | 涨幅倒序 |
| 33 | 次新股池 | `/hslt/cxgc/{date}/{licence}` | `get_new_stock_pool()` | 开板几日升序 |
| 34 | 炸板股池 | `/hslt/zbgc/{date}/{licence}` | `get_broken_board_pool()` | 首封时间升序 |
| 35 | 股票基础信息 | `/hsstock/instrument/{code}/{licence}` | `get_stock_instrument()` | 上市日期/行业等 |
| 36 | 历史涨跌停价 | `/hsstock/stopprice/history/{code}/{licence}` | `get_stop_price_history()` | st/et参数 |
| 37 | 行情指标 | `/hsstock/indicators/{code}/{licence}` | `get_stock_indicators()` | 综合技术指标 |

## 代码格式规范

- **A股代码**: 6位纯数字 `000001`, `600519`, `688608`
- **带后缀代码**: 仅财务/技术指标接口需要 `000001.SZ`, `600519.SH`
  - 6开头=SH, 0/3开头=SZ, 4/8开头=BJ, 688=SH(科创板)
- **分时级别**: `5/15/30/60`(分钟), `d`(日), `w`(周), `m`(月), `y`(年)
- **除权方式**: `n`(不复权), `f`(前复权), `b`(后复权), `fr`(等比前复权), `br`(等比后复权)
- **日期格式**: `YYYYMMDD` 或 `YYYYMMDDhhmmss`
- **日期(股池)**: `yyyy-MM-dd` (涨停/跌停/强势/次新/炸板)

## 麦蕊无法替代的数据 (需保留akshare/东财/新浪)

| 数据 | 原因 | 当前方案 |
|------|------|----------|
| 美股指数(S&P500/纳指) | 麦蕊仅覆盖A股 | akshare(Sina后端) → 新浪 → yfinance |
| VIX | 麦蕊无VIX接口 | 新浪 `gb_vix` → 备用 |
| USDCNY汇率 | 麦蕊无外汇接口 | 东财 → 新浪 `fx_susdcny` |
| 亚洲指数(日经/KOSPI) | 麦蕊无海外指数 | 东财 → 新浪 → ETF近似 |
| 行业板块涨跌幅 | 麦蕊有行业树但无涨跌幅 | akshare `stock_board_industry_name_em` |
| 北向资金 | 麦蕊无接口 | akshare `stock_hsgt_north_net_flow_in_em` |
| 龙虎榜 | 麦蕊无接口 | akshare `stock_lhb_detail_em` |
| A股历史K线(>241天) | 麦蕊免费版限制 | akshare本地数据 |
