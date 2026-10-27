"""
Pagination helpers for Forgejo REST API.
Standard RFC 5988 Link header parser and query parameter pagination handler.
"""

import re
from typing import Dict, List, Optional, Tuple


def parse_link_header(link_header: Optional[str]) -> Dict[str, str]:
    """
    Parses standard RFC 5988 Link headers into a dict mapping relation -> url.
    Example header:
      '<https://git.example.com/api/v1/repos/o/r/commits?page=2>; rel="next", <...>; rel="last"'
    """
    if not link_header:
        return {}
    links: Dict[str, str] = {}
    parts = link_header.split(",")
    for part in parts:
        section = part.strip().split(";")
        if len(section) < 2:
            continue
        url_match = re.search(r"<([^>]+)>", section[0].strip())
        rel_match = re.search(r'rel=["\']?([^"\']+)["\']?', section[1].strip())
        if url_match and rel_match:
            links[rel_match.group(1)] = url_match.group(1)
    return links


def extract_page_param(url: str) -> Optional[int]:
    """Extracts the page integer parameter from a URL if present."""
    match = re.search(r"[?&]page=([0-9]+)", url)
    if match:
        return int(match.group(1))
    return None
