# Google Search Console MCP (read-only). 5 tools covering the Search Console API read surface.
#
# References:
#   - https://developers.google.com/webmaster-tools/v1/api_reference_index
#   - https://developers.google.com/webmaster-tools/limits
#   - https://developers.google.com/search/blog/2025/04/san-hourly-data

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Iterable

import google.auth
import google.auth.credentials
import google.auth.transport.requests
import requests
from google.oauth2 import service_account
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# --- Constants ---

WEBMASTERS_BASE = os.environ.get("GSC_WEBMASTERS_BASE", "https://www.googleapis.com/webmasters/v3")
SEARCHCONSOLE_BASE = os.environ.get("GSC_SEARCHCONSOLE_BASE", "https://searchconsole.googleapis.com/v1")

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DEFAULT_TIMEOUT = 60.0

# Search Analytics limits (April 2026).
SA_PAGE_SIZE_DEFAULT = 25_000
SA_PAGE_SIZE_HARD_CAP = 25_000  # API hard cap per call.
SA_MAX_ROWS_HARD_CAP = 1_000_000  # Our own safety bound for auto-pagination.

# URL Inspection limits.
URL_INSPECT_QPM = 600  # Per site.
URL_INSPECT_QPD = 2_000  # Per site.
URL_INSPECT_SAFE_RPS = 8  # Stay well under 600/60 = 10 QPS.

# Valid Search Analytics fields, April 2026.
VALID_SA_DIMENSIONS = frozenset(
    {"date", "query", "page", "country", "device", "searchAppearance", "HOUR"}
)
VALID_SA_SEARCH_TYPES = frozenset(
    {"web", "image", "video", "news", "discover", "googleNews"}
)
VALID_SA_AGGREGATION = frozenset(
    {"auto", "byPage", "byProperty", "byNewsShowcasePanel"}
)
VALID_SA_DATA_STATE = frozenset({"final", "all", "hourly_all"})
VALID_FILTER_OPS = frozenset(
    {
        "equals",
        "contains",
        "notEquals",
        "notContains",
        "includingRegex",
        "excludingRegex",
    }
)

REDACTION_PATTERNS = (
    re.compile(r"(?i)(\"private_key\"\s*:\s*\")([^\"]+)(\")"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)(\S+)"),
)

mcp = FastMCP("gsc")
LOGGER = logging.getLogger(__name__)
def _reply(status: str, operation: str, summary: str, *, result=None, target=None, proof=None, warnings=None, recovery=None) -> str:
    """Plain JSON reply. status is one of succeeded, partial, no_op."""
    return json.dumps({"status": status, "operation": operation, "summary": summary, "target": target, "result": result, "proof": proof, "warnings": warnings or [], "recovery": recovery}, indent=2)


def _fail(operation: str, code: str, message: str, retryable: bool = False) -> None:
    hint = "Retry once after a delay." if retryable else "Correct credentials, permissions, identifiers, or parameters before retrying."
    raise RuntimeError(f"{code}: {message} {hint}")

# --- Security helpers ---


def _redact(text: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        if (m.lastindex or 0) >= 3:
            return f"{m.group(1)}***REDACTED***{m.group(3)}"
        if (m.lastindex or 0) >= 2:
            return f"{m.group(1)}***REDACTED***"
        return "***REDACTED***"

    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = pattern.sub(_replace, redacted)
    return redacted


# --- Auth ---

_credentials: google.auth.credentials.Credentials | None = None
_cred_lock = threading.Lock()


def _get_credentials() -> google.auth.credentials.Credentials:
    """Load a service-account credential with read-only webmaster scope.

    Priority:
      1. GOOGLE_APPLICATION_CREDENTIALS → service-account JSON file.
      2. Application Default Credentials.

    Must be refreshed before each call when expired.
    """
    global _credentials
    with _cred_lock:
        if _credentials is None:
            sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if sa_path and os.path.isfile(sa_path):
                _credentials = service_account.Credentials.from_service_account_file(
                    sa_path, scopes=DEFAULT_SCOPES
                )
            else:
                _credentials, _ = google.auth.default(scopes=DEFAULT_SCOPES)
        if not getattr(_credentials, "valid", False):
            _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials


def _auth_headers() -> dict[str, str]:
    test_token = os.environ.get("GSC_TEST_ACCESS_TOKEN")
    if test_token:
        return {"Authorization": f"Bearer {test_token}"}
    creds = _get_credentials()
    return {"Authorization": f"Bearer {creds.token}"}


# --- HTTP core ---


def _call(
    method: str,
    url: str,
    json_body: dict | None = None,
    params: dict | None = None,
) -> dict:
    headers = _auth_headers()
    headers["Content-Type"] = "application/json"
    resp = None
    retry_base = float(os.environ.get("GSC_RETRY_BASE_SECONDS", "1"))
    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                _fail("gsc_api_request", "transport_failure", f"GSC request failed: {_redact(str(exc))}", True)
            time.sleep(min(retry_base * (2**attempt), 5.0))
            continue
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt < 2:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(retry_base * (2**attempt), 5.0)
            time.sleep(delay)
            continue
        break
    assert resp is not None
    if resp.status_code == 204:
        return {"status": "success", "code": 204}
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if not resp.ok:
        LOGGER.error(
            "GSC API error %s %s → %s",
            method,
            url,
            _redact(json.dumps(data)),
        )
        _fail("gsc_api_request", "api_error", f"GSC API {resp.status_code}: {json.dumps(data, indent=2)}", resp.status_code == 429 or resp.status_code >= 500)
    return data


def _site_path(site_url: str) -> str:
    return requests.utils.quote(site_url, safe="")


# --- Filter normalization ---


def _normalize_filters(
    filters: list[dict[str, Any]] | None,
    group_type: str = "and",
) -> list[dict[str, Any]] | None:
    """Convert a flat filter list into a dimensionFilterGroups structure.

    Each item: {"dimension": "query", "operator": "contains", "expression": "..."}.
    Returns: [{"groupType": "and", "filters": [...]}].

    If None or empty, returns None (no filter).
    """
    if not filters:
        return None
    norm = []
    for f in filters:
        if not isinstance(f, dict):
            raise ValueError(f"filter must be a dict, got {type(f).__name__}")
        dim = f.get("dimension")
        op = f.get("operator") or f.get("op") or "equals"
        expr = f.get("expression") or f.get("value")
        if dim is None or expr is None:
            raise ValueError(
                f"filter requires 'dimension' and 'expression' (or 'value'): {f}"
            )
        if op not in VALID_FILTER_OPS:
            raise ValueError(
                f"filter operator {op!r} invalid. "
                f"Expected one of: {sorted(VALID_FILTER_OPS)}"
            )
        norm.append({"dimension": dim, "operator": op, "expression": expr})
    return [{"groupType": group_type, "filters": norm}]


def _validate_sa_params(
    dimensions: list[str] | None,
    search_type: str | None,
    aggregation_type: str | None,
    data_state: str | None,
) -> None:
    if dimensions:
        bad = [d for d in dimensions if d not in VALID_SA_DIMENSIONS]
        if bad:
            raise ValueError(
                f"Invalid dimensions {bad!r}. "
                f"Valid: {sorted(VALID_SA_DIMENSIONS)}"
            )
    if search_type and search_type not in VALID_SA_SEARCH_TYPES:
        raise ValueError(
            f"search_type {search_type!r} invalid. "
            f"Valid: {sorted(VALID_SA_SEARCH_TYPES)}"
        )
    if aggregation_type and aggregation_type not in VALID_SA_AGGREGATION:
        raise ValueError(
            f"aggregation_type {aggregation_type!r} invalid. "
            f"Valid: {sorted(VALID_SA_AGGREGATION)}"
        )
    if data_state and data_state not in VALID_SA_DATA_STATE:
        raise ValueError(
            f"data_state {data_state!r} invalid. "
            f"Valid: {sorted(VALID_SA_DATA_STATE)}"
        )
    # HOUR dimension requires hourly_all data_state.
    if dimensions and "HOUR" in dimensions and data_state != "hourly_all":
        raise ValueError(
            "HOUR dimension requires data_state='hourly_all'. "
            "See https://developers.google.com/search/blog/2025/04/san-hourly-data"
        )


# --- Tool 1: list_sites (absorbs get_site) ---


@mcp.tool(annotations=ToolAnnotations(title="List Search Console properties", readOnlyHint=True, openWorldHint=True))
def gsc_list_sites() -> str:
    """List every verified Search Console property accessible to this service account.

    Returns: {"sites": [{siteUrl, permissionLevel}, ...], "count": N}.
    permissionLevel: siteOwner | siteFullUser | siteRestrictedUser | siteUnverifiedUser.

    Call this first. URL Inspection requires siteFullUser or siteOwner;
    restricted users cannot inspect.
    """
    data = _call("GET", f"{WEBMASTERS_BASE}/sites")
    sites = data.get("siteEntry", [])
    result = {"sites": sites, "count": len(sites)}
    return _reply("succeeded" if sites else "no_op", "gsc_list_sites", f"Found {len(sites)} accessible Search Console properties.", result=result, target={"type": "gsc_property_registry"}, proof={"complete": True, "count": len(sites)})


# --- Tool 2: query_search_analytics (workhorse) ---


def _summarize_sa_response(data: dict, row_limit: int, start_row: int) -> dict[str, Any]:
    """Pull first_incomplete_date / first_incomplete_hour out to top level."""
    meta = data.get("metadata") or {}
    out: dict[str, Any] = {
        "rows": data.get("rows", []),
        "response_aggregation_type": data.get("responseAggregationType"),
        "row_count": len(data.get("rows", [])),
    }
    complete = len(out["rows"]) < row_limit
    out["complete"] = complete
    out["truncated"] = not complete
    if not complete:
        out["next_start_row"] = start_row + len(out["rows"])
    if meta:
        out["metadata"] = meta
        if "first_incomplete_date" in meta or "firstIncompleteDate" in meta:
            out["first_incomplete_date"] = meta.get(
                "first_incomplete_date", meta.get("firstIncompleteDate")
            )
        if "first_incomplete_hour" in meta or "firstIncompleteHour" in meta:
            out["first_incomplete_hour"] = meta.get(
                "first_incomplete_hour", meta.get("firstIncompleteHour")
            )
    return out


@mcp.tool(annotations=ToolAnnotations(title="Query Search Analytics", readOnlyHint=True, openWorldHint=True))
def gsc_query_search_analytics(
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str] | None = None,
    search_type: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    filter_groups: list[dict[str, Any]] | None = None,
    aggregation_type: str | None = None,
    data_state: str | None = None,
    row_limit: int = 1000,
    start_row: int = 0,
    max_rows: int | None = None,
) -> str:
    """Query Search Console traffic data - clicks, impressions, CTR, position.

    THE workhorse tool. Supports all six dimensions (incl. HOUR, April 2025),
    all six search types, full filter compose, pagination, and auto-pagination.

    Args:
      site_url: "https://example.com/" (URL-prefix) or "sc-domain:example.com" (domain).
      start_date / end_date: YYYY-MM-DD (Pacific Time). 16-month retention hard cap.
      dimensions: list of {date, query, page, country, device, searchAppearance, HOUR}.
        HOUR requires data_state='hourly_all'. searchAppearance cannot be combined with
        other dimensions in one query - query it alone and join client-side.
      search_type: web (default) | image | video | news | discover | googleNews.
      filters: flat list [{dimension, operator, expression}]. Operators:
        equals, contains, notEquals, notContains, includingRegex, excludingRegex.
      filter_groups: advanced - pass raw dimensionFilterGroups if you need OR logic.
        Mutually exclusive with `filters`.
      aggregation_type: auto (default) | byPage | byProperty | byNewsShowcasePanel.
      data_state: final (default, ~2-3d lag) | all (fresh, includes unfinalized) |
        hourly_all (~10d history with HOUR dimension).
      row_limit: per-call cap, 1–25000 (API hard cap).
      start_row: 0-based offset for single-page paging.
      max_rows: if set (up to 1M), auto-paginate until exhausted or max_rows hit.

    Response: {rows, row_count, response_aggregation_type, first_incomplete_date?,
    first_incomplete_hour?, metadata}. Use first_incomplete_date to distinguish
    'data still cooking' from 'data is final' when data_state != 'final'.

    Gotchas:
      - 16-month retention. start_date older than 16 months returns empty.
      - Anonymized queries (fewer than ~a dozen users over 2-3 months) drop from
        the 'query' dimension but count in totals - page-level data is more complete.
      - Aggregation shift: with a page filter, totals aggregate byPage (inflates
        clicks vs property-level). Set aggregation_type explicitly when comparing.
      - searchAppearance cannot co-exist with other dimensions (API 400).
    """
    if filters and filter_groups:
        raise ValueError("Pass either filters or filter_groups, not both.")
    _validate_sa_params(dimensions, search_type, aggregation_type, data_state)
    row_limit = min(max(int(row_limit), 1), SA_PAGE_SIZE_HARD_CAP)
    if max_rows is not None:
        max_rows = min(max(int(max_rows), 1), SA_MAX_ROWS_HARD_CAP)

    body: dict[str, Any] = {"startDate": start_date, "endDate": end_date}
    if dimensions:
        body["dimensions"] = dimensions
    if search_type:
        body["type"] = search_type
    if filters:
        body["dimensionFilterGroups"] = _normalize_filters(filters)
    elif filter_groups:
        body["dimensionFilterGroups"] = filter_groups
    if aggregation_type:
        body["aggregationType"] = aggregation_type
    if data_state:
        body["dataState"] = data_state

    url = f"{WEBMASTERS_BASE}/sites/{_site_path(site_url)}/searchAnalytics/query"

    # Auto-paginate when max_rows > row_limit. Otherwise single call.
    if max_rows is None or max_rows <= row_limit:
        body["rowLimit"] = row_limit
        body["startRow"] = int(start_row)
        data = _call("POST", url, body)
        result = _summarize_sa_response(data, row_limit, int(start_row))
        partial = result["truncated"] or (data_state != "final" and (result.get("first_incomplete_date") or result.get("first_incomplete_hour")))
        return _reply("partial" if partial else ("succeeded" if result["rows"] else "no_op"), "gsc_query_search_analytics", f"Search Analytics returned {result['row_count']} row(s){' with more data or incomplete dates remaining' if partial else ''}.", result=result, target={"type": "gsc_property", "siteUrl": site_url}, proof={"complete": not partial, "nextStartRow": result.get("next_start_row"), "dataState": data_state or "final"}, warnings=["Do not treat this bounded or unfinalized result as complete."] if partial else [], recovery={"nextAction": "Continue from next_start_row or rerun after data finalizes."} if partial else None)

    # Auto-paginate.
    all_rows: list[dict[str, Any]] = []
    fetched = 0
    cursor = int(start_row)
    first_incomplete_date: str | None = None
    first_incomplete_hour: str | None = None
    last_agg: str | None = None
    last_meta: dict[str, Any] | None = None
    exhausted = False
    while fetched < max_rows:
        remaining = max_rows - fetched
        page_limit = min(row_limit, remaining)
        body["rowLimit"] = page_limit
        body["startRow"] = cursor
        page = _call("POST", url, body)
        rows = page.get("rows", []) or []
        all_rows.extend(rows)
        last_agg = page.get("responseAggregationType", last_agg)
        last_meta = page.get("metadata", last_meta)
        if last_meta:
            first_incomplete_date = last_meta.get(
                "first_incomplete_date",
                last_meta.get("firstIncompleteDate", first_incomplete_date),
            )
            first_incomplete_hour = last_meta.get(
                "first_incomplete_hour",
                last_meta.get("firstIncompleteHour", first_incomplete_hour),
            )
        fetched += len(rows)
        cursor += len(rows)
        if len(rows) < page_limit:
            exhausted = True
            break

    out: dict[str, Any] = {
        "rows": all_rows,
        "response_aggregation_type": last_agg,
        "row_count": len(all_rows),
        "auto_paginated": True,
        "complete": exhausted,
        "truncated": not exhausted,
    }
    if not exhausted:
        out["next_start_row"] = cursor
    if last_meta:
        out["metadata"] = last_meta
    if first_incomplete_date is not None:
        out["first_incomplete_date"] = first_incomplete_date
    if first_incomplete_hour is not None:
        out["first_incomplete_hour"] = first_incomplete_hour
    partial = not exhausted or (data_state != "final" and (first_incomplete_date or first_incomplete_hour))
    return _reply("partial" if partial else ("succeeded" if all_rows else "no_op"), "gsc_query_search_analytics", f"Search Analytics auto-pagination returned {len(all_rows)} row(s){' with more data or incomplete dates remaining' if partial else ''}.", result=out, target={"type": "gsc_property", "siteUrl": site_url}, proof={"complete": not partial, "nextStartRow": out.get("next_start_row"), "dataState": data_state or "final"}, warnings=["Do not treat this bounded or unfinalized result as complete."] if partial else [], recovery={"nextAction": "Continue from next_start_row or rerun after data finalizes."} if partial else None)


# --- Tool 3: list_sitemaps (absorbs get_sitemap) ---


@mcp.tool(annotations=ToolAnnotations(title="List sitemaps", readOnlyHint=True, openWorldHint=True))
def gsc_list_sitemaps(
    site_url: str,
    sitemap_index: str | None = None,
    feedpath: str | None = None,
) -> str:
    """List sitemaps on a site - or fetch one sitemap by feedpath.

    Args:
      site_url: the Search Console property.
      sitemap_index: optional - if set, list only the children of a sitemap-index
        URL (e.g. "https://example.com/sitemap_index.xml").
      feedpath: optional - if set, returns the single sitemap entry at that URL
        instead of the list (absorbs the prior get_sitemap tool).

    Returns: {"sitemaps": [...], "count": N} or {"sitemap": {...}} if feedpath given.
    Each entry: {path, lastSubmitted, lastDownloaded, isPending, isSitemapsIndex,
    type, warnings, errors, contents}.
    """
    site_enc = _site_path(site_url)
    if feedpath:
        feed_enc = _site_path(feedpath)
        data = _call("GET", f"{WEBMASTERS_BASE}/sites/{site_enc}/sitemaps/{feed_enc}")
        return _reply("succeeded", "gsc_list_sitemaps", "Read one sitemap.", result={"sitemap": data}, target={"type": "gsc_sitemap", "siteUrl": site_url, "feedpath": feedpath}, proof={"complete": True})
    params = {}
    if sitemap_index:
        params["sitemapIndex"] = sitemap_index
    data = _call(
        "GET", f"{WEBMASTERS_BASE}/sites/{site_enc}/sitemaps", params=params or None
    )
    entries = data.get("sitemap", []) or []
    return _reply("succeeded" if entries else "no_op", "gsc_list_sitemaps", f"Found {len(entries)} sitemap(s).", result={"sitemaps": entries, "count": len(entries)}, target={"type": "gsc_property", "siteUrl": site_url}, proof={"complete": True})


# --- Tool 4: inspect_url (flattened) ---


def _flatten_inspection(raw: dict) -> dict[str, Any]:
    """Flatten the 4-level nested URL Inspection response for agent ergonomics."""
    result = raw.get("inspectionResult") or {}
    idx = result.get("indexStatusResult") or {}
    amp = result.get("ampResult") or None
    rich = result.get("richResultsResult") or None

    out: dict[str, Any] = {
        "verdict": idx.get("verdict"),
        "coverage_state": idx.get("coverageState"),
        "robots_txt_state": idx.get("robotsTxtState"),
        "indexing_state": idx.get("indexingState"),
        "last_crawl_time": idx.get("lastCrawlTime"),
        "google_canonical": idx.get("googleCanonical"),
        "user_canonical": idx.get("userCanonical"),
        "page_fetch_state": idx.get("pageFetchState"),
        "crawled_as": idx.get("crawledAs"),
        "referring_urls": idx.get("referringUrls", []),
        "sitemaps": idx.get("sitemap", []),
        "inspection_link": result.get("inspectionResultLink"),
    }
    if amp:
        out["amp"] = {
            "verdict": amp.get("verdict"),
            "amp_url": amp.get("ampUrl"),
            "robots_txt_state": amp.get("robotsTxtState"),
            "indexing_state": amp.get("indexingState"),
            "amp_index_status_verdict": amp.get("ampIndexStatusVerdict"),
            "issues": amp.get("issues", []),
        }
    if rich:
        out["rich_results"] = {
            "verdict": rich.get("verdict"),
            "detected_items": rich.get("detectedItems", []),
        }
    # Intentionally omit mobileUsabilityResult - deprecated Dec 2023 + always
    # returns empty. Include `_raw` for agents that need the untouched payload.
    out["_raw"] = raw
    return out


@mcp.tool(annotations=ToolAnnotations(title="Inspect a URL", readOnlyHint=True, openWorldHint=True))
def gsc_inspect_url(
    inspection_url: str,
    site_url: str,
    language_code: str = "en-US",
) -> str:
    """Inspect a URL in the Google index. Flattened response.

    Checks index status, coverage state, canonical URLs, crawl info, AMP status,
    and rich results. Does NOT trigger a live crawl - checks Google's current
    index snapshot only.

    Args:
      inspection_url: fully-qualified URL to inspect (must be under the property).
      site_url: the Search Console property - "https://example.com/" or
        "sc-domain:example.com".
      language_code: IETF BCP-47, default "en-US".

    Quota: 2,000 requests/day/site + 600 requests/minute/site. Requires the SA
    to be a Full user on the property (Restricted users get 403 here).

    Returns a flat dict with top-level verdict, coverage_state, canonical info,
    plus optional `amp` and `rich_results` sub-objects. `_raw` has the full
    untouched payload.
    """
    body: dict[str, Any] = {
        "inspectionUrl": inspection_url,
        "siteUrl": site_url,
    }
    if language_code:
        body["languageCode"] = language_code
    data = _call("POST", f"{SEARCHCONSOLE_BASE}/urlInspection/index:inspect", body)
    result = _flatten_inspection(data)
    return _reply("succeeded", "gsc_inspect_url", f"Inspected {inspection_url} in Google's current index snapshot.", result=result, target={"type": "url", "url": inspection_url, "siteUrl": site_url}, proof={"liveCrawl": False, "verdict": result.get("verdict")})


# --- Tool 5: batch_inspect_urls (rate-limited bulk) ---


@mcp.tool(annotations=ToolAnnotations(title="Inspect URLs in batch", readOnlyHint=True, openWorldHint=True))
def gsc_batch_inspect_urls(
    urls: list[str],
    site_url: str,
    language_code: str = "en-US",
    requests_per_second: float = URL_INSPECT_SAFE_RPS,
    continue_on_error: bool = True,
) -> str:
    """Inspect many URLs under a single Search Console property. Rate-limited.

    Respects Google's 600 QPM / 2,000 QPD per-site limits. Default pace of
    8 QPS stays well under 600/minute with headroom. For large jobs (>2000
    URLs), split across days or across multiple verified properties (e.g.
    per-subdomain).

    Args:
      urls: list of fully-qualified URLs under site_url. No dedup.
      site_url: the Search Console property.
      language_code: BCP-47, default "en-US".
      requests_per_second: pace. Max ~10 (600 QPM). Caller can lower on
        429 pressure.
      continue_on_error: if True, collect per-URL errors instead of aborting.

    Returns: {
      "results": [{"url": str, "inspection": {...flattened...}} | {"url": str, "error": str}],
      "count": total, "errors": N, "skipped": 0
    }

    Quota warning: This does NOT replace the Search Analytics API for bulk query
    analysis. If the intent is "which pages have most traffic", use
    gsc_query_search_analytics instead - 40,000 QPM per project vs 600/site here.
    """
    if not urls:
        return _reply("no_op", "gsc_batch_inspect_urls", "No URLs were provided.", result={"results": [], "count": 0, "errors": 0}, target={"type": "gsc_property", "siteUrl": site_url}, proof={"complete": True})
    if len(urls) > URL_INSPECT_QPD:
        LOGGER.warning(
            "batch_inspect_urls len=%d exceeds per-site daily quota %d; "
            "Google will 429 after the first %d.",
            len(urls),
            URL_INSPECT_QPD,
            URL_INSPECT_QPD,
        )
    rps = max(0.1, min(float(requests_per_second), URL_INSPECT_SAFE_RPS))
    interval = 1.0 / rps

    results: list[dict[str, Any]] = []
    errors = 0
    last = 0.0
    for url in urls:
        wait = (last + interval) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            body = {
                "inspectionUrl": url,
                "siteUrl": site_url,
                "languageCode": language_code,
            }
            data = _call(
                "POST",
                f"{SEARCHCONSOLE_BASE}/urlInspection/index:inspect",
                body,
            )
            results.append({"url": url, "inspection": _flatten_inspection(data)})
        except Exception as exc:
            errors += 1
            msg = str(exc)
            results.append({"url": url, "error": msg[:500]})
            if not continue_on_error:
                break
    complete = errors == 0 and len(results) == len(urls)
    return _reply("succeeded" if complete else "partial", "gsc_batch_inspect_urls", f"Inspected {len(results)} of {len(urls)} URL(s) with {errors} error(s).", result={"results": results, "count": len(results), "errors": errors, "requested": len(urls)}, target={"type": "gsc_property", "siteUrl": site_url}, proof={"complete": complete, "processed": len(results), "requested": len(urls)}, warnings=["Some URL inspections failed or processing stopped early; do not report the batch as complete."] if not complete else [], recovery={"nextAction": "Retry only failed or unprocessed URLs after checking quota and permissions."} if not complete else None)


# --- Entry ---


def _configure_logging() -> None:
    level = os.environ.get("GSC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s gsc-mcp %(message)s",
    )


def main() -> None:
    _configure_logging()
    try:
        _get_credentials()
    except Exception as exc:
        LOGGER.error("Credential load failed at startup: %s", exc)
    mcp.run()


if __name__ == "__main__":
    main()
