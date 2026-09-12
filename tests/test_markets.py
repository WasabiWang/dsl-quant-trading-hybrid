"""
Test Market Module
"""

import pytest
from markets import AShareMarket, HKShareMarket, USShareMarket, get_market, detect_market


class TestMarketDetection:
    """Test market detection"""
    
    def test_detect_a_share(self):
        """Test A-share detection"""
        assert detect_market("600519.SH") == "a"
        assert detect_market("000001.SZ") == "a"
        assert detect_market("600519") == "a"
    
    def test_detect_hk_share(self):
        """Test HK-share detection"""
        assert detect_market("0700.HK") == "hk"
        assert detect_market("0700") == "hk"
    
    def test_detect_us_share(self):
        """Test US-share detection"""
        assert detect_market("AAPL") == "us"
        assert detect_market("TSLA") == "us"


class TestAShareMarket:
    """Test A-Share Market"""
    
    def test_init(self):
        """Test initialization"""
        market = AShareMarket()
        assert market is not None
    
    def test_normalize_symbol(self):
        """Test symbol normalization"""
        market = AShareMarket()
        assert market.normalize_symbol("600519") == "600519.SH"
        assert market.normalize_symbol("000001") == "000001.SZ"
    
    def test_validate_symbol(self):
        """Test symbol validation"""
        market = AShareMarket()
        assert market.validate_symbol("600519.SH") == True
        assert market.validate_symbol("600519") == True
        assert market.validate_symbol("") == False


class TestHKShareMarket:
    """Test HK-Share Market"""
    
    def test_normalize_symbol(self):
        """Test symbol normalization"""
        market = HKShareMarket()
        # 港股代码应该补齐到5位
        assert market.normalize_symbol("0700") == "00700.HK"
        assert market.normalize_symbol("700") == "00700.HK"
        assert market.normalize_symbol("00700") == "00700.HK"


class TestUSShareMarket:
    """Test US-Share Market"""
    
    def test_normalize_symbol(self):
        """Test symbol normalization"""
        market = USShareMarket()
        assert market.normalize_symbol("AAPL") == "AAPL"
        assert market.normalize_symbol("tsla") == "TSLA"
