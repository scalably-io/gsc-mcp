# Google Search Console MCP

Read-only MCP server for Google Search Console. Five tools cover the read surface: properties, Search Analytics (filters, auto-pagination past the 25,000-row cap, hourly data), sitemaps, URL inspection, batch inspection with throttling. We run this server in production for every SEO client.

<!-- mcp-name: io.scalably/gsc-mcp -->

## Install

Claude Code:

```bash
claude mcp add gsc -e GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json -- uvx scalably-gsc-mcp
```

Codex:

```bash
codex mcp add gsc --env GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json -- uvx scalably-gsc-mcp
```

Claude Desktop: download `gsc-mcp.mcpb` from the latest GitHub release and open it.

## Setup

1. Create a service account in Google Cloud.
2. Download its JSON key.
3. Add its email as a user on each Search Console property you want to query (Settings, Users and permissions).

No OAuth consent screen needed.

## Tools (5)

| Tool | What it does |
|---|---|
| `gsc_list_sites` | List the Search Console properties the service account can read |
| `gsc_query_search_analytics` | Query Search Analytics with filters, auto-pagination and hourly data |
| `gsc_list_sitemaps` | List sitemaps for a property or read one sitemap |
| `gsc_inspect_url` | Inspect one URL's index status |
| `gsc_batch_inspect_urls` | Inspect many URLs with client-side throttling |

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | yes | Path to a Google service-account JSON file with the Search Console read-only scope; share each property with the service account email |
| `GSC_LOG_LEVEL` | no | INFO (default) or DEBUG |
| `GSC_RETRY_BASE_SECONDS` | no | Base delay in seconds for the retry backoff on transient API errors (default 1) |

## Reply shape

Every tool returns JSON with `status` (`succeeded`, `partial`, `no_op`), `summary`, `result`, `proof`, `warnings`, `recovery`. A `partial` status with `proof.nextStartRow` means: continue from that row.

## Limits

25,000 rows per Search Analytics call; 600 URL inspections per minute and 2,000 per day per property. Both are enforced client-side.

## Verify

Each release lists the package version, the `.mcpb` sha256 and the production commit it was derived from in CHANGELOG.md. CI runs the tests and a clean install of the built wheel on every push.

## Privacy Policy

This server runs locally, on your machine, under your own credentials. It collects no personal data, contains no telemetry, stores nothing persistently, and talks only to the vendor API it wraps. No third party, including Scalably, receives your data. Contact: hello@scalably.io. Canonical copy: https://scalably.io/connector-privacy.html

## License

MIT. Copyright Scalably.
