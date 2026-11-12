# ADR-0012: MCP App は「配る時に橋渡しを足す」で作る（本体は変えない）

会話の中の画面（MCP App）として配る時も `wbs_viewer.html` は無改変とし、サーバーが配る直前に橋渡しのスクリプトを足す。

## ステータス
提案（2026-11-12・フォーク側で実装。採否は未決）

## コンテキスト
MCP Apps 対応のホスト（Claude など）は、MCP サーバーが出す `ui://` の HTML を会話の中の iframe に表示できる。
`wbs_viewer.html` は依存ゼロの単一 HTML なので、そのまま資源の中身にできる。ただし iframe の sandbox の中では次が使えない。

- File System Access API（読込・保存の経路そのもの）
- `alert`／`confirm`／`prompt`（`allow-modals` が無い）
- 新しいタブ（`allow-popups` が無い）

案は3つあった。

- **A. 本体に「MCP モード」の分岐を足す**
- **B. MCP App 専用の別ビューアを作る**
- **C. 本体は変えず、配る時に橋渡しを足す**（`scripts/build_demo.py` がデモで採っている作法）

## 決定
**C** を採る。

- サーバー（`scripts/mcp_server.py`）が `wbs_viewer.html` の `</body>` の手前に `scripts/mcp_app/bridge.js` を差し込んで配る。
- 橋渡しは、本体が元から持つ入口（`showOpenFilePicker()` の返すハンドル）に**代役のハンドル**を渡す。読む＝`wbs_read`、書く＝`wbs_save` のツール呼び出し。
- サーバーも橋渡しも**依存ゼロ**（Python 標準ライブラリ／SDK を使わない素の `postMessage`）。

## 理由
- **本体の「1枚で `file://`」を守れる**（[ADR-0001](0001-dependency-free-single-file.md)）。本体に分岐を足すと、`file://` で開く人全員が MCP 用のコードを運ぶことになる。
- **保存パスに手を入れない**（[ADR-0002](0002-file-system-access-editing.md)）。データ消失歴のある経路なので、キュー・直列化・保存状態の表示は本体のものをそのまま通す。ハンドルの差し替えは e2e のフェイクハンドルと同じ入口で、すでに回帰網が踏んでいる。
- **B を却下**：描画を二重に持つと、本体の変更に追随し続ける手間が永続する。
- **サーバーを Python にした**：検査（`scripts/check.py`）やデモ生成と同じ「依存ゼロの Python スクリプト」に揃う。`uv` もビルドも要らず `python3 scripts/mcp_server.py` で動く。
- **外部変更の検知をサーバー側の版（中身のハッシュ）にした**：sandbox では `confirm` が使えないので、本体の「上書きしますか」の確認に頼れない。サーバーが断れば、確認なしでも上書きは起きない。

## 影響
- `wbs_viewer.html` の内部（`#openBtn`・`showOpenFilePicker` を読む経路・`window.__PM.lang()`）に橋渡しが依存する。本体のこの入口を変える時は `tests/e2e_mcp/` が赤くなる。
- sandbox の中では確認と入力の手触りが変わる（同じ操作をもう一度／帯の入力欄）。`file://` で開いた時の動きは変わらない。
- 仕様の細部と確認済みの範囲は [`docs/design/mcp-app.md`](../design/mcp-app.md)。
