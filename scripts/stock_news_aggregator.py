import feedparser
import yaml
import os
import json
from datetime import datetime

def get_stock_related_news(config_path, max_items=10):
    """抓取与自选股高度相关的新闻"""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    keywords = set()
    for tags in config['stocks'].values():
        keywords.update(tags)
    
    all_news = []
    # 模拟抓取逻辑（实际应连接真实 RSS）
    feeds = [
        "https://www.zaobao.com/rss/finance/china",
        "https://rss.hk01.com/channel/3"
    ]
    
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                if any(kw in entry.title for kw in keywords):
                    all_news.append({
                        "title": entry.title,
                        "link": entry.link,
                        "published": entry.get('published', 'N/A')
                    })
        except:
            continue
            
    return all_news[:max_items]

if __name__ == "__main__":
    config_file = os.path.join(os.path.dirname(__file__), '../config/stock_pool.yaml')
    news = get_stock_related_news(config_file)
    print(json.dumps(news, indent=2, ensure_ascii=False))