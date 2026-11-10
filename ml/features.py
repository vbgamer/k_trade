"""The feature code lives in backend/app/ml/features.py so training and live trading share one copy."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
from app.ml.features import *  # noqa: E402,F401,F403
from app.ml.features import FEATURE_NAMES, STRIKE_STEP, atm_strike, compute_features  # noqa: E402,F401
