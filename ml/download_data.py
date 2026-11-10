"""Download the historical data used to train the trading model.

Sources (both public):
  * NIFTY 50 spot, 1-minute, 2017 onwards
    https://github.com/technovusin/nifty50-historical-data
  * NIFTY option chains, 1-minute OHLC + OI, 2021 onwards (licence CC BY-NC 4.0: personal / non-commercial use)
    https://huggingface.co/datasets/thetrademarkk/india-index-options-1m

Files are saved under ml/data/raw and are skipped if already present, so the script is safe to re-run
(it picks up new months / expiries).

    ml/.venv/Scripts/python.exe ml/download_data.py
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")

GH_REPO = "technovusin/nifty50-historical-data"
HF_REPO = "thetrademarkk/india-index-options-1m"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "k-trade-ml-downloader"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _download(url, dest, expected_size=None):
    if os.path.exists(dest) and (expected_size is None or os.path.getsize(dest) == expected_size):
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "k-trade-ml-downloader"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)
    return True


def spot_jobs():
    tree = _get_json(f"https://api.github.com/repos/{GH_REPO}/git/trees/HEAD?recursive=1")["tree"]
    for t in tree:
        p = t["path"]
        if p.startswith("1min/") and p.endswith(".csv"):
            url = f"https://raw.githubusercontent.com/{GH_REPO}/main/{p}"
            yield url, os.path.join(RAW, "spot", os.path.basename(p)), t.get("size")


def option_jobs():
    files = _get_json(f"https://huggingface.co/api/datasets/{HF_REPO}/tree/main/options/NIFTY")
    for x in files:
        p = x["path"]
        if p.endswith(".parquet"):
            url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{p}"
            yield url, os.path.join(RAW, "options", os.path.basename(p)), x.get("size")
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/index/NIFTY.parquet"
    yield url, os.path.join(RAW, "hf_index_NIFTY.parquet"), None


def main():
    jobs = list(spot_jobs()) + list(option_jobs())
    print(f"{len(jobs)} files to check")
    new = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for done in ex.map(lambda j: _download(*j), jobs):
            new += int(done)
    print(f"Downloaded {new} new files into {RAW}")


if __name__ == "__main__":
    sys.exit(main())
