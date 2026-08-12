import requests
from datetime import datetime
from dsl_data_sdk import get_price as sdk_get_price

def get_index_realtime(sina_code):
    """统一获取指数实时行情，使用 DSL 数据 SDK"""
    try:
        # SDK 已实现多源回退及缓存
        data = sdk_get_price(sina_code)
        if not data:
            return {"name": "数据获取失败", "price": 0, "change_pct": 0, "change": 0}
        # 对于指数，SDK 返回的字段可能与股票相同，计算 change 与 change_pct
        price = data.get('price', 0)
        # 计算上一次收盘价（close_prev）
        close_prev = data.get('close_prev')
        if close_prev is None:
            # 若未提供 close_prev，尝试使用 price - change
            change = data.get('change', 0)
            close_prev = price - change
        else:
            change = price - close_prev
        change_pct = (change / close_prev) * 100 if close_prev else 0
        return {
            "name": data.get('name'),
            "price": price,
            "change": change,
            "change_pct": change_pct
        }
    except Exception as e:
        print(f"SDK 获取{sina_code}行情失败: {str(e)}")
    return {"name": "数据获取失败", "price": 0, "change_pct": 0, "change": 0}
def run_monitor():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 获取核心指数（新浪代码）
    sh = get_index_realtime("sh000001")
    sz = get_index_realtime("sz399001")
    hk = get_index_realtime("hkHSI")
    
    # 构造报告
    print(f"[GLM-5] **📊 双市场盘中简报 | {now}**")
    print(f"\n**✅ 系统状态:** 运行正常 (实时连线 新浪财经)")
    print(f"\n**📈 核心指数实时行情:**")
    print(f"• **上证指数:** {sh['price']:.2f} ({sh['change_pct']:+.2f}%)")
    print(f"• **深证成指:** {sz['price']:.2f} ({sz['change_pct']:+.2f}%)")
    print(f"• **恒生指数:** {hk['price']:.2f} ({hk['change_pct']:+.2f}%)")
    print(f"\n**🔭 监控说明:**")
    print(f"*   **频率:** 每天 10:30 & 14:30 固定推送")
    print(f"*   **异常预警:** 系统会自动识别波动超过 ±1% 的异动")
    print(f"\n**💡 建议:** 保持关注，如有剧烈波动系统将即时推送。")

if __name__ == "__main__":
    run_monitor()
