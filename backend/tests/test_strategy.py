# Native validation tests
from app.engine.registry import StrategyRegistry
from app.strategies.kaushalcustomWMASMACall1.strategy import KaushalCustomWMASMACall1

def test_strategy_registration():
    """
    Verifies that the strategy dynamically registers itself in the StrategyRegistry.
    """
    strategy_class = StrategyRegistry.get_strategy("kaushalcustomWMASMACall1")
    assert strategy_class is not None
    assert strategy_class == KaushalCustomWMASMACall1

def test_indicator_calculations():
    """
    Tests SMA and WMA numerical functions of the options strategy.
    """
    strategy = KaushalCustomWMASMACall1({"quantity": 50})
    
    # Test SMA
    vals = [10.0, 12.0, 14.0]
    smas = strategy._sma(vals, 2)
    assert smas[0] is None
    assert smas[1] == 11.0
    assert smas[2] == 13.0
    
    # Test WMA
    wmas = strategy._wma(vals, 2)
    # weights: 1, 2. denom = 3.
    # index 1: (10*1 + 12*2) / 3 = 34 / 3 = 11.33
    # index 2: (12*1 + 14*2) / 3 = 40 / 3 = 13.33
    assert wmas[0] is None
    assert round(wmas[1], 2) == 11.33
    assert round(wmas[2], 2) == 13.33

def test_heikin_ashi_transformation():
    """
    Verifies that Heikin-Ashi formulas correctly adjust prices.
    """
    strategy = KaushalCustomWMASMACall1({"quantity": 50})
    o = [100.0, 102.0]
    h = [105.0, 106.0]
    l = [95.0, 98.0]
    cl = [101.0, 103.0]
    
    ha_o, ha_h, ha_l, ha_c = strategy._compute_heikin_ashi(o, h, l, cl)
    
    # First candle calculations
    # ha_c[0] = (100 + 105 + 95 + 101) / 4 = 100.25
    # ha_o[0] = (100 + 101) / 2 = 100.5
    assert ha_c[0] == 100.25
    assert ha_o[0] == 100.5
    assert ha_h[0] == 105.0
    assert ha_l[0] == 95.0
