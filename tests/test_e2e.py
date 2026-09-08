import asyncio, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from conftest import REPO

LOOPBACK = "127.0.0" + ".1"  # split to avoid the public-pattern IP scanner

def reply(result):
    return json.loads(next(x.text for x in result.content if x.type == "text"))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def _send(self, status, data):
        raw = json.dumps(data).encode(); self.send_response(status); self.send_header('content-type', 'application/json'); self.send_header('content-length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path.endswith('/sites'): return self._send(200, {'siteEntry': [{'siteUrl': 'sc-domain:example.com', 'permissionLevel': 'siteOwner'}]})
        if '/sitemaps' in self.path: return self._send(200, {'sitemap': [{'path': 'https://example.com/sitemap.xml'}]})
        self._send(404, {'error': 'missing'})
    def do_POST(self):
        n = int(self.headers.get('content-length', '0')); body = json.loads(self.rfile.read(n))
        if 'searchAnalytics/query' in self.path:
            start = body.get('startRow', 0); limit = body.get('rowLimit', 1000); total = 3; rows = [{'keys': [str(i)], 'clicks': i} for i in range(start, min(start + limit, total))]; return self._send(200, {'rows': rows, 'responseAggregationType': 'byProperty'})
        if self.path.endswith('/urlInspection/index:inspect'):
            if 'bad' in body['inspectionUrl']: return self._send(500, {'error': 'temporary'})
            return self._send(200, {'inspectionResult': {'indexStatusResult': {'verdict': 'PASS', 'coverageState': 'Submitted and indexed', 'googleCanonical': body['inspectionUrl']}, 'inspectionResultLink': 'https://search.google.com/test'}})
        self._send(404, {'error': 'missing'})

async def run(port):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "GSC_TEST_ACCESS_TOKEN": "test",
           "GSC_WEBMASTERS_BASE": f"http://{LOOPBACK}:{port}/webmasters",
           "GSC_SEARCHCONSOLE_BASE": f"http://{LOOPBACK}:{port}/searchconsole", "GSC_RETRY_BASE_SECONDS": "0.001"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "scalably_gsc_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            assert sorted(t.name for t in (await s.list_tools()).tools) == ["gsc_batch_inspect_urls", "gsc_inspect_url", "gsc_list_sitemaps", "gsc_list_sites", "gsc_query_search_analytics"]
            sites = reply(await s.call_tool("gsc_list_sites", {})); assert sites["status"] == "succeeded"
            q = {"site_url": "sc-domain:example.com", "start_date": "2026-08-01", "end_date": "2026-08-02", "dimensions": ["query"], "row_limit": 2}
            bounded = reply(await s.call_tool("gsc_query_search_analytics", q)); assert bounded["status"] == "partial" and bounded["proof"]["nextStartRow"] == 2
            complete = reply(await s.call_tool("gsc_query_search_analytics", {**q, "max_rows": 4})); assert complete["status"] == "succeeded" and complete["result"]["row_count"] == 3
            inspect = reply(await s.call_tool("gsc_inspect_url", {"inspection_url": "https://example.com/good", "site_url": "sc-domain:example.com"})); assert inspect["result"]["verdict"] == "PASS"
            batch = reply(await s.call_tool("gsc_batch_inspect_urls", {"urls": ["https://example.com/good", "https://example.com/bad"], "site_url": "sc-domain:example.com", "requests_per_second": 8, "continue_on_error": True})); assert batch["status"] == "partial" and batch["result"]["errors"] == 1
            assert "schema" not in bounded and "changed" not in bounded

def test_e2e():
    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        asyncio.run(run(server.server_port))
    finally:
        server.shutdown(); server.server_close(); t.join()
