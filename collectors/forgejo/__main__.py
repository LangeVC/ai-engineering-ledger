"""
Main CLI entrypoint for Forgejo collector adapter.
Can be executed as: python3 -m collectors.forgejo
"""

import argparse
import json
import sys

from .adapter import ForgejoAccountConfig, ForgejoAdapter, ForgejoRepoConfig


def main():
    parser = argparse.ArgumentParser(description="Forgejo Collector Adapter for AI Engineering Ledger")
    parser.add_argument("--host", required=True, help="Forgejo host from individual configuration")
    parser.add_argument("--owner", required=True, help="Repository owner")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument("--token", default=None, help="Forgejo API token (optional)")
    parser.add_argument("--check-only", action="store_true", help="Only check and print capabilities")

    args = parser.parse_args()

    account = ForgejoAccountConfig(
        alias="default",
        profile_id="forgejo_default",
        host=args.host,
        base_url=f"https://{args.host}",
        api_token=args.token
    )
    repo = ForgejoRepoConfig(
        owner=args.owner,
        name=args.repo
    )

    adapter = ForgejoAdapter(account_config=account, repo_configs=[repo])

    if args.check_only:
        caps = adapter.assess_capabilities()
        print(json.dumps(caps, indent=2))
        return 0

    result = adapter.collect()
    print(json.dumps(result.run_record, indent=2))
    return result.exit_status


if __name__ == "__main__":
    sys.exit(main())
