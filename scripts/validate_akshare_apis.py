#!/usr/bin/env python3
"""
定期验证akshare接口可用性
建议每周运行一次，确保接口仍然可用
"""
import akshare as ak
import pandas as pd
import inspect
from datetime import datetime, timedelta
import json
import os

def validate_interface(func_name, expected_params=None, test_args=None):
    """验证单个接口是否可用"""
    try:
        # 检查接口是否存在
        if not hasattr(ak, func_name):
            return {"status": "missing", "error": f"接口{func_name}不存在"}
        
        func = getattr(ak, func_name)
        
        # 检查函数签名
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        
        # 如果有测试参数，尝试调用
        if test_args:
            try:
                result = func(**test_args)
                if result is None:
                    return {"status": "error", "error": "返回None"}
                elif isinstance(result, pd.DataFrame) and len(result) == 0:
                    return {"status": "empty", "error": "返回空DataFrame"}
                else:
                    return {"status": "ok", "params": params, "result_shape": result.shape if hasattr(result, 'shape') else str(type(result))}
            except Exception as e:
                return {"status": "error", "error": str(e), "params": params}
        else:
            return {"status": "exists", "params": params}
    
    except Exception as e:
        return {"status": "error", "error": str(e)}

def main():
    """主验证函数"""
    print(f"🔍 akshare接口验证 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"akshare版本: {getattr(ak, '__version__', 'unknown')}")
    
    # 需要验证的核心接口
    interfaces_to_test = [
        {
            "name": "stock_individual_fund_flow",
            "test_args": {"stock": "000001", "market": "sz"},
            "description": "个股资金流"
        },
        {
            "name": "stock_hsgt_hist_em",
            "test_args": {"symbol": "北向资金"},
            "description": "北向资金历史"
        },
        {
            "name": "stock_hsgt_individual_em",
            "test_args": {"symbol": "002008"},
            "description": "个股北向资金"
        },
        {
            "name": "index_hist_sw",
            "test_args": {"symbol": "801780"},
            "description": "申万行业指数"
        },
        {
            "name": "stock_zh_index_daily",
            "test_args": {"symbol": "sh000922"},
            "description": "指数日线数据"
        },
        {
            "name": "stock_notice_report",
            "test_args": {"symbol": "000001", "start_date": "20240101", "end_date": "20240420"},
            "description": "公告数据"
        },
        {
            "name": "stock_lhb_detail_em",
            "test_args": {"start_date": "20240401", "end_date": "20240420"},
            "description": "龙虎榜数据"
        }
    ]
    
    results = []
    all_ok = True
    
    for interface in interfaces_to_test:
        print(f"\n📊 验证 {interface['name']} ({interface['description']})...")
        result = validate_interface(interface['name'], test_args=interface.get('test_args'))
        
        # 显示结果
        if result["status"] == "ok":
            print(f"   ✅ 通过 - 参数: {result.get('params', [])}, 返回: {result.get('result_shape', 'N/A')}")
        elif result["status"] == "exists":
            print(f"   ℹ️  存在 - 参数: {result.get('params', [])}")
        elif result["status"] == "missing":
            print(f"   ❌ 缺失 - {result['error']}")
            all_ok = False
        elif result["status"] == "empty":
            print(f"   ⚠️  空数据 - {result['error']}")
            all_ok = False
        elif result["status"] == "error":
            print(f"   ❌ 错误 - {result['error']}")
            all_ok = False
        
        results.append({
            "interface": interface['name'],
            "description": interface['description'],
            **result
        })
    
    # 总结报告
    print(f"\n{'='*60}")
    print(f"📋 验证总结")
    print(f"{'='*60}")
    
    ok_count = len([r for r in results if r["status"] == "ok"])
    exists_count = len([r for r in results if r["status"] == "exists"])
    error_count = len([r for r in results if r["status"] in ["error", "missing", "empty"]])
    
    print(f"✅ 通过: {ok_count}个")
    print(f"ℹ️  存在: {exists_count}个")
    print(f"❌ 问题: {error_count}个")
    
    if all_ok:
        print(f"\n🎉 所有核心接口验证通过！")
    else:
        print(f"\n⚠️  发现接口问题，请检查akshare版本或接口调用方式")
        print(f"   建议：pip install akshare --upgrade")
        print(f"   或查看：https://github.com/akfamily/akshare")
    
    # 保存验证结果
    report = {
        "timestamp": datetime.now().isoformat(),
        "akshare_version": getattr(ak, '__version__', 'unknown'),
        "summary": {"ok": ok_count, "exists": exists_count, "error": error_count, "all_ok": all_ok},
        "details": results
    }
    
    # 保存到日志目录
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "akshare_validation")
    os.makedirs(log_dir, exist_ok=True)
    report_path = os.path.join(log_dir, f"validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n📄 详细报告已保存到: {report_path}")
    
    return all_ok

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)