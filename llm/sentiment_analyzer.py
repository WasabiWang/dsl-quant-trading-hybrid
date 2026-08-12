"""
LLM新闻情绪分析模块
核心功能：分析新闻对股票的影响，给出情绪评分和影响等级
"""
import json
import logging
import subprocess
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from typing import List, Dict
from scripts.stock_news_aggregator import get_stock_related_news
logger = logging.getLogger(__name__)
class LLMSentimentAnalyzer:
    def __init__(self, model: str = "nvidia/z-ai/glm5"):
        self.model = model
    
    def analyze_news(self, news: List[Dict], stock_code: str, stock_name: str) -> Dict:
        """
        分析新闻对指定股票的情绪影响
        :param news: 新闻列表，每条包含title、link、published
        :param stock_code: 股票代码
        :param stock_name: 股票名称
        :return: 分析结果：
            - sentiment_score: 情绪评分，-1~1之间，-1极空，1极多
            - impact_level: 影响等级：0无影响，1低，2中，3高
            - key_events: 关键事件列表
            - suggestion: 交易建议调整
        """
        if not news:
            return {
                "sentiment_score": 0.0,
                "impact_level": 0,
                "key_events": [],
                "suggestion": "无相关新闻，情绪中性"
            }
        
        prompt = f"""
        你是专业的股票市场情绪分析师，请分析以下新闻对股票【{stock_name}({stock_code})】的影响：
        
        新闻列表：
        {json.dumps(news, ensure_ascii=False, indent=2)}
        
        请输出JSON格式的分析结果，包含以下字段：
        - sentiment_score: 情绪评分，-1到1之间的浮点数，-1表示极度利空，1表示极度利好，0表示中性
        - impact_level: 影响等级，0=无影响，1=低影响，2=中等影响，3=高影响
        - key_events: 字符串数组，列出对股价有重大影响的关键事件
        - suggestion: 交易建议调整，简要说明应该加仓/减仓/观望
        
        不要输出任何其他内容，只输出JSON。
        """
        
        try:
            # 调用OpenClaw LLM接口
            cmd = [
                "openclaw", "run", "--model", self.model,
                "--prompt", prompt.replace('"', '\\"'),
                "--output-format", "json"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                logger.error(f"LLM调用失败: {result.stderr}")
                return {
                    "sentiment_score": 0.0,
                    "impact_level": 0,
                    "key_events": [],
                    "suggestion": "情绪分析失败，默认中性"
                }
        except Exception as e:
            logger.error(f"情绪分析异常: {str(e)}")
            return {
                "sentiment_score": 0.0,
                "impact_level": 0,
                "key_events": [],
                "suggestion": "情绪分析异常，默认中性"
            }
    
    def batch_analyze(self, stock_list: List[Dict]) -> Dict[str, Dict]:
        """批量分析多只股票的新闻情绪"""
        results = {}
        for stock in stock_list:
            news = get_stock_related_news("config/stock_pool.yaml")
            results[stock['code']] = self.analyze_news(news, stock['code'], stock['name'])
        return results
