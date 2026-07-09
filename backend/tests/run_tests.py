import sys
import os

# Align python path to backend/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_strategy import (
    test_strategy_registration,
    test_indicator_calculations,
    test_heikin_ashi_transformation
)

if __name__ == "__main__":
    print("Running system validation tests...")
    try:
        test_strategy_registration()
        print("[PASS] test_strategy_registration passed.")
        
        test_indicator_calculations()
        print("[PASS] test_indicator_calculations passed.")
        
        test_heikin_ashi_transformation()
        print("[PASS] test_heikin_ashi_transformation passed.")
        
        print("\nAll validation tests passed successfully!")
    except AssertionError as e:
        print(f"Test failure occurred: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        sys.exit(1)
