"""MCP App としての WBS Viewer：ホスト（テスト用の最小ホスト）の iframe の中で、
表示・編集・保存・外部変更の検知・ダイアログの代役・全画面・外部リンクが通ること。

ビューア → ホスト → サーバー（mcp_server.Server）→ 一時フォルダの実ファイル、を往復で通す。
sandbox は参照実装と同じもの（allow-modals なし）と、いちばん厳しい形（オリジンなし）の2通り。
CSP は MCP Apps の既定（外部の資源も通信も不可）を掛け、違反が1件も出ないことを見る。
"""
import json
import pathlib
import tempfile
from playwright.sync_api import sync_playwright
from common import (SANDBOX_OPAQUE, check, finish, mcp_server, mount, sent, text_of, tool)

DOC = {
    "name": "テスト用ブック", "_meta": {"owner": "tester"},
    "projects": [{
        "name": "案件A", "milestones": [],
        "tasks": [{"id": "1", "name": "工程1", "_memo": "user key", "children": [
            {"id": "1.1", "name": "作業A", "qty": 1, "hours": 8, "assignee": "佐藤",
             "plan": {"start": "2026-06-01", "end": "2026-06-05"},
             "actual": {"start": None, "end": None},
             "note": "仕様 https://example.com/spec を参照"},
            {"id": "1.2", "name": "作業B", "qty": 1, "hours": 8, "assignee": "佐藤",
             "plan": {"start": "2026-06-03", "end": "2026-06-08"},
             "actual": {"start": None, "end": None}, "note": ""}]}],
        "issues": [{
            "id": 1, "title": "課題その1", "priority": "mid", "opened": "2026-06-01",
            "due": "2026-06-30", "ifIgnored": "困る", "ifDone": "", "closeWhen": "確かめた",
            "decisions": [], "pending": None, "closed": None,
            "links": [{"wbs": "1.2"}],
            "actions": [{"date": "2026-06-02", "text": "調査", "assignee": "佐藤", "done": True}],
            "note": ""}],
    }],
}
ASG = 'input[data-path="0/0/0"][data-field="assignee"]'

errors = []
with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
    path = pathlib.Path(tmp) / "wbs.json"
    disk = lambda: json.loads(path.read_text(encoding="utf-8"))

    def reset():
        path.write_text(json.dumps(DOC, ensure_ascii=False, indent=2), encoding="utf-8")

    def page():
        pg = b.new_page(viewport={"width": 1500, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e).split("\n")[0]))
        # CSP 違反はコンソールのエラーとして出る（"Refused to …"）。警告は拾わない
        # （allow-same-origin の sandbox に Chromium が出す注意書きは、ホスト側の構成の話）。
        pg.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
        pg.on("dialog", lambda d: (errors.append("dialog:" + d.message), d.dismiss()))
        return pg

    def edit_assignee(f, value):
        f.fill(ASG, value)
        f.dispatch_event(ASG, "change")

    flush = lambda pg: pg.wait_for_timeout(700)    # 保存デバウンス(400ms)＋ツール呼び出しの往復
    bar = lambda f: (f.inner_text("#mcpBar") if f.eval_on_selector(
        "#mcpBar", "e=>e.classList.contains('show')") else "")

    b = p.chromium.launch()
    reset()
    srv = mcp_server.Server(mcp_server.Workspace(path))

    # ===== 表示（参照実装と同じ sandbox） =====
    pg = page()
    f = mount(pg, srv)
    rows = f.eval_on_selector_all("#leftRows .lrow .c.num", "e=>e.map(x=>x.innerText.trim())")
    check(rows[-3:] == ["1", "1.1", "1.2"], f"会話の中の iframe に計画の行が出る -> {rows}")
    body = f.inner_text("body")
    check("案件A" in body and "NaN" not in body, "案件名が出て NaN が無い")
    check("wbs.json" in f.inner_text("#stat"), "ヘッダにファイル名が出る（ハンドル経由で読めている）")
    order = pg.evaluate("window.__log.map(m=>m.method).filter(Boolean)")
    check(order[:2] == ["ui/initialize", "ui/notifications/initialized"],
          f"初期化は ui/initialize → initialized の順 -> {order[:3]}")
    init = sent(pg, "ui/initialize")[0]
    check(init["protocolVersion"] == "2026-01-26" and init["appInfo"]["name"] == "single-file-wbs"
          and init["appCapabilities"]["availableDisplayModes"] == ["inline", "fullscreen"],
          "ui/initialize に版・名前・対応する表示モードを載せる")
    calls = [c["name"] for c in sent(pg, "tools/call")]
    check(calls == ["wbs_read"], f"中身は画面が wbs_read で取りに行く（1回）-> {calls}")
    check(sent(pg, "ui/notifications/size-changed") == [{"height": 640}]
          and pg.eval_on_selector("#app", "e=>e.offsetHeight") == 640,
          "高さをホストが決めていなければ、欲しい高さ（640px）を伝える")
    check(f.eval_on_selector("#openBtn", "e=>getComputedStyle(e).display") == "none",
          "「ファイルを開く」は隠す（開くのはホスト側のツール）")
    check(f.eval_on_selector("#editBtn", "e=>getComputedStyle(e).display") != "none",
          "書けるサーバーでは「編集」が出る")
    check(not f.eval_on_selector("#pmSwitch", "e=>e.hidden"), "「計画｜課題」スイッチが出る")

    # ===== 編集 → 自動保存（wbs_save） =====
    f.click("#editBtn")
    pg.wait_for_timeout(150)
    check("on" in (f.get_attribute("#editBtn", "class") or "") and bar(f) == "",
          "編集ON（保存ダイアログも確認も挟まらない）")
    edit_assignee(f, "鈴木")
    flush(pg)
    check(disk()["projects"][0]["tasks"][0]["children"][0]["assignee"] == "鈴木",
          "フィールド編集が実ファイルに保存される")
    check(disk()["_meta"] == {"owner": "tester"}
          and disk()["projects"][0]["tasks"][0]["_memo"] == "user key", "_ キーが残る（round-trip）")
    raw = path.read_text(encoding="utf-8")
    check("_calc" not in raw and "_leaf" not in raw, "内部派生値（_calc/_leaf）は書かれない")
    check("保存済" in f.inner_text("#stat") or ":" in f.inner_text("#saveMsg"),
          f"保存状態が出る -> {f.inner_text('#saveMsg')!r}")
    edit_assignee(f, "高橋")
    flush(pg)
    check(disk()["projects"][0]["tasks"][0]["children"][0]["assignee"] == "高橋",
          "続けて保存できる（保存のたびに版を進めている）")

    # ===== 外部変更：上書きせず、知らせる =====
    ext = disk()
    ext["projects"][0]["tasks"][0]["children"][1]["name"] = "外で直した"
    path.write_text(json.dumps(ext, ensure_ascii=False, indent=2), encoding="utf-8")
    edit_assignee(f, "上書きしようとした")
    flush(pg)
    now = disk()["projects"][0]["tasks"][0]["children"]
    check(now[1]["name"] == "外で直した" and now[0]["assignee"] == "高橋",
          "外で変わったファイルは上書きしない")
    check("更新" in bar(f) and "保存失敗" in f.inner_text("#saveMsg"),
          f"保存できなかった理由を画面内の帯で知らせる -> {bar(f)[:40]!r}")

    # ===== confirm の代役：同じ操作をもう一度で確定 =====
    f.click("#refreshBtn")
    pg.wait_for_timeout(200)
    check("もう一度" in bar(f) and "上書きしようとした" in f.input_value(ASG),
          "未保存があるままの「更新」は、1回目では読み直さない（確認を帯で出す）")
    f.click("#refreshBtn")
    flush(pg)
    names = f.eval_on_selector_all("#leftRows input.nm-in", "e=>e.map(x=>x.value)")
    check("外で直した" in names and f.input_value(ASG) == "高橋",
          f"2回目で読み直し、外の変更が画面に出る -> {names}")
    edit_assignee(f, "田中")
    flush(pg)
    now = disk()["projects"][0]["tasks"][0]["children"]
    check(now[0]["assignee"] == "田中" and now[1]["name"] == "外で直した",
          "読み直した後は、外の変更を保ったまま保存できる")

    f.click('button[data-act="del"][data-path="0/0/1"]')
    pg.wait_for_timeout(200)
    check(len(disk()["projects"][0]["tasks"][0]["children"]) == 2 and "もう一度" in bar(f),
          "削除の ✕ は1回では消えない")
    f.click('button[data-act="del"][data-path="0/0/1"]')
    flush(pg)
    check([c["id"] for c in disk()["projects"][0]["tasks"][0]["children"]] == ["1.1"],
          "2回目の ✕ で削除され、保存される")

    # ===== prompt の代役：帯の入力欄で受けて、同じ操作をもう一度で反映 =====
    f.click('#pmSwitch .pm-b[data-pmv="issue"]')
    pg.wait_for_timeout(200)
    f.click('[data-act="addproj"]')
    pg.wait_for_timeout(200)
    check(f.query_selector("#mcpBar input") is not None and len(disk()["projects"]) == 1,
          "案件の追加は、1回目は名前を帯の入力欄で聞く")
    f.fill("#mcpBar input", "案件B")
    f.click("#mcpBar div button")
    f.click('[data-act="addproj"]')
    flush(pg)
    check([x["name"] for x in disk()["projects"]] == ["案件A", "案件B"],
          f"入力した名前で案件が追加・保存される -> {[x['name'] for x in disk()['projects']]}")
    f.click('#pmSwitch .pm-b[data-pmv="plan"]')

    # ===== 閉じる前：書きかけを流し切ってから返事をする =====
    edit_assignee(f, "閉じる直前")
    pg.evaluate("""() => new Promise(done => {
        const t = setInterval(() => { if (window.__log.some(m => m.id === 'bye' && m.result)) {
            clearInterval(t); done(); } }, 20);
        window.__send({jsonrpc:'2.0', id:'bye', method:'ui/resource-teardown', params:{}}); })""")
    check(disk()["projects"][0]["tasks"][0]["children"][0]["assignee"] == "閉じる直前",
          "ui/resource-teardown の返事の時点で、直前の編集が保存されている")

    # ===== 全画面 =====
    check("全画面" in f.inner_text("#mcpFs"), "ホストが全画面に対応していれば切替ボタンを出す")
    f.click("#mcpFs")
    pg.wait_for_timeout(200)
    check(sent(pg, "ui/request-display-mode") == [{"mode": "fullscreen"}]
          and "戻す" in f.inner_text("#mcpFs"), "全画面を頼み、ラベルが「戻す」に変わる")
    f.click("#mcpFs")
    pg.wait_for_timeout(200)
    check(sent(pg, "ui/request-display-mode")[-1] == {"mode": "inline"}
          and sent(pg, "ui/notifications/size-changed")[-1] == {"height": 640},
          "戻すと、会話の中の高さをもう一度伝える")
    f.click("#langBtn")
    pg.wait_for_timeout(100)
    check("Full screen" in f.inner_text("#mcpFs"), "言語を替えるとボタンの文言も替わる")
    f.click("#langBtn")

    # ===== 外部リンク：ホストに開いてもらう =====
    f.click("#editBtn")                             # 編集OFF（備考がリンクになる）
    pg.wait_for_timeout(200)
    f.click('#leftRows a[href="https://example.com/spec"]')
    pg.wait_for_timeout(150)
    check(sent(pg, "ui/open-link") == [{"url": "https://example.com/spec"}],
          "備考の URL は ui/open-link でホストに開いてもらう")
    check(len(pg.context.pages) == 1, "iframe から新しいタブは開かない")
    check([m for m in pg.evaluate("window.__log") if m.get("error")] == [],
          "ビューアからホストへエラーの返事を出していない")
    pg.close()

    # ===== いちばん厳しい sandbox（オリジンなし）：表示・行への移動・保存 =====
    reset()
    pg = page()
    f = mount(pg, srv, sandbox=SANDBOX_OPAQUE)
    check(len(f.query_selector_all("#leftRows .lrow")) == 4,
          "オリジンなしの sandbox でも描ける（localStorage が使えなくても落ちない）")
    f.click('#pmSwitch .pm-b[data-pmv="issue"]')
    pg.wait_for_timeout(200)
    f.click("#isMain .lk-i[data-jw]")
    pg.wait_for_timeout(300)
    check(f.eval_on_selector("#isMain", "e=>e.hidden")
          and f.eval_on_selector_all(".lrow.pmjump .c.num", "e=>e.map(x=>x.innerText.trim())") == ["1.2"],
          "課題 → 計画のリンクで、同じ画面のまま該当行へ飛べる（URL を書き換えられなくても）")
    f.click("#editBtn")
    pg.wait_for_timeout(150)
    edit_assignee(f, "厳しい側")
    flush(pg)
    check(disk()["projects"][0]["tasks"][0]["children"][0]["assignee"] == "厳しい側",
          "オリジンなしの sandbox でも保存できる")
    pg.close()

    # ===== 読み取り専用のサーバー =====
    reset()
    before = path.read_bytes()
    ro = mcp_server.Server(mcp_server.Workspace(path, read_only=True))
    pg = page()
    f = mount(pg, ro)
    check(len(f.query_selector_all("#leftRows .lrow")) == 4
          and f.eval_on_selector("#editBtn", "e=>getComputedStyle(e).display") == "none",
          "読み取り専用では表示だけで、「編集」を出さない")
    check(path.read_bytes() == before, "読み取り専用ではファイルが変わらない")
    pg.close()

    # ===== 開けなかった時：理由を帯で出す（真っ白にしない） =====
    pg = page()
    failed = tool(srv, "wbs_open", {"file": "nope.json"})
    f = mount(pg, srv, args={"file": "nope.json"}, result=failed)
    check(failed.get("isError") is True and text_of(failed) in bar(f)
          and len(f.query_selector_all("#leftRows .lrow")) == 0,
          f"ツールが失敗した時は、その文言を画面に出す -> {bar(f)!r}")
    pg.close()

    # ===== 壊れた JSON：本体の通知が帯に出る =====
    path.write_text("{ broken json !!!", encoding="utf-8")
    pg = page()
    f = mount(pg, srv)
    check("wbs.json" in bar(f) and len(f.query_selector_all("#leftRows .lrow")) == 0,
          f"壊れた JSON は alert の代役で知らせる -> {bar(f)[:50]!r}")
    pg.close()
    b.close()

finish(errors)
