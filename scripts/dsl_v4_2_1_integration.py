#!/usr/bin/env python3
"""
DSL v4.5.1 中优先级功能集成主脚本

功能：
1. LLM策略生成器集成
2. 现有策略导入仓库
3. 多标回测引擎集成
4. 生成集成报告

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

class DSLv421Integration:
    """DSL v4.5.1 集成管理器"""
    
    def __init__(self):
        self.project_root = Path(__file__).parent.parent
        self.integration_report = {
            "integration_id": f"dsl_v421_integration_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "start_time": datetime.now().isoformat(),
            "modules": {},
            "results": {},
            "status": "running"
        }
    
    def log_step(self, step_name: str, status: str, details: str = ""):
        """记录集成步骤"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {step_name}: {status} {details}")
        
        if "steps" not in self.integration_report:
            self.integration_report["steps"] = []
        
        self.integration_report["steps"].append({
            "step": step_name,
            "status": status,
            "timestamp": timestamp,
            "details": details
        })
    
    def check_prerequisites(self) -> bool:
        """检查前置条件"""
        self.log_step("检查前置条件", "开始")
        
        prerequisites = {
            "项目根目录": self.project_root.exists(),
            "策略目录": (self.project_root / "strategies").exists(),
            "LLM模块": (self.project_root / "llm").exists(),
            "核心模块": (self.project_root / "core").exists(),
            "回测模块": (self.project_root / "simulation").exists(),
        }
        
        all_ok = True
        for name, exists in prerequisites.items():
            status = "✅ 存在" if exists else "❌ 缺失"
            self.log_step(f"检查 {name}", status)
            if not exists:
                all_ok = False
        
        self.integration_report["prerequisites"] = prerequisites
        self.log_step("检查前置条件", "完成" if all_ok else "失败")
        return all_ok
    
    def integrate_llm_strategy_generator(self) -> bool:
        """集成LLM策略生成器"""
        self.log_step("集成LLM策略生成器", "开始")
        
        try:
            # 1. 检查LLM策略生成器脚本
            llm_script = self.project_root / "scripts" / "integrate_llm_strategy.py"
            if not llm_script.exists():
                self.log_step("LLM集成脚本", "❌ 不存在", "正在创建...")
                # 这里应该调用创建脚本的逻辑
                return False
            
            # 2. 测试LLM策略生成
            self.log_step("测试LLM策略生成", "进行中")
            
            # 运行测试
            test_cmd = [
                sys.executable, str(llm_script),
                "--test"
            ]
            
            result = subprocess.run(test_cmd, capture_output=True, text=True, cwd=self.project_root)
            
            if result.returncode == 0:
                self.log_step("LLM策略生成测试", "✅ 通过")
                
                # 3. 批量生成示例策略
                self.log_step("批量生成示例策略", "进行中")
                
                descriptions_file = self.project_root / "strategy_descriptions.json"
                if descriptions_file.exists():
                    batch_cmd = [
                        sys.executable, str(llm_script),
                        "--batch", str(descriptions_file)
                    ]
                    
                    batch_result = subprocess.run(batch_cmd, capture_output=True, text=True, cwd=self.project_root)
                    
                    if batch_result.returncode == 0:
                        self.log_step("批量生成策略", "✅ 完成")
                        self.integration_report["results"]["llm_generation"] = {
                            "status": "success",
                            "output": batch_result.stdout[-500:]  # 保存最后500字符
                        }
                        return True
                    else:
                        self.log_step("批量生成策略", "❌ 失败", batch_result.stderr[:200])
                        return False
                else:
                    self.log_step("策略描述文件", "❌ 不存在")
                    return False
            else:
                self.log_step("LLM策略生成测试", "❌ 失败", result.stderr[:200])
                return False
                
        except Exception as e:
            self.log_step("集成LLM策略生成器", "❌ 异常", str(e))
            return False
    
    def import_existing_strategies(self) -> bool:
        """导入现有策略到仓库"""
        self.log_step("导入现有策略", "开始")
        
        try:
            import_script = self.project_root / "scripts" / "import_existing_strategies.py"
            if not import_script.exists():
                self.log_step("导入脚本", "❌ 不存在")
                return False
            
            # 1. 扫描现有策略
            self.log_step("扫描现有策略", "进行中")
            
            scan_cmd = [
                sys.executable, str(import_script),
                "--scan"
            ]
            
            scan_result = subprocess.run(scan_cmd, capture_output=True, text=True, cwd=self.project_root)
            
            if scan_result.returncode != 0:
                self.log_step("扫描策略", "❌ 失败", scan_result.stderr[:200])
                return False
            
            # 2. 导入策略到仓库
            self.log_step("导入策略到仓库", "进行中")
            
            import_cmd = [
                sys.executable, str(import_script),
                "--import"
            ]
            
            import_result = subprocess.run(import_cmd, capture_output=True, text=True, cwd=self.project_root)
            
            if import_result.returncode == 0:
                self.log_step("导入策略", "✅ 完成")
                
                # 3. 生成策略目录
                self.log_step("生成策略目录", "进行中")
                
                catalog_cmd = [
                    sys.executable, str(import_script),
                    "--catalog"
                ]
                
                catalog_result = subprocess.run(catalog_cmd, capture_output=True, text=True, cwd=self.project_root)
                
                if catalog_result.returncode == 0:
                    self.log_step("生成策略目录", "✅ 完成")
                    self.integration_report["results"]["strategy_import"] = {
                        "status": "success",
                        "catalog_generated": True
                    }
                    return True
                else:
                    self.log_step("生成策略目录", "⚠️ 部分完成", "导入成功但目录生成失败")
                    return True  # 导入成功就算部分成功
            else:
                self.log_step("导入策略", "❌ 失败", import_result.stderr[:200])
                return False
                
        except Exception as e:
            self.log_step("导入现有策略", "❌ 异常", str(e))
            return False
    
    def integrate_pool_backtest(self) -> bool:
        """集成多标回测引擎"""
        self.log_step("集成多标回测引擎", "开始")
        
        try:
            pool_script = self.project_root / "scripts" / "integrate_pool_backtest.py"
            if not pool_script.exists():
                self.log_step("回测集成脚本", "❌ 不存在")
                return False
            
            # 运行集成测试
            self.log_step("测试回测集成", "进行中")
            
            test_cmd = [
                sys.executable, str(pool_script),
                "--test"
            ]
            
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, cwd=self.project_root)
            
            if test_result.returncode == 0:
                self.log_step("回测集成测试", "✅ 通过")
                
                # 运行批量回测示例
                self.log_step("运行批量回测示例", "进行中")
                
                batch_cmd = [
                    sys.executable, str(pool_script),
                    "--batch",
                    "--start", "2025-01-01",
                    "--end", "2025-12-31"
                ]
                
                batch_result = subprocess.run(batch_cmd, capture_output=True, text=True, cwd=self.project_root)
                
                if batch_result.returncode == 0:
                    self.log_step("批量回测示例", "✅ 完成")
                    self.integration_report["results"]["pool_backtest"] = {
                        "status": "success",
                        "output": batch_result.stdout[-500:]  # 保存最后500字符
                    }
                    return True
                else:
                    self.log_step("批量回测示例", "⚠️ 部分完成", "测试通过但示例运行失败")
                    return True  # 测试通过就算部分成功
            else:
                self.log_step("回测集成测试", "❌ 失败", test_result.stderr[:200])
                return False
                
        except Exception as e:
            self.log_step("集成多标回测引擎", "❌ 异常", str(e))
            return False
    
    def generate_integration_report(self) -> Dict[str, Any]:
        """生成集成报告"""
        self.log_step("生成集成报告", "开始")
        
        # 计算总体状态
        results = self.integration_report.get("results", {})
        
        success_count = sum(1 for r in results.values() if r.get("status") == "success")
        total_count = len(results)
        
        overall_status = "success" if success_count == total_count else "partial" if success_count > 0 else "failed"
        
        self.integration_report.update({
            "end_time": datetime.now().isoformat(),
            "status": overall_status,
            "summary": {
                "total_modules": total_count,
                "successful_modules": success_count,
                "failed_modules": total_count - success_count,
                "success_rate": f"{success_count/total_count*100:.1f}%" if total_count > 0 else "0%"
            }
        })
        
        # 保存报告
        report_file = self.project_root / f"integration_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(self.integration_report, f, indent=2, ensure_ascii=False)
        
        self.log_step("生成集成报告", "✅ 完成", f"报告已保存: {report_file}")
        
        # 生成Markdown摘要
        self._generate_markdown_summary(report_file)
        
        return self.integration_report
    
    def _generate_markdown_summary(self, report_file: Path):
        """生成Markdown摘要"""
        summary_file = self.project_root / "INTEGRATION_SUMMARY.md"
        
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"# DSL v4.5.1 功能集成摘要\n\n")
            f.write(f"**集成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**总体状态**: {self.integration_report['status'].upper()}\n\n")
            
            f.write("## 📊 集成结果\n\n")
            
            # 模块状态表格
            f.write("| 模块 | 状态 | 详情 |\n")
            f.write("|------|------|------|\n")
            
            for step in self.integration_report.get("steps", []):
                status_emoji = "✅" if step["status"] in ["完成", "通过"] else "❌" if step["status"] in ["失败", "异常"] else "⚠️"
                f.write(f"| {step['step']} | {status_emoji} {step['status']} | {step.get('details', '')} |\n")
            
            f.write("\n## 🎯 下一步建议\n\n")
            
            if self.integration_report["status"] == "success":
                f.write("1. **验证生成策略**: 检查生成的策略文件是否正确\n")
                f.write("2. **测试回测功能**: 使用真实数据测试回测引擎\n")
                f.write("3. **部署到生产**: 将集成功能部署到生产环境\n")
            elif self.integration_report["status"] == "partial":
                f.write("1. **修复失败模块**: 检查失败模块的详细错误\n")
                f.write("2. **重新运行集成**: 修复后重新运行集成脚本\n")
                f.write("3. **验证可用功能**: 测试已成功集成的功能\n")
            else:
                f.write("1. **检查前置条件**: 确认所有必要模块都存在\n")
                f.write("2. **查看详细日志**: 分析集成过程中的错误\n")
                f.write("3. **分步手动集成**: 考虑分步骤手动集成\n")
            
            f.write(f"\n## 🔗 相关文件\n\n")
            f.write(f"- 详细报告: `{report_file.name}`\n")
            f.write(f"- 策略目录: `strategy_catalog.md`\n")
            f.write(f"- 批量回测结果: `batch_backtest_results_*.json`\n")
            
            f.write(f"\n---\n")
            f.write(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
        
        print(f"\n📄 Markdown摘要已生成: {summary_file}")
    
    def run_full_integration(self) -> bool:
        """运行完整集成流程"""
        print("=" * 60)
        print("🚀 DSL v4.5.1 中优先级功能集成")
        print("=" * 60)
        
        # 1. 检查前置条件
        if not self.check_prerequisites():
            print("\n❌ 前置条件检查失败，无法继续集成")
            return False
        
        print("\n" + "=" * 60)
        print("📦 开始功能集成...")
        print("=" * 60)
        
        # 2. 集成LLM策略生成器
        llm_success = self.integrate_llm_strategy_generator()
        
        # 3. 导入现有策略
        import_success = self.import_existing_strategies()
        
        # 4. 集成多标回测引擎
        backtest_success = self.integrate_pool_backtest()
        
        # 5. 生成报告
        report = self.generate_integration_report()
        
        # 显示最终结果
        print("\n" + "=" * 60)
        print("🎉 集成完成!")
        print("=" * 60)
        
        summary = report["summary"]
        print(f"\n📊 集成统计:")
        print(f"   总模块数: {summary['total_modules']}")
        print(f"   成功模块: {summary['successful_modules']}")
        print(f"   失败模块: {summary['failed_modules']}")
        print(f"   成功率: {summary['success_rate']}")
        print(f"\n📈 总体状态: {report['status'].upper()}")
        
        return report["status"] in ["success", "partial"]

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="DSL v4.5.1 功能集成工具")
    parser.add_argument("--run", action="store_true", help="运行完整集成")
    parser.add_argument("--check", action="store_true", help="仅检查前置条件")
    parser.add_argument("--llm", action="store_true", help="仅集成LLM策略生成器")
    parser.add_argument("--import", dest="import_strat", action="store_true", 
                       help="仅导入现有策略")
    parser.add_argument("--backtest", action="store_true", help="仅集成回测引擎")
    
    args = parser.parse_args()
    
    integrator = DSLv421Integration()
    
    if args.check:
        integrator.check_prerequisites()
    elif args.llm:
        integrator.integrate_llm_strategy_generator()
    elif args.import_strat:
        integrator.import_existing_strategies()
    elif args.backtest:
        integrator.integrate_pool_backtest()
    elif args.run:
        success = integrator.run_full_integration()
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        print("\n📋 使用示例:")
        print("  python3 dsl_v4_2_1_integration.py --run    # 运行完整集成")
        print("  python3 dsl_v4_2_1_integration.py --check  # 检查前置条件")
        print("  python3 dsl_v4_2_1_integration.py --llm    # 仅集成LLM")
        print("  python3 dsl_v4_2_1_integration.py --import # 仅导入策略")
        print("  python3 dsl_v4_2_1_integration.py --backtest # 仅集成回测")

if __name__ == "__main__":
    main()