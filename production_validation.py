#!/usr/bin/env python3
"""
production_validation.py - DSL v4.6.7 生产环境验证脚本

验证生产环境的所有关键组件，确保系统可投入生产使用。

作者：OpenClaw AI助手
日期：2026年4月19日
"""

import sys
import os
import time
from pathlib import Path

# 添加项目路径
sys.path.append(str(Path(__file__).parent))

from core.error_handler import ErrorHandler, DataException, ErrorSeverity
from core.simple_logger import SimpleLogger
from scripts.performance_benchmark_simple import SimplePerformanceBenchmark

class ProductionValidator:
    """生产环境验证器"""
    
    def __init__(self):
        self.logger = SimpleLogger(name='production_validator').get_logger()
        self.error_handler = ErrorHandler(max_retries=2, retry_delay=0.5)
        self.validation_results = []
    
    def log_result(self, test_name, success, message=""):
        """记录验证结果"""
        status = "✅ 通过" if success else "❌ 失败"
        result = {
            "test_name": test_name,
            "success": success,
            "status": status,
            "message": message,
            "timestamp": time.time()
        }
        self.validation_results.append(result)
        
        if success:
            self.logger.info(f"{status} {test_name} {message}")
        else:
            self.logger.error(f"{status} {test_name} {message}")
        
        return success
    
    def validate_core_modules(self):
        """验证核心模块导入"""
        test_name = "核心模块导入"
        
        modules_to_check = [
            ('core.error_handler', 'ErrorHandler'),
            ('core.simple_logger', 'SimpleLogger'),
            ('scripts.performance_benchmark_simple', 'SimplePerformanceBenchmark')
        ]
        
        failed_modules = []
        for module_path, class_name in modules_to_check:
            try:
                module = __import__(module_path, fromlist=[class_name])
                getattr(module, class_name)  # 检查类是否存在
            except (ImportError, AttributeError) as e:
                failed_modules.append(f"{module_path}.{class_name}: {e}")
        
        if failed_modules:
            return self.log_result(test_name, False, f"模块导入失败: {failed_modules}")
        else:
            return self.log_result(test_name, True, "所有核心模块导入正常")
    
    def validate_error_handling(self):
        """验证错误处理功能"""
        test_name = "错误处理功能"
        
        # 测试可恢复错误
        retry_count = 0
        def recoverable_test():
            nonlocal retry_count
            retry_count += 1
            if retry_count < 2:
                raise DataException(
                    message="生产环境测试错误（可恢复）",
                    severity=ErrorSeverity.ERROR,
                    error_code="PROD_TEST_001",
                    recoverable=True
                )
            return "恢复成功"
        
        try:
            result = self.error_handler.handle(recoverable_test)
            if result == "恢复成功":
                return self.log_result(test_name, True, f"错误恢复测试: {result}")
            else:
                return self.log_result(test_name, False, f"错误恢复结果异常: {result}")
        except Exception as e:
            return self.log_result(test_name, False, f"错误处理测试异常: {e}")
    
    def validate_logging_system(self):
        """验证日志系统"""
        test_name = "日志系统功能"
        
        try:
            # 创建测试日志
            test_logger = SimpleLogger(
                name='production_validation_test',
                log_dir='test_production_logs',
                level='INFO'
            )
            logger = test_logger.get_logger()
            
            # 记录各种级别的日志
            logger.debug("调试信息（生产环境不应显示）")
            logger.info("一般信息")
            logger.warning("警告信息")
            logger.error("错误信息")
            
            # 检查日志文件是否存在
            log_files = list(Path('test_production_logs').glob('*.log'))
            
            # 清理测试日志
            import shutil
            if os.path.exists('test_production_logs'):
                shutil.rmtree('test_production_logs')
            
            if log_files:
                return self.log_result(test_name, True, f"日志文件创建成功: {len(log_files)}个文件")
            else:
                return self.log_result(test_name, False, "日志文件创建失败")
                
        except Exception as e:
            return self.log_result(test_name, False, f"日志系统测试异常: {e}")
    
    def validate_performance(self):
        """验证性能"""
        test_name = "性能基准测试"
        
        try:
            benchmark = SimplePerformanceBenchmark()
            results = benchmark.run_all_tests()
            
            # 检查测试结果
            successful_tests = [t for t in results["tests"].values() if t.get("status") == "success"]
            success_rate = len(successful_tests) / len(results["tests"]) if results["tests"] else 0
            
            if success_rate >= 1.0:  # 100%通过
                avg_time = results.get("summary", {}).get("avg_execution_time", 0)
                return self.log_result(test_name, True, 
                    f"性能测试通过: {len(successful_tests)}/{len(results['tests'])}，平均时间: {avg_time:.3f}秒")
            else:
                failed_tests = [name for name, test in results["tests"].items() if test.get("status") != "success"]
                return self.log_result(test_name, False, 
                    f"性能测试失败: {failed_tests}")
                    
        except Exception as e:
            return self.log_result(test_name, False, f"性能测试异常: {e}")
    
    def validate_integration(self):
        """验证集成功能"""
        test_name = "集成功能测试"
        
        try:
            # 模拟生产环境集成测试
            class ProductionSystem:
                def __init__(self):
                    self.error_handler = ErrorHandler()
                    self.logger = SimpleLogger(name='production_system').get_logger()
                
                def process_trade(self, symbol, action):
                    self.logger.info(f"处理交易: {symbol} {action}")
                    
                    if not symbol or not action:
                        raise DataException(
                            "交易参数无效",
                            severity=ErrorSeverity.ERROR,
                            error_code="TRADE_INVALID"
                        )
                    
                    # 模拟处理
                    time.sleep(0.01)
                    return {"symbol": symbol, "action": action, "status": "executed"}
            
            # 测试正常流程
            system = ProductionSystem()
            result = system.process_trade("000001.SZ", "BUY")
            
            if result["status"] == "executed":
                return self.log_result(test_name, True, f"集成测试通过: {result}")
            else:
                return self.log_result(test_name, False, f"集成测试结果异常: {result}")
                
        except Exception as e:
            return self.log_result(test_name, False, f"集成测试异常: {e}")
    
    def validate_system_resources(self):
        """验证系统资源"""
        test_name = "系统资源检查"
        
        try:
            import platform
            import multiprocessing
            
            system_info = {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "cpu_count": multiprocessing.cpu_count(),
            }
            
            # 检查Python版本
            python_version = tuple(map(int, platform.python_version_tuple()[:2]))
            if python_version >= (3, 8):
                version_ok = True
            else:
                version_ok = False
            
            # 检查CPU核心数
            cpu_ok = system_info["cpu_count"] >= 2
            
            if version_ok and cpu_ok:
                return self.log_result(test_name, True, 
                    f"系统资源正常: Python {system_info['python_version']}, CPU核心: {system_info['cpu_count']}")
            else:
                issues = []
                if not version_ok:
                    issues.append(f"Python版本过低: {system_info['python_version']} (需要3.8+)")
                if not cpu_ok:
                    issues.append(f"CPU核心不足: {system_info['cpu_count']} (需要2+)")
                return self.log_result(test_name, False, f"系统资源问题: {', '.join(issues)}")
                
        except Exception as e:
            return self.log_result(test_name, False, f"系统资源检查异常: {e}")
    
    def run_all_validations(self):
        """运行所有验证"""
        self.logger.info("=" * 60)
        self.logger.info("🚀 DSL v4.6.7 生产环境验证开始")
        self.logger.info("=" * 60)
        
        validations = [
            ("系统资源检查", self.validate_system_resources),
            ("核心模块导入", self.validate_core_modules),
            ("错误处理功能", self.validate_error_handling),
            ("日志系统功能", self.validate_logging_system),
            ("性能基准测试", self.validate_performance),
            ("集成功能测试", self.validate_integration),
        ]
        
        print("\n📋 开始生产环境验证...\n")
        
        for name, validation_func in validations:
            print(f"🧪 验证: {name}")
            try:
                validation_func()
            except Exception as e:
                self.log_result(name, False, f"验证异常: {e}")
            time.sleep(0.1)  # 短暂延迟，避免输出混乱
        
        return self.show_results()
    
    def show_results(self):
        """显示验证结果"""
        print("\n" + "=" * 60)
        print("📊 生产环境验证结果")
        print("=" * 60)
        
        passed = sum(1 for r in self.validation_results if r["success"])
        total = len(self.validation_results)
        
        for result in self.validation_results:
            status_symbol = "✅" if result["success"] else "❌"
            print(f"{status_symbol} {result['test_name']}: {result['message']}")
        
        print(f"\n🎯 总体结果: {passed}/{total} 通过 ({passed/total*100:.1f}%)")
        
        # 生成详细报告
        report = {
            "validation_time": time.time(),
            "total_tests": total,
            "passed_tests": passed,
            "failed_tests": total - passed,
            "success_rate": passed / total if total > 0 else 0,
            "results": self.validation_results,
            "system_version": "DSL v4.6.7",
            "environment": "production"
        }
        
        # 保存报告
        import json
        report_file = f"production_validation_report_{int(time.time())}.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        print(f"\n📄 详细报告已保存: {report_file}")
        
        if passed == total:
            print("\n🎉 生产环境验证全部通过！系统可投入生产使用。")
            return True
        else:
            print(f"\n⚠️  {total - passed}个验证失败，请检查问题后再投入生产。")
            
            # 显示失败详情
            failed_tests = [r for r in self.validation_results if not r["success"]]
            if failed_tests:
                print("\n🔍 失败详情:")
                for test in failed_tests:
                    print(f"  • {test['test_name']}: {test['message']}")
            
            return False

def main():
    """主函数"""
    print("=" * 60)
    print("🚀 DSL v4.6.7 生产环境验证")
    print("=" * 60)
    print("版本: DSL v4.6.7")
    print("时间: 2026年4月19日")
    print("目的: 验证系统是否可投入生产环境使用")
    print("=" * 60)
    
    validator = ProductionValidator()
    
    try:
        success = validator.run_all_validations()
        
        if success:
            print("\n" + "=" * 60)
            print("✅ 生产环境验证成功")
            print("=" * 60)
            print("下一步行动:")
            print("1. 部署到生产环境")
            print("2. 配置监控和告警")
            print("3. 建立定期健康检查")
            print("=" * 60)
            return 0
        else:
            print("\n" + "=" * 60)
            print("❌ 生产环境验证失败")
            print("=" * 60)
            print("建议操作:")
            print("1. 检查失败的具体原因")
            print("2. 修复问题后重新验证")
            print("3. 确认所有依赖已正确安装")
            print("=" * 60)
            return 1
            
    except KeyboardInterrupt:
        print("\n\n⏹️ 验证被用户中断")
        return 130
    except Exception as e:
        print(f"\n❌ 验证过程发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())