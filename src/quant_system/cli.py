from __future__ import annotations
import argparse,json
from datetime import date
from .service import QuantService
from .providers import provider_from_env

def main():
    p=argparse.ArgumentParser(description="运行A股波段系统日终研究流水线（不下单）")
    p.add_argument("--as-of",type=date.fromisoformat);p.add_argument("--enforce-freshness",action="store_true")
    args=p.parse_args();print(json.dumps(QuantService(provider=provider_from_env()).run_eod(args.as_of,enforce_freshness=args.enforce_freshness),ensure_ascii=False,indent=2))

if __name__=="__main__":main()
