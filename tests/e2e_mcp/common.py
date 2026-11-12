"""MCP App（scripts/mcp_server.py）のテスト共通ヘルパー（依存: playwright のみ）.

ホスト役は、このファイルの `HOST_PAGE`（テスト用の最小ホスト）。MCP Apps の仕様
（2026-01-26）どおりに、sandbox 付き iframe へ配布 HTML を流し込み、postMessage の
JSON-RPC で応対する。ビューアからの `tools/call` は、同じプロセスに読み込んだ
`mcp_server.Server` に渡す＝ビューア → ホスト → サーバー → 実ファイル（一時フォルダ）の
往復をそのまま通す。実データには一切書き込まない。
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]   # リポジトリルート
sys.path.append(str(ROOT / "scripts"))               # 末尾に足す＝隣の common.py を覆わない
import mcp_server  # noqa: E402

_failures = []


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        _failures.append(msg)


def finish(errors=None):
    """JSエラーとFAILを集計して終了コードを返す"""
    errors = [e for e in (errors or []) if e]
    if errors:
        print("=== JSエラー ===", errors)
    print("=== RESULT ===", "ALL PASS" if not _failures and not errors
          else f"{len(_failures)} FAIL + {len(errors)} error")
    sys.exit(1 if (_failures or errors) else 0)


def rpc(server, method, params=None, mid=1):
    """サーバーに 1 通送って result を返す（error なら例外）."""
    reply = server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                           "params": params or {}})
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


def tool(server, name, args=None):
    return rpc(server, "tools/call", {"name": name, "arguments": args or {}})


def text_of(result):
    return "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")


# MCP Apps の既定の CSP（`ui.csp` を宣言しない資源にホストが掛けるもの）。
# srcdoc では 'self' が意味を持たないので外してある＝仕様の既定より厳しい側。
DEFAULT_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src data:; media-src data:; connect-src 'none'; frame-src 'none'; "
               "object-src 'none'; base-uri 'none'")

# 参照実装（ext-apps の basic-host）と同じ sandbox。allow-modals も allow-popups も無い
# ＝ alert は出ず、confirm は false、新しいタブは開けない。
SANDBOX = "allow-scripts allow-same-origin allow-forms"
# いちばん厳しい形（オリジンなし）。localStorage も history の書き換えも拒まれる。
SANDBOX_OPAQUE = "allow-scripts allow-forms"

HOST_PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:0">
<iframe id="app" style="display:block;width:100%;height:120px;border:0"></iframe>
<script>
(() => {
  const f = document.getElementById('app');
  window.__log = [];                      // ビューアから届いたメッセージ（届いた順）
  window.__ctx = {displayMode:'inline', availableDisplayModes:['inline','fullscreen'],
                  containerDimensions:{maxHeight:900}, platform:'web'};
  const send = m => f.contentWindow.postMessage(m, '*');
  window.__send = send;
  window.addEventListener('message', async ev => {
    if (ev.source !== f.contentWindow) return;
    const m = ev.data;
    if (!m || m.jsonrpc !== '2.0') return;
    window.__log.push(m);
    const ok = r => send({jsonrpc:'2.0', id:m.id, result:r});
    if (m.method === 'ui/initialize') {
      ok({protocolVersion:'2026-01-26', hostInfo:{name:'test-host', version:'0'},
          hostCapabilities:{openLinks:{}, serverTools:{}, logging:{}}, hostContext:window.__ctx});
    } else if (m.method === 'ui/notifications/initialized') {
      send({jsonrpc:'2.0', method:'ui/notifications/tool-input', params:{arguments:window.__toolArgs}});
      send({jsonrpc:'2.0', method:'ui/notifications/tool-result', params:window.__toolResult});
    } else if (m.method === 'tools/call') {
      const r = JSON.parse(await window.mcpRpc(JSON.stringify(
        {jsonrpc:'2.0', id:1, method:'tools/call', params:m.params})));
      if (r.error) send({jsonrpc:'2.0', id:m.id, error:r.error}); else ok(r.result);
    } else if (m.method === 'ui/notifications/size-changed') {
      if (m.params && m.params.height != null) f.style.height = m.params.height + 'px';
    } else if (m.method === 'ui/request-display-mode') {
      ok({mode:m.params.mode});
      send({jsonrpc:'2.0', method:'ui/notifications/host-context-changed',
            params:{displayMode:m.params.mode}});
    } else if (m.method === 'ui/open-link') {
      ok({});
    } else if (m.method && m.id !== undefined) {
      send({jsonrpc:'2.0', id:m.id, error:{code:-32601, message:'Method not found'}});
    }
  });
})();
</script>"""


def with_csp(html, csp=DEFAULT_CSP):
    """配布 HTML の <head> 直後に CSP を差す（ホストの sandbox proxy が掛けるものの代わり）."""
    meta = '<meta http-equiv="Content-Security-Policy" content="%s">' % csp
    out, n = re.subn(r"(<head[^>]*>)", lambda m: m.group(1) + meta, html, count=1)
    assert n == 1, "<head> が見つからない"
    return out


def mount(pg, server, args=None, sandbox=SANDBOX, result=None):
    """ホスト役のページを作り、wbs_open の結果でビューアを表示する。iframe の Frame を返す.

    本物のホストと同じ順で進める：tools/list → `_meta.ui.resourceUri` → resources/read →
    tools/call(wbs_open) → iframe に流し込み → 初期化の後に tool-input / tool-result。
    """
    args = args or {}
    opener = next(t for t in rpc(server, "tools/list")["tools"] if t["name"] == "wbs_open")
    uri = opener["_meta"]["ui"]["resourceUri"]
    html = rpc(server, "resources/read", {"uri": uri})["contents"][0]["text"]
    if result is None:
        result = tool(server, "wbs_open", args)
    pg.set_content(HOST_PAGE)
    try:
        pg.expose_function("mcpRpc", lambda s: json.dumps(server.handle(json.loads(s)),
                                                          ensure_ascii=False))
    except Exception:                      # 同じページで 2 回目以降（公開済み）
        pass
    pg.evaluate("""a => { window.__toolArgs = a.args; window.__toolResult = a.result;
        const f = document.getElementById('app');
        f.setAttribute('sandbox', a.sandbox); f.srcdoc = a.html; }""",
                {"args": args, "result": result, "sandbox": sandbox, "html": with_csp(html)})
    pg.wait_for_timeout(400)
    return pg.frames[-1]


def sent(pg, method):
    """ビューアがホストへ送ったメッセージのうち、method が一致するものの params."""
    return pg.evaluate("m => window.__log.filter(x => x.method === m).map(x => x.params)", method)
