# Changelog

## 1.0.1

- The console script is now `scalably-gsc-mcp`, so `uvx scalably-gsc-mcp` runs as documented (1.0.0 only worked as `uvx --from scalably-gsc-mcp gsc-mcp`).
- README: enable the Search Console API step, honest quota wording, the test-only environment variables documented.
- Tests: tool annotations are asserted; the public-pattern test walks tracked files only and exempts the release digest.
- Dependabot ignores `mcp` major versions (2.x removed `mcp.server.fastmcp`).

## 1.0.0

- First public release. Derived from `container/tools/gsc-mcp/server.py` at `ef174fc3` (2026-08-31) in the private ScalablyAI repository. Changes from production: the private tool-outcome envelope is replaced by a plain JSON reply, tool annotations added, no functional change.
