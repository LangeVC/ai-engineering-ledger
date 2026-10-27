"""
Forgejo Collector / Adapter Module.

Provides:
- ForgejoClient: Client for Forgejo REST API (with Link-header / pagination support)
- GitSSHClient: Fallback client inspecting local clone / remote refs via git CLI
- ForgejoAdapter: High-level adapter collecting repository metadata, commits,
  releases, pull requests, and status according to PRD US-12 and US-13.
- Data structures conforming to collector schema and core event schema envelopes.
"""

from .adapter import ForgejoAdapter, ForgejoAccountConfig, ForgejoRepoConfig, CollectionResult
from .client import ForgejoApiClient, GitSSHClient
from .pagination import parse_link_header, extract_page_param

__all__ = [
    "ForgejoAdapter",
    "ForgejoAccountConfig",
    "ForgejoRepoConfig",
    "CollectionResult",
    "ForgejoApiClient",
    "GitSSHClient",
    "parse_link_header",
    "extract_page_param",
]
