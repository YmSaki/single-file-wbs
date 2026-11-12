# MCP App のテスト（サーバー＋ headless Chromium）

`scripts/mcp_server.py`（MCP サーバー）と、それが配る画面（`wbs_viewer.html`＋`scripts/mcp_app/bridge.js`）を検証します。
書き込み先は一時フォルダだけで、**実データには一切書き込みません**。

> 注: ここで使う Playwright は**テスト専用の開発ツール**です。サーバーも橋渡しも製品本体も依存ゼロのままです。

## 実行方法

```bash
uv sync                                  # 初回のみ
uv run playwright install chromium       # 初回のみ
uv run python tests/e2e_mcp/run_all.py         # 全スイート
uv run python tests/e2e_mcp/test_server.py     # 個別（こちらはブラウザ不要）
```

## スイート一覧

| ファイル | 検証内容 |
|---|---|
| run_all.py | 全 `test_*.py` を順に実行（1つでも赤なら FAILED） |
| test_server.py | サーバー単体：ツールと `ui://` 資源の宣言・**配る HTML が本体とバイト一致**（追記ブロックを除く）・要約に中身を流さない・フォルダの外／JSON 以外／symlink を開かせない・**古い版からの保存を断る**・壊れた JSON を書かない・読み取り専用・stdio と HTTP（合言葉・`Host`／`Origin` の守り） |
| test_app.py | 画面の往復：ホスト役の iframe（sandbox 2通り＋既定の CSP）で、表示 → 編集 → 自動保存（実ファイル）→ 外部変更は上書きしない →「同じ操作をもう一度」の確認 → 帯の入力欄 → 閉じる前に書き切る → 全画面 → 外部リンク → 読み取り専用 → 失敗時の文言 |

## ホスト役について

`common.py` の `HOST_PAGE` が、MCP Apps 仕様（2026-01-26）どおりに応対する最小のホストです。
画面からの `tools/call` は、同じプロセスに読み込んだ `mcp_server.Server` に渡します＝**画面 → ホスト → サーバー → ファイルの往復を本物のコードで通します**。

公式の参照ホスト（[ext-apps](https://github.com/modelcontextprotocol/ext-apps) の `examples/basic-host`）での確認結果と、Claude 実機で未確認の点は [`docs/design/mcp-app.md`](../../docs/design/mcp-app.md) にあります。
