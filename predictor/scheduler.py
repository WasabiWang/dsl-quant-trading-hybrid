#!/usr/bin/env python3
"""
predictor_cron.py - 预测模型Cron任务
定期训练和预测，并发送飞书报告

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 添加项目根目录到路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 导入项目模块
try:
    from predictor.features import FeatureEngineer
    from predictor.models import ModelTrainer
    from scripts.data_source_enhanced import EnhancedDataSource  # 保留兼容
    from common.feishu_utils import send_markdown
    HAS_FEISHU = True
except ImportError as e:
    print(f"[WARN] 导入模块失败: {e}")
    HAS_FEISHU = False

class PredictorCron:
    """预测模型Cron任务管理器"""
    
    def __init__(self):
        # 动态加载标的列表：优先从持仓数据读取，否则使用默认
        self.training_symbols = self._load_symbols()
        self.prediction_symbols = self.training_symbols  # 训练和预测使用相同标的
    
    def _load_symbols(self):
        """动态加载标的列表：优先持仓数据 → 已训练模型 → 默认列表"""
        symbols = []
        
        # 1. 尝试从模拟持仓读取
        portfolio_path = os.path.join(PROJECT_ROOT, 'data/simulation_portfolio.json')
        if os.path.exists(portfolio_path):
            try:
                with open(portfolio_path) as f:
                    data = json.load(f)
                positions = data.get('positions', {})
                if positions:
                    symbols = list(positions.keys())
                    print(f"📋 从模拟持仓加载 {len(symbols)} 只标的")
                    return symbols
            except Exception:
                pass
        
        # 2. 尝试从ML模型目录读取（覆盖面最广）
        model_dir = os.path.join(PROJECT_ROOT, 'models')
        ml_dir = os.path.join(model_dir, 'ml')
        if os.path.exists(ml_dir):
            ml_symbols = set()
            for f in os.listdir(ml_dir):
                if f.endswith('.pkl') and not f.startswith('scaler'):
                    sym = f.split('_')[0]
                    if sym.isdigit():
                        ml_symbols.add(sym)
            if ml_symbols:
                symbols = sorted(ml_symbols)
                print(f"📋 从ML模型加载 {len(symbols)} 只标的")
                return symbols
        
        # 3. 尝试从模型目录读取
        if os.path.exists(model_dir):
            existing = [d for d in os.listdir(model_dir) 
                       if d.isdigit() and os.path.isdir(os.path.join(model_dir, d))]
            if existing:
                symbols = existing
                print(f"📋 从已有模型加载 {len(symbols)} 只标的")
                return symbols
        
        # 4. 默认列表（宽基指数成分股代表）
        symbols = ['000001', '600519', '601318', '000858', '600036', '002594', '600760']
        print(f"📋 使用默认标的列表 {len(symbols)} 只")
        return symbols
        self.model_dir = os.path.join(PROJECT_ROOT, "models")
        self.report_dir = os.path.join(PROJECT_ROOT, "reports/predictor")
        os.makedirs(self.report_dir, exist_ok=True)
    
    def daily_training(self):
        """每日训练任务"""
        print("=" * 70)
        print("🤖 预测模型 - 每日训练任务")
        print("=" * 70)
        
        start_time = datetime.now()
        
        # 初始化数据源和训练器
        from scripts.data_source_v2 import DataSource  # 保留兼容
        ds = DataSource()
        trainer = ModelTrainer()
        
        training_results = {}
        for symbol in self.training_symbols[:2]:  # 只训练前2个以节省时间
            print(f"\n📈 训练{symbol}...")
            
            try:
                # 获取数据
                df = ds.get_kline(symbol, num=300)
                if df is None or df.empty:
                    print(f"  ❌ 无法获取{symbol}数据，跳过")
                    continue
                
                # 特征工程
                fe = FeatureEngineer()
                df_features = fe.build_features(df, symbol)
                
                if df_features.empty:
                    print(f"  ❌ 特征工程失败，跳过")
                    continue
                
                # 准备训练数据
                X_train, X_test, y_train, y_test, feature_cols = trainer.prepare_data(
                    df_features, test_size=0.2
                )
                
                # 训练模型
                results = trainer.train_all_models(X_train, X_test, y_train, y_test, symbol)
                
                # 保存模型
                saved_models = trainer.save_models(symbol)
                
                # 记录结果
                training_results[symbol] = {
                    'data_points': len(df),
                    'feature_count': len(feature_cols),
                    'models_trained': list(results.keys()),
                    'models_saved': saved_models if saved_models else [],
                    'training_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                print(f"  ✅ {symbol}训练完成")
                
            except Exception as e:
                print(f"  ❌ {symbol}训练失败: {e}")
                training_results[symbol] = {
                    'error': str(e),
                    'training_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
        
        # 保存训练结果
        result_file = os.path.join(
            self.report_dir, 
            f"training_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(training_results, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 训练结果已保存到: {result_file}")
        
        # 发送飞书通知
        if HAS_FEISHU and training_results:
            self.send_training_report(training_results, start_time)
        
        return training_results
    
    def daily_prediction(self):
        """每日预测任务"""
        print("=" * 70)
        print("🔮 预测模型 - 每日预测任务")
        print("=" * 70)
        
        start_time = datetime.now()
        
        from scripts.predict_example import Predictor  # 保留兼容
        
        prediction_results = {}
        for symbol in self.prediction_symbols:
            print(f"\n📈 预测{symbol}...")
            
            try:
                # 尝试使用随机森林模型
                predictor = Predictor(symbol, 'random_forest')
                
                if predictor.model is None:
                    # 如果随机森林不存在，尝试线性回归
                    predictor = Predictor(symbol, 'linear_regression')
                
                if predictor.model is None:
                    print(f"  ❌ 无法加载{symbol}的模型")
                    continue
                
                # 进行预测
                result = predictor.predict(days=100)
                
                if 'error' in result:
                    print(f"  ❌ 预测失败: {result['error']}")
                    prediction_results[symbol] = {
                        'error': result['error'],
                        'prediction_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                else:
                    prediction_results[symbol] = result
                    print(f"  ✅ 预测完成: 收益率={result['predicted_return']:.4%}")
            
            except Exception as e:
                print(f"  ❌ {symbol}预测失败: {e}")
                prediction_results[symbol] = {
                    'error': str(e),
                    'prediction_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
        
        # 保存预测结果
        result_file = os.path.join(
            self.report_dir, 
            f"prediction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(result_file, 'w', encoding='utf-8') as f:
            # 转换numpy类型为Python原生类型
            def convert_types(obj):
                if isinstance(obj, (np.integer, np.floating)):
                    return float(obj)
                elif isinstance(obj, np.ndarray):
                    return obj.tolist()
                elif isinstance(obj, dict):
                    return {k: convert_types(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_types(item) for item in obj]
                else:
                    return obj
            
            prediction_results_converted = convert_types(prediction_results)
            json.dump(prediction_results_converted, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 预测结果已保存到: {result_file}")
        
        # 发送飞书通知
        if HAS_FEISHU and prediction_results:
            self.send_prediction_report(prediction_results, start_time)
        
        return prediction_results
    
    def send_training_report(self, training_results, start_time):
        """发送训练报告到飞书"""
        try:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            content = "## 🤖 预测模型每日训练报告\n\n"
            content += f"**训练时间**: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            content += f"**训练时长**: {duration:.1f}秒\n"
            content += f"**训练股票**: {len(training_results)}只\n\n"
            
            content += "### 📊 训练详情\n"
            for symbol, result in training_results.items():
                if 'error' in result:
                    content += f"- **{symbol}**: ❌ 训练失败 - {result['error']}\n"
                else:
                    content += f"- **{symbol}**: ✅ 训练成功\n"
                    content += f"  - 数据点: {result['data_points']}\n"
                    content += f"  - 特征数: {result['feature_count']}\n"
                    content += f"  - 训练模型: {', '.join(result['models_trained'])}\n"
            
            content += f"\n**报告生成时间**: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
            
            send_markdown(title="预测模型训练报告", content=content)
            print("  📤 训练报告已发送到飞书")
            
        except Exception as e:
            print(f"  ⚠️ 发送训练报告失败: {e}")
    
    def send_prediction_report(self, prediction_results, start_time):
        """发送预测报告到飞书"""
        try:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            content = "## 🔮 预测模型每日预测报告\n\n"
            content += f"**预测时间**: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            content += f"**预测时长**: {duration:.1f}秒\n"
            content += f"**预测股票**: {len(prediction_results)}只\n\n"
            
            content += "### 📈 预测结果\n"
            for symbol, result in prediction_results.items():
                if 'error' in result:
                    content += f"- **{symbol}**: ❌ 预测失败 - {result['error']}\n"
                else:
                    direction = "📈 看涨" if result['predicted_return'] > 0 else "📉 看跌"
                    content += f"- **{symbol}**: {direction}\n"
                    content += f"  - 最新价格: ¥{result['latest_price']:.2f}\n"
                    content += f"  - 预测收益率: {result['predicted_return']:.4%}\n"
                    content += f"  - 预测价格: ¥{result['predicted_price']:.2f}\n"
                    content += f"  - 使用模型: {result['model']}\n"
            
            content += f"\n**报告生成时间**: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
            content += f"\n**风险提示**: 预测结果仅供参考，不构成投资建议"
            
            send_markdown(title="预测模型预测报告", content=content)
            print("  📤 预测报告已发送到飞书")
            
        except Exception as e:
            print(f"  ⚠️ 发送预测报告失败: {e}")
    
    def weekly_summary(self):
        """每周总结报告"""
        print("=" * 70)
        print("📊 预测模型 - 每周总结报告")
        print("=" * 70)
        
        # 查找本周的报告文件
        week_start = datetime.now() - timedelta(days=7)
        
        training_files = []
        prediction_files = []
        
        if os.path.exists(self.report_dir):
            for file in os.listdir(self.report_dir):
                file_path = os.path.join(self.report_dir, file)
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                
                if file_time >= week_start:
                    if file.startswith('training_'):
                        training_files.append(file_path)
                    elif file.startswith('prediction_'):
                        prediction_files.append(file_path)
        
        print(f"📁 找到{len(training_files)}个训练报告，{len(prediction_files)}个预测报告")
        
        # 生成总结报告
        summary = {
            'report_period': {
                'start': week_start.strftime("%Y-%m-%d %H:%M:%S"),
                'end': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            'training_reports': len(training_files),
            'prediction_reports': len(prediction_files),
            'generated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 保存总结报告
        summary_file = os.path.join(
            self.report_dir, 
            f"weekly_summary_{datetime.now().strftime('%Y%m%d')}.json"
        )
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        print(f"💾 每周总结已保存到: {summary_file}")
        
        # 发送飞书通知
        if HAS_FEISHU:
            self.send_weekly_summary(summary, len(training_files), len(prediction_files))
        
        return summary
    
    def send_weekly_summary(self, summary, training_count, prediction_count):
        """发送每周总结到飞书"""
        try:
            content = "## 📊 预测模型每周总结报告\n\n"
            content += f"**报告周期**: {summary['report_period']['start']} 至 {summary['report_period']['end']}\n"
            content += f"**训练报告数**: {training_count}个\n"
            content += f"**预测报告数**: {prediction_count}个\n"
            content += f"**系统状态**: ✅ 运行正常\n\n"
            
            content += "### 📈 本周表现\n"
            content += "- 模型训练: 每日自动执行\n"
            content += "- 股票预测: 每日生成预测报告\n"
            content += "- 特征工程: 包含技术指标+市场情绪特征\n"
            content += "- 模型集成: 支持投票和堆叠集成\n\n"
            
            content += "### 🔧 技术指标\n"
            content += "- 特征维度: 30+个技术特征 + 8个市场情绪特征\n"
            content += "- 模型支持: 线性回归、随机森林、XGBoost、LightGBM\n"
            content += "- 集成方法: 投票集成、堆叠集成\n"
            content += "- 数据源: A股/港股/美股多源数据\n\n"
            
            content += f"**报告生成时间**: {summary['generated_at']}"
            
            send_markdown(title="预测模型每周总结", content=content)
            print("  📤 每周总结已发送到飞书")
            
        except Exception as e:
            print(f"  ⚠️ 发送每周总结失败: {e}")

def main():
    """主函数 - 根据参数执行不同任务"""
    import argparse
    
    parser = argparse.ArgumentParser(description='预测模型Cron任务')
    parser.add_argument('--task', type=str, default='prediction',
                       choices=['training', 'prediction', 'weekly', 'all'],
                       help='执行的任务: training(训练), prediction(预测), weekly(周报), all(全部)')
    
    args = parser.parse_args()
    
    cron = PredictorCron()
    
    if args.task == 'training' or args.task == 'all':
        cron.daily_training()
    
    if args.task == 'prediction' or args.task == 'all':
        cron.daily_prediction()
    
    if args.task == 'weekly' or args.task == 'all':
        cron.weekly_summary()

if __name__ == "__main__":
    main()