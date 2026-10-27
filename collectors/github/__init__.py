"""GitHub Collector Package (AEL-ADAPTER-GITHUB).
Implements native GitHub API collector conforming to AEL v0.1 schema specifications.
"""

from .collector import (
    GitHubCollector,
    GitHubConfig,
    GitHubClient,
    GitHubApiError,
    parse_link_header,
    compute_idempotency_key,
    generate_ulid,
)

__all__ = [
    "GitHubCollector",
    "GitHubConfig",
    "GitHubClient",
    "GitHubApiError",
    "parse_link_header",
    "compute_idempotency_key",
    "generate_ulid",
]
