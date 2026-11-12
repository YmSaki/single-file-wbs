"""MCP サーバー（scripts/mcp_server.py）：宣言・配布 HTML・読み書きの守り・stdio と HTTP の通信路。

ブラウザは使わない（ビューア側は test_app.py）。書き込み先は一時フォルダだけ。
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from common import ROOT, check, finish, mcp_server, rpc, text_of, tool

SCRIPT = ROOT / "scripts" / "mcp_server.py"
SAMPLE = ROOT / "wbs_sample_issues.json"

with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp).resolve()
    work = tmp / "work"
    work.mkdir()
    shutil.copy(SAMPLE, work / "wbs.json")
    shutil.copy(ROOT / "wbs_sample.json", work / "other.json")
    (work / "notes.json").write_text('["not", "a", "wbs"]', encoding="utf-8")
    (tmp / "outside.json").write_text('{"projects": []}', encoding="utf-8")

    ws = mcp_server.Workspace(work)
    srv = mcp_server.Server(ws)

    # ===== 宣言 =====
    init = rpc(srv, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    check(init["protocolVersion"] == "2025-06-18", "知っている版はそのまま返す")
    check(rpc(srv, "initialize", {"protocolVersion": "1999-01-01"})["protocolVersion"]
          == mcp_server.PROTOCOL_VERSIONS[0], "知らない版には自分の最新で答える")
    check(set(init["capabilities"]) == {"tools", "resources"}, "機能は tools と resources だけ")
    check(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None,
          "通知には返事をしない")
    check(rpc(srv, "ping") == {}, "ping に空で答える")
    check(srv.handle({"jsonrpc": "2.0", "id": 9, "method": "no/such"})["error"]["code"] == -32601,
          "知らないメソッドは -32601")

    tools = {t["name"]: t for t in rpc(srv, "tools/list")["tools"]}
    check(set(tools) == {"wbs_open", "wbs_read", "wbs_save"}, f"ツールは3つ -> {sorted(tools)}")
    ui = tools["wbs_open"]["_meta"]["ui"]
    check(ui["resourceUri"].startswith("ui://") and "model" in ui["visibility"],
          "wbs_open は ui:// の画面に結び付き、モデルから見える")
    check(tools["wbs_open"]["_meta"]["ui/resourceUri"] == ui["resourceUri"],
          "旧い書き方（_meta['ui/resourceUri']）も同じ値で併記")
    check(tools["wbs_read"]["_meta"]["ui"]["visibility"] == ["app"]
          and tools["wbs_save"]["_meta"]["ui"]["visibility"] == ["app"],
          "wbs_read / wbs_save は画面専用（モデルには見せない）")
    check(all(t["inputSchema"].get("type") == "object" for t in tools.values()),
          "入力スキーマの根はすべて type: object")

    res = rpc(srv, "resources/list")["resources"]
    check(len(res) == 1 and res[0]["uri"] == ui["resourceUri"]
          and res[0]["mimeType"] == "text/html;profile=mcp-app", "資源は画面1つ・MCP App の MIME")
    body = rpc(srv, "resources/read", {"uri": ui["resourceUri"]})["contents"][0]
    check(body["mimeType"] == "text/html;profile=mcp-app" and body["uri"] == ui["resourceUri"],
          "resources/read が同じ URI・MIME で返る")
    check(srv.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                      "params": {"uri": "ui://nope"}})["error"]["code"] == -32002,
          "知らない資源は -32002")

    # ===== 配布 HTML：本体を変えていない =====
    html = body["text"]
    viewer = (ROOT / "wbs_viewer.html").read_bytes().decode("utf-8")
    i, j = html.find(mcp_server.BEGIN), html.find(mcp_server.END)
    check(0 < i < j, "追記ブロックの目印がある")
    stripped = html[:i] + html[j + len(mcp_server.END) + 1:]
    check(stripped == viewer, "追記ブロックを除くと wbs_viewer.html とバイト一致（本体は無改変）")
    block = html[i:j]
    check(block.lower().count("</script") == 1, "追記ブロックの中の </script は終端の1つだけ")
    check(not re.search(r"\b(src|href)\s*=\s*[\"']https?://", block)
          and "fetch(" not in block and "XMLHttpRequest" not in block,
          "橋渡しは外部の資源も通信も使わない（依存ゼロ・外部送信なし）")
    printed = subprocess.run([sys.executable, str(SCRIPT), "--print-html"], capture_output=True)
    check(printed.returncode == 0 and printed.stdout.decode("utf-8") == html,
          "--print-html は配る HTML と同じものを出す")

    # ===== 開く・読む =====
    opened = tool(srv, "wbs_open")
    sc = opened["structuredContent"]
    check(sc["file"] == "wbs.json" and sc["readOnly"] is False and len(sc["version"]) == 16,
          f"フォルダに wbs.json があれば既定で開く -> {sc}")
    summary = text_of(opened)
    check("販売管理システム移行" in summary and "issues: 9 (5 open, 4 closed" in summary,
          "要約に案件名と課題の件数が出る")
    check("plan: 3 leaf tasks" in summary, "要約に計画の葉の数が出る")
    check(len(summary) < 1200 and '"projects"' not in summary,
          "要約は短い（JSON の中身をモデルの文脈に流さない）")
    check("structuredContent" in opened and "text" not in json.dumps(sc),
          "画面へ渡すのはファイル名と版だけ")
    read = tool(srv, "wbs_read", {"file": "wbs.json"})
    check(text_of(read) == (work / "wbs.json").read_text(encoding="utf-8")
          and read["structuredContent"]["version"] == sc["version"],
          "wbs_read は中身をそのまま返し、版は wbs_open と同じ")
    check("plan: 7 leaf tasks" in text_of(tool(srv, "wbs_open", {"file": "other.json"})),
          "file を指定して別の JSON を開ける")

    # 範囲の守り：フォルダの外・JSON 以外・無いファイル
    for bad, why in [("../outside.json", "親フォルダ"), (str(tmp / "outside.json"), "絶対パス"),
                     ("wbs.txt", "JSON 以外"), ("nope.json", "無いファイル"),
                     ("sub/../../outside.json", "途中の ..")]:
        r = tool(srv, "wbs_read", {"file": bad})
        check(r.get("isError") is True, f"{why} は開けない -> {text_of(r)[:50]}")
    if hasattr(os, "symlink"):
        try:
            os.symlink(tmp / "outside.json", work / "link.json")
            r = tool(srv, "wbs_read", {"file": "link.json"})
            check(r.get("isError") is True, "symlink でフォルダの外へは出られない")
        except OSError:
            pass                                   # symlink を作れない環境では省く

    # 既定が無いフォルダ：候補を挙げて聞き返させる（WBS らしくない JSON は挙げない）
    os.remove(work / "wbs.json")
    r = tool(mcp_server.Server(mcp_server.Workspace(work)), "wbs_open")
    check(r.get("isError") is True and "other.json" in text_of(r) and "notes.json" not in text_of(r),
          f"既定が無ければ候補を返す -> {text_of(r)}")
    shutil.copy(SAMPLE, work / "wbs.json")

    # ===== 保存 =====
    v0 = tool(srv, "wbs_read", {"file": "wbs.json"})["structuredContent"]["version"]
    doc = json.loads((work / "wbs.json").read_text(encoding="utf-8"))
    doc["name"] = "保存テスト"
    new_text = json.dumps(doc, ensure_ascii=False, indent=2)
    saved = tool(srv, "wbs_save", {"file": "wbs.json", "text": new_text, "baseVersion": v0})
    v1 = saved["structuredContent"]["version"]
    check(not saved.get("isError") and v1 != v0
          and (work / "wbs.json").read_bytes() == new_text.encode("utf-8"),
          "wbs_save は渡した文字列をそのまま書く（整形し直さない）")
    check([p.name for p in work.iterdir() if p.name.endswith(".tmp")] == [],
          "一時ファイルを残さない")
    stale = tool(srv, "wbs_save", {"file": "wbs.json", "text": "{}", "baseVersion": v0})
    check(stale.get("isError") is True and "更新" in text_of(stale)
          and (work / "wbs.json").read_bytes() == new_text.encode("utf-8"),
          "古い版からの保存は断る（外で変わったファイルを上書きしない）")
    r = tool(srv, "wbs_save", {"file": "wbs.json", "text": "{}"})
    check(r.get("isError") is True, "版を渡さない保存は断る")
    r = tool(srv, "wbs_save", {"file": "wbs.json", "text": "{ broken", "baseVersion": v1})
    check(r.get("isError") is True and (work / "wbs.json").read_bytes() == new_text.encode("utf-8"),
          "壊れた JSON は書かない")
    r = tool(srv, "wbs_save", {"file": "new.json", "text": "{}", "baseVersion": v1})
    check(r.get("isError") is True and not (work / "new.json").exists(),
          "無いファイルは作らない（保存は既存ファイルの更新だけ）")

    # ===== ファイル 1 つで起動した時：隣の JSON には触れない =====
    one = mcp_server.Server(mcp_server.Workspace(work / "wbs.json"))
    check(tool(one, "wbs_open")["structuredContent"]["file"] == "wbs.json", "指したファイルが既定になる")
    r = tool(one, "wbs_read", {"file": "other.json"})
    check(r.get("isError") is True and "wbs.json" in text_of(r),
          "ファイルを指して起動したら、同じフォルダの別の JSON は開けない")
    r = tool(one, "wbs_save", {"file": "other.json", "text": "{}", "baseVersion": v1})
    check(r.get("isError") is True, "同じく、別の JSON には書けない")

    # ===== 読み取り専用 =====
    ro = mcp_server.Server(mcp_server.Workspace(work / "wbs.json", read_only=True))
    names = [t["name"] for t in rpc(ro, "tools/list")["tools"]]
    check(names == ["wbs_open", "wbs_read"], f"--read-only では wbs_save を出さない -> {names}")
    check(tool(ro, "wbs_open")["structuredContent"]["readOnly"] is True, "画面に読み取り専用と伝える")
    check(ro.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "wbs_save", "arguments": {"text": "{}", "baseVersion": v1}}}
                    )["error"]["code"] == -32602, "隠したツールは呼んでも断る")
    check((work / "wbs.json").read_bytes() == new_text.encode("utf-8"), "読み取り専用では書かれない")

    # ===== 通信路：stdio =====
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "wbs_open", "arguments": {}}}]
    p = subprocess.run([sys.executable, str(SCRIPT), str(work / "wbs.json")],
                       input=("\n".join(json.dumps(m) for m in lines) + "\nnot json\n").encode(),
                       capture_output=True, timeout=30)
    out = [json.loads(x) for x in p.stdout.decode("utf-8").splitlines()]
    check(p.returncode == 0 and [m.get("id") for m in out] == [1, 2, None],
          f"stdio：要求にだけ 1 行ずつ返す（通知は無言・壊れた行は Parse error）-> {[m.get('id') for m in out]}")
    check(out[1]["result"]["structuredContent"]["file"] == "wbs.json"
          and out[2]["error"]["code"] == -32700, "stdio：ツールの結果と -32700")
    check(b"[wbs-mcp]" in p.stderr and b"[wbs-mcp]" not in p.stdout,
          "人向けの出力は stderr だけ（stdout は通信路）")

    # ===== 通信路：HTTP =====
    def start(*extra):
        proc = subprocess.Popen([sys.executable, str(SCRIPT), str(work / "wbs.json"),
                                 "--http", "0", *extra], stderr=subprocess.PIPE)
        port = int(re.search(rb":(\d+)/mcp", proc.stderr.readline()).group(1))
        return proc, port

    def post(port, path, msg, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(msg).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None

    call = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
    proc, port = start()
    try:
        st, js = post(port, "/mcp", call)
        check(st == 200 and len(js["result"]["tools"]) == 3, "HTTP：POST /mcp に JSON で答える")
        st, js = post(port, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        check(st == 202 and js is None, "HTTP：通知だけなら 202・本文なし")
        check(post(port, "/other", call)[0] == 404, "HTTP：別のパスは 404")
        check(post(port, "/mcp", call, {"Host": "evil.example"})[0] == 403,
              "HTTP：合言葉なしでは、よそのホスト名での要求を断る（DNS リバインディング対策）")
        check(post(port, "/mcp", call, {"Origin": "https://evil.example"})[0] == 403,
              "HTTP：合言葉なしでは、よその Web ページからの要求を断る")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=10)
            got = 200
        except urllib.error.HTTPError as e:
            got = e.code
        check(got == 405, "HTTP：GET は 405（サーバーからの push は持たない）")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    secret = "s3cret-token-0123456789"
    proc, port = start("--token", secret, "--read-only")
    try:
        check(post(port, "/mcp", call)[0] == 404, "HTTP：合言葉つきでは /mcp だけでは届かない")
        st, js = post(port, "/mcp/" + secret, call, {"Host": "abc.trycloudflare.com"})
        check(st == 200 and [t["name"] for t in js["result"]["tools"]] == ["wbs_open", "wbs_read"],
              "HTTP：/mcp/<合言葉> ならトンネル越しのホスト名でも届く（読み取り専用）")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    bad = subprocess.run([sys.executable, str(SCRIPT), str(work), "--http", "0", "--token", "short"],
                         capture_output=True, timeout=30)
    check(bad.returncode != 0, "短すぎる合言葉は起動時に断る")

finish()
