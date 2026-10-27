"""
CLI entrypoint for faigate collector module.
python3 -m collectors.faigate [--db-path PATH] [--client-tag TAG] [--limit N]
"""

import argparse
import json
import sys

from .adapter import FaigateAdapter, FaigateConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="AEL Faigate Collector CLI")
    parser.add_argument("--db-path", default="/opt/homebrew/var/lib/faigate/faigate.db", help="Path to faigate sqlite3 db")
    parser.add_argument("--client-tag", default=None, help="Filter by client_tag")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows")
    parser.add_argument("--json", action="store_true", help="Output JSON result")

    args = parser.parse_args()
    cfg = FaigateConfig(db_path=args.db_path)
    adapter = FaigateAdapter(cfg)
    result = adapter.collect(client_tag_filter=args.client_tag, limit=args.limit)

    if args.json:
        out = {
            "status": result.status,
            "exit_status": result.exit_status,
            "totals": result.totals,
            "account_run": result.account_run,
            "events_count": len(result.events),
            "observations": result.observations,
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"Faigate Collector: {result.status.upper()} (exit code {result.exit_status})")
        print(f"Total Requests: {result.totals.get('total_requests', 0)}")
        print(f"Tokens: prompt={result.totals.get('total_prompt_tok', 0)}, compl={result.totals.get('total_compl_tok', 0)}")
        print(f"Estimated Cost: ${result.totals.get('total_cost_usd', 0.0):.6f} (Execution Observation, not billing truth)")

    return result.exit_status


if __name__ == "__main__":
    sys.exit(main())
