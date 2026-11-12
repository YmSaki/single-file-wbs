#!/usr/bin/env python3
"""WBS Viewer を MCP App として配る MCP サーバー（依存ゼロ・標準ライブラリだけ）.

Claude などの MCP Apps 対応ホストの会話の中に、`wbs_viewer.html` をそのまま
表示する。製品本体 `wbs_viewer.html` は 1 バイトも変えない。配る直前に、その
複製の `</body>` の手前へ橋渡しのスクリプト（`scripts/mcp_app/bridge.js`）を
1 本足すだけ（`scripts/build_demo.py` と同じ作法）。

橋渡しは、ビューアが元から持っている「ファイルを開く／更新／編集（自動保存）」
の経路をそのまま使う。File System Access API のファイルハンドルの代わりに、
このサーバーのツールを呼ぶ仮想ハンドルを渡すだけなので、表示も編集も保存も
ビューア側のコードは同じものが動く。

ツール:
    wbs_open   モデルから呼ぶ。JSON を WBS Viewer で表示する（要約を文字で返す）
    wbs_read   ビューア専用（モデルには見せない）。JSON の中身を返す
    wbs_save   ビューア専用。JSON を書き戻す（`--read-only` では出さない）

保存は「読んだ時の版」と今のファイルが違えば断る（AI や別の道具が先に
書き換えていたら上書きしない）。書き込みは一時ファイル経由の置換。

使い方:
    python3 scripts/mcp_server.py wbs.json              # stdio（Claude Desktop など）。この 1 枚だけ
    python3 scripts/mcp_server.py ~/work --read-only    # フォルダの中の .json を、見るだけで配る
    python3 scripts/mcp_server.py wbs.json --http 8765  # HTTP（http://127.0.0.1:8765/mcp）
    python3 scripts/mcp_server.py --print-html > app.html   # 配る HTML を確かめる
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEWER = ROOT / "wbs_viewer.html"
BRIDGE = pathlib.Path(__file__).resolve().parent / "mcp_app" / "bridge.js"

SERVER_NAME = "single-file-wbs"
SERVER_VERSION = "0.1.0"
# 新しい順。相手が求めた版を知っていればそれで、知らなければ先頭で答える。
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

UI_URI = "ui://single-file-wbs/viewer.html"
UI_MIME = "text/html;profile=mcp-app"

# 追記ブロックの目印。この間を取り除くと `wbs_viewer.html` とバイト一致する
# （tests/e2e_mcp/test_server.py が確かめる＝「本体を変えていない」の機械検証）。
BEGIN = "<!-- BEGIN mcp_server.py -->"
END = "<!-- END mcp_server.py -->"

MAX_BYTES = 32 * 1024 * 1024          # 読み書きする JSON と HTTP 本文の上限
MAX_CANDIDATES = 20                   # 候補として挙げるファイルの上限
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INSTRUCTIONS = (
    "WBS Viewer (single-file-wbs): shows a plan (WBS / Gantt) and an issue list "
    "kept in one JSON file. Call wbs_open to display it to the user. The viewer "
    "is live: it re-reads the file when the user presses Refresh, and writes "
    "edits back to the same file."
)


class ToolError(Exception):
    """ツールの失敗（プロトコルのエラーではなく、結果として返す）."""


def log(*parts):
    """stdout は MCP の通信路なので、人向けの出力はすべて stderr に出す."""
    print("[wbs-mcp]", *parts, file=sys.stderr, flush=True)


def version_of(raw):
    """ファイルの版＝中身のハッシュ（更新時刻は同期ツールで動くので使わない）."""
    return hashlib.sha256(raw).hexdigest()[:16]


class Workspace:
    """開いてよい JSON の範囲（1 つのフォルダ）と、その読み書き."""

    def __init__(self, path, read_only=False):
        target = pathlib.Path(path).expanduser().resolve()
        self.single = False                        # True＝ファイル 1 つだけを配る
        if target.is_dir():
            self.root, self.default = target, None
            if (target / "wbs.json").is_file():
                self.default = "wbs.json"          # 慣用名があればそれを既定にする
        else:
            if target.suffix.lower() != ".json":
                raise SystemExit(f"{target}: JSON ファイルかフォルダを指定する。")
            if not target.parent.is_dir():
                raise SystemExit(f"{target.parent}: フォルダが無い。")
            # ファイルを指された時は、隣の JSON（設定ファイルなど）には触れさせない。
            self.root, self.default, self.single = target.parent, target.name, True
        self.read_only = bool(read_only)
        self.lock = threading.Lock()               # 保存は 1 本ずつ（HTTP は複数スレッド）

    def resolve(self, name):
        """`file` 引数を実パスにする。フォルダの外・JSON 以外は断る."""
        if name is None or name == "":
            name = self.default
            if name is None:
                raise ToolError(self._no_default())
        if not isinstance(name, str):
            raise ToolError("file must be a string (path relative to the served folder).")
        rel = pathlib.PurePosixPath(name.replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ToolError("file must be a relative path inside the served folder.")
        if rel.suffix.lower() != ".json":
            raise ToolError("Only .json files can be opened.")
        if self.single and rel.as_posix() != self.default:
            raise ToolError(f"Only {self.default} can be opened "
                            "(the server was started for this single file).")
        path = (self.root / pathlib.Path(*rel.parts)).resolve()
        # Why not 文字列の前方一致: symlink でフォルダの外へ出る経路を、解決後の実パスで断つ。
        if self.root != path and self.root not in path.parents:
            raise ToolError("file must be a relative path inside the served folder.")
        return path, rel.as_posix()

    def _no_default(self):
        names = self.candidates()
        if not names:
            return "No WBS JSON file found in the served folder."
        return ("No default file. Pass `file` as one of: " + ", ".join(names))

    def candidates(self):
        """フォルダ直下と 1 段下から、WBS らしい JSON を挙げる."""
        if self.single:
            return [self.default]
        found = []
        for base in [self.root] + sorted(
                d for d in self.root.iterdir()
                if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")):
            try:
                files = sorted(f for f in base.iterdir()
                               if f.is_file() and f.suffix.lower() == ".json")
            except OSError:
                continue
            for f in files:
                if len(found) >= MAX_CANDIDATES:
                    return found
                if f.stat().st_size > MAX_BYTES:
                    continue
                try:
                    doc = json.loads(f.read_bytes().decode("utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(doc, dict) and (isinstance(doc.get("projects"), list)
                                              or isinstance(doc.get("tasks"), list)):
                    found.append(f.relative_to(self.root).as_posix())
        return found

    def read(self, name):
        path, rel = self.resolve(name)
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ToolError(f"{rel}: file is too large.")
            raw = path.read_bytes()
        except FileNotFoundError:
            raise ToolError(f"{rel}: file not found.") from None
        except OSError as e:
            raise ToolError(f"{rel}: cannot read ({e.strerror}).") from None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ToolError(f"{rel}: not UTF-8 text.") from None
        return rel, text, version_of(raw)

    def save(self, name, text, base_version):
        if self.read_only:
            raise ToolError("This server is read-only. / 読み取り専用のため保存できません。")
        path, rel = self.resolve(name)
        if not isinstance(text, str):
            raise ToolError("text must be a string (the whole JSON document).")
        raw = text.encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ToolError(f"{rel}: document is too large.")
        try:
            json.loads(text)
        except ValueError as e:
            # 壊れた JSON は書かない（ビューアは JSON.stringify の結果しか送らないので、ここに来るのは誤用）
            raise ToolError(f"{rel}: text is not valid JSON ({e}). Nothing was written.") from None
        with self.lock:
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                raise ToolError(f"{rel}: file not found.") from None
            except OSError as e:
                raise ToolError(f"{rel}: cannot read ({e.strerror}).") from None
            if not isinstance(base_version, str) or version_of(current) != base_version:
                raise ToolError(
                    "ファイルがこの画面の外で変更されています。上書きしていません。"
                    "「更新」で読み直してください。 / "
                    "The file was changed outside this view. Nothing was written. "
                    "Press Refresh to reload.")
            # 一時ファイルに書き切ってから置き換える＝途中で落ちても空や半端なファイルを残さない
            fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                       dir=str(path.parent))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(tmp, path.stat().st_mode & 0o777)
                except OSError:
                    pass
                os.replace(tmp, path)
            except OSError as e:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise ToolError(f"{rel}: cannot write ({e.strerror}).") from None
        return rel, version_of(raw)


# ---------------------------------------------------------------- 要約（モデルに返す文字）

def _is_date(value):
    return isinstance(value, str) and bool(DATE_RE.match(value))


def _leaves(tasks):
    """計画の葉（`children` を持たないノード）を深さを問わず集める."""
    out = []
    if not isinstance(tasks, list):
        return out
    for node in tasks:
        if not isinstance(node, dict):
            continue
        kids = node.get("children")
        if isinstance(kids, list) and kids:
            out.extend(_leaves(kids))
        else:
            out.append(node)
    return out


def _projects(doc):
    if not isinstance(doc, dict):
        return []
    projects = doc.get("projects")
    if isinstance(projects, list):
        return [p for p in projects if isinstance(p, dict)]
    if isinstance(doc.get("tasks"), list):             # 計画だけの旧形式 { project, tasks }
        return [{"name": doc.get("project") or doc.get("name") or "", "tasks": doc["tasks"]}]
    return []


def summarize(rel, text, today):
    """開いたファイルの要約。状態の数え方は AGENTS.md の導出規則に合わせる."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        return (f"Opened {rel} in WBS Viewer, but it is not valid JSON ({e}). "
                "The viewer shows the parse error.")
    lines = [f"Opened {rel} in WBS Viewer (today: {today})."]
    if isinstance(doc, dict) and isinstance(doc.get("name"), str) and doc["name"]:
        lines.append(f"Book: {doc['name']}")
    projects = _projects(doc)
    if not projects:
        lines.append("No projects in this file.")
    for proj in projects:
        parts = []
        leaves = _leaves(proj.get("tasks"))
        if leaves:
            done = started = overdue = 0
            for leaf in leaves:
                actual = leaf.get("actual") if isinstance(leaf.get("actual"), dict) else {}
                plan = leaf.get("plan") if isinstance(leaf.get("plan"), dict) else {}
                if _is_date(actual.get("end")):
                    done += 1
                    continue
                if _is_date(actual.get("start")):
                    started += 1
                if _is_date(plan.get("end")) and today > plan["end"]:
                    overdue += 1                   # 本日 > 予定終了 かつ 未完了
            parts.append(f"plan: {len(leaves)} leaf tasks ({len(leaves) - done - started} "
                         f"not started, {started} in progress, {done} done; "
                         f"{overdue} past planned end)")
        issues = proj.get("issues")
        if isinstance(issues, list) and issues:
            rows = [i for i in issues if isinstance(i, dict)]
            closed = sum(1 for i in rows if isinstance(i.get("closed"), dict))
            late = sum(1 for i in rows if not isinstance(i.get("closed"), dict)
                       and _is_date(i.get("due")) and today > i["due"])
            parts.append(f"issues: {len(rows)} ({len(rows) - closed} open, {closed} closed; "
                         f"{late} past due)")
        name = proj.get("name") if isinstance(proj.get("name"), str) else ""
        lines.append(f"- {name or '(no name)'}: " + ("; ".join(parts) or "empty"))
    lines.append("The user now sees the interactive viewer (Gantt, progress, dependency "
                 "and issue views). Do not repeat the whole table in text.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 配る HTML

_html_cache = {"key": None, "html": None}


def _read_text(path):
    """改行を変換せずに読む（本体とのバイト一致を壊さないため）."""
    return path.read_bytes().decode("utf-8")


def build_app_html():
    """ビューアの `</body>` の直前に、橋渡しのスクリプトを差し込んだ HTML を返す."""
    key = (VIEWER.stat().st_mtime_ns, BRIDGE.stat().st_mtime_ns)
    if _html_cache["key"] == key:
        return _html_cache["html"]
    viewer, bridge = _read_text(VIEWER), _read_text(BRIDGE)
    if BEGIN in viewer:
        raise RuntimeError("wbs_viewer.html に追記ブロックの目印がある（生成物を入力にしている）。")
    if "</script" in bridge.lower():
        raise RuntimeError("bridge.js に </script を書かない（HTML が script を早く閉じる）。")
    at = viewer.rfind("</body>")
    if at < 0:
        raise RuntimeError("wbs_viewer.html に </body> が見つからない。")
    block = f"{BEGIN}\n<script>\n{bridge.rstrip()}\n</script>\n{END}\n"
    html = viewer[:at] + block + viewer[at:]
    _html_cache.update(key=key, html=html)
    return html


# ---------------------------------------------------------------- MCP（JSON-RPC）

class Server:
    def __init__(self, workspace):
        self.ws = workspace

    # --- 宣言 ---
    def tools(self):
        file_prop = {
            "type": "string",
            "description": "Path of the JSON file, relative to the served folder "
                           "(e.g. \"wbs.json\"). Omit to open the default file.",
        }
        tools = [{
            "name": "wbs_open",
            "title": "Open WBS Viewer",
            "description": (
                "Show a WBS JSON file (plan / Gantt chart and issue list) to the user in the "
                "interactive WBS Viewer. Use when the user wants to see the plan, schedule, "
                "progress, delays, dependencies or issues. Returns a short text summary; the "
                "viewer itself shows the details."
                + ("" if self.ws.read_only else
                   " The user can edit in the viewer; edits are saved to the same file.")),
            "inputSchema": {"type": "object", "properties": {"file": file_prop},
                            "additionalProperties": False},
            "annotations": {"title": "Open WBS Viewer", "readOnlyHint": True,
                            "openWorldHint": False},
            "_meta": {"ui": {"resourceUri": UI_URI, "visibility": ["model", "app"]},
                      "ui/resourceUri": UI_URI},      # 旧い書き方も併記（古いホスト向け）
        }, {
            "name": "wbs_read",
            "title": "Read WBS JSON (viewer)",
            "description": "Used by the WBS Viewer itself to load the JSON text. "
                           "Returns the whole file.",
            "inputSchema": {"type": "object", "properties": {"file": file_prop},
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "_meta": {"ui": {"visibility": ["app"]}},
        }]
        if not self.ws.read_only:
            tools.append({
                "name": "wbs_save",
                "title": "Save WBS JSON (viewer)",
                "description": "Used by the WBS Viewer itself to write the edited JSON back. "
                               "Refused when the file changed since it was read.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": file_prop,
                        "text": {"type": "string",
                                 "description": "The whole JSON document to write."},
                        "baseVersion": {"type": "string",
                                        "description": "Version returned by wbs_read / wbs_save."},
                    },
                    "required": ["text", "baseVersion"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": True,
                                "idempotentHint": True, "openWorldHint": False},
                "_meta": {"ui": {"visibility": ["app"]}},
            })
        return tools

    def resource(self):
        return {"uri": UI_URI, "name": "wbs_viewer", "title": "WBS Viewer",
                "description": "WBS Viewer (plan / Gantt and issue list) as an MCP App.",
                "mimeType": UI_MIME}

    # --- ツール ---
    def call_tool(self, name, args):
        if not isinstance(args, dict):
            args = {}
        if name == "wbs_open":
            rel, text, version = self.ws.read(args.get("file"))
            today = datetime.date.today().isoformat()
            return {
                "content": [{"type": "text", "text": summarize(rel, text, today)}],
                # 画面が要るのは「どのファイルか」だけ。中身は wbs_read で取りに来る
                # （数百 KB の JSON をモデルの文脈に流さない）。
                "structuredContent": {"file": rel, "version": version,
                                      "readOnly": self.ws.read_only},
            }
        if name == "wbs_read":
            rel, text, version = self.ws.read(args.get("file"))
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": {"file": rel, "version": version}}
        if name == "wbs_save" and not self.ws.read_only:
            rel, version = self.ws.save(args.get("file"), args.get("text"),
                                        args.get("baseVersion"))
            return {"content": [{"type": "text", "text": f"Saved {rel}."}],
                    "structuredContent": {"file": rel, "version": version}}
        raise KeyError(name)

    # --- 1 通の処理 ---
    def handle(self, msg):
        """JSON-RPC のメッセージ 1 通を処理し、返事（無ければ None）を返す."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        mid, method = msg.get("id"), msg.get("method")
        if not isinstance(method, str):
            return None                                   # 相手からの返事（このサーバーは要求を出さない）
        if "id" not in msg:
            return None                                   # 通知には返事をしない
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": self._dispatch(method, params)}
        except _RpcError as e:
            return _error(mid, e.code, e.message)
        except Exception as e:  # noqa: BLE001  落ちずに返す（通信路を止めない）
            log("internal error:", repr(e))
            return _error(mid, -32603, "Internal error")

    def _dispatch(self, method, params):
        if method == "initialize":
            want = params.get("protocolVersion")
            return {
                "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": SERVER_NAME, "title": "WBS Viewer",
                               "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            name = params.get("name")
            try:
                return self.call_tool(name, params.get("arguments"))
            except ToolError as e:
                return {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except KeyError:
                raise _RpcError(-32602, f"Unknown tool: {name}") from None
        if method == "resources/list":
            return {"resources": [self.resource()]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            if params.get("uri") != UI_URI:
                raise _RpcError(-32002, "Resource not found")
            return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME,
                                  "text": build_app_html(),
                                  # 外部への通信も外部の資源も使わない（既定の厳しい CSP のままで動く）
                                  "_meta": {"ui": {"prefersBorder": True}}}]}
        raise _RpcError(-32601, f"Method not found: {method}")

    def handle_payload(self, payload):
        """1 通、または配列（古い版のバッチ）を処理する."""
        if isinstance(payload, list):
            out = [r for r in (self.handle(m) for m in payload) if r is not None]
            return out or None
        return self.handle(payload)


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 通信路

def serve_stdio(server):
    """改行区切りの JSON-RPC を stdin から読み、stdout に返す."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    log(f"stdio: {server.ws.root} (default: {server.ws.default or '-'}"
        f"{', read-only' if server.ws.read_only else ''})")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line.decode("utf-8"))
        except ValueError:
            reply = _error(None, -32700, "Parse error")
        else:
            reply = server.handle_payload(payload)
        if reply is not None:
            stdout.write(_dumps(reply).encode("utf-8") + b"\n")
            stdout.flush()


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _hostname(value):
    """`Host` ヘッダや Origin からホスト名だけを取り出す."""
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value).split("/")[0]
    if value.startswith("["):                      # [::1]:8765
        return value[1:].split("]")[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def serve_http(server, bind, port, token):
    """Streamable HTTP（状態を持たない・POST に JSON で答える）."""
    import http.server

    path = "/mcp" + ("/" + token if token else "")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "wbs-mcp"

        def log_message(self, fmt, *args):         # 既定は stderr に 1 行ずつ。秘密のパスを残さない
            pass

        def _send(self, status, body=b"", ctype="application/json"):
            self.send_response(status)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if status == 405:
                self.send_header("Allow", "POST")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _allowed(self):
            got = self.path.split("?", 1)[0].rstrip("/")
            if not hmac.compare_digest(got.encode(), path.encode()):
                self._send(404)
                return False
            if not token:
                # 合言葉なしで受けるのは自分の端末からだけ（DNS リバインディングと、
                # 開いている Web ページからの呼び出しを断る）。外へ出すなら --token。
                if _hostname(self.headers.get("Host")) not in LOCAL_HOSTS:
                    self._send(403)
                    return False
                origin = self.headers.get("Origin")
                if origin and _hostname(origin) not in LOCAL_HOSTS:
                    self._send(403)
                    return False
            return True

        def do_POST(self):
            if not self._allowed():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BYTES * 2:
                self._send(413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._send(400, _dumps(_error(None, -32700, "Parse error")).encode("utf-8"))
                return
            reply = server.handle_payload(payload)
            if reply is None:
                self._send(202)                    # 通知だけ＝本文なし
            else:
                self._send(200, _dumps(reply).encode("utf-8"))

        def do_GET(self):                          # サーバーからの push は持たない
            if self._allowed():
                self._send(405)

        do_DELETE = do_GET

    httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    shown = f"http://{bind}:{httpd.server_address[1]}/mcp" + ("/<token>" if token else "")
    log(f"http: {shown} -> {server.ws.root} (default: {server.ws.default or '-'}"
        f"{', read-only' if server.ws.read_only else ''})")
    if bind not in LOCAL_HOSTS and not token:
        log("warning: --bind で外から届く状態。--token が無いので、端末の外からの要求は断る。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="WBS Viewer を MCP App として配る MCP サーバー（依存ゼロ）。")
    parser.add_argument("path", nargs="?", default=".",
                        help="JSON ファイル、または JSON を置いたフォルダ（既定: 今のフォルダ）。"
                             "ファイルを指すとそのファイルだけ、フォルダを指すとその中の .json を開ける")
    parser.add_argument("--read-only", action="store_true",
                        help="見るだけ（保存ツールを出さない・編集ボタンを隠す）")
    parser.add_argument("--http", metavar="PORT", type=int,
                        help="stdio の代わりに HTTP で待つ（http://127.0.0.1:PORT/mcp）")
    parser.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                        help="--http の待ち受けアドレス（既定: 127.0.0.1）")
    parser.add_argument("--token", metavar="SECRET",
                        default=os.environ.get("WBS_MCP_TOKEN") or None,
                        help="--http の合言葉。URL が /mcp/SECRET になる（トンネルで外へ出す時は必須。"
                             "環境変数 WBS_MCP_TOKEN でも渡せる）")
    parser.add_argument("--print-html", action="store_true",
                        help="配る HTML（ビューア＋橋渡し）を標準出力に書いて終わる")
    args = parser.parse_args(argv)

    if args.print_html:
        sys.stdout.buffer.write(build_app_html().encode("utf-8"))
        return 0
    if args.token and not re.fullmatch(r"[A-Za-z0-9._~-]{16,}", args.token):
        raise SystemExit("--token は 16 文字以上の英数字と . _ ~ - にする。")
    build_app_html()                               # 起動時に 1 度作って、壊れていたらここで止める
    server = Server(Workspace(args.path, read_only=args.read_only))
    if args.http is not None:
        serve_http(server, args.bind, args.http, args.token)
    else:
        serve_stdio(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
