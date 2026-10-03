# reqviz — 要件パラメータ可視化ツール

業務要件からインフラ設計までのパラメータと、その算出式・制約・関連をグラフで可視化し、
確定的な計算と AI による定性評価、変更・承認・版管理までを一元管理する Web アプリです。

- 実行時の依存は **Python 標準ライブラリのみ**（`http.server` / `sqlite3` / `urllib` / `ast`）
- 画面は **Vanilla JS + SVG**（外部 JS ライブラリ・CDN なし）。閉域網で動作します
- AI は **OpenAI 互換 API**（LiteLLM Proxy / vLLM / Ollama など）を `urllib` で呼び出します

要件と設計は [docs/requirements.md](docs/requirements.md)、[docs/implementation-plan.md](docs/implementation-plan.md) を参照してください。

## 起動

```bash
uv run --no-dev python -m reqviz
```

ブラウザで http://127.0.0.1:8765/ を開きます。初回起動時に DB（`data/reqviz.db`）を作成し、
同梱テンプレート「Web三層」（`templates/web-3tier.json`）を登録します。

### 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `REQVIZ_HOST` | `127.0.0.1` | 待ち受けアドレス（共有する場合は `0.0.0.0`） |
| `REQVIZ_PORT` | `8765` | ポート |
| `REQVIZ_DB_PATH` | `data/reqviz.db` | SQLite ファイル |
| `REQVIZ_USER` | `designer` | 操作者名の既定値（画面の設定で上書き可） |
| `REQVIZ_LLM_BASE_URL` | なし | 例: `http://llm-proxy.internal:4000/v1` |
| `REQVIZ_LLM_MODEL` | なし | モデル名 |
| `REQVIZ_LLM_API_KEY` | なし | API キー（環境変数のみで指定。DB・画面には保存しない） |
| `REQVIZ_LLM_TIMEOUT` | `60` | 秒 |

LLM の接続先とモデルは画面の「設定」からも変更できます。LLM が未設定でも、式による計算・制約チェック・承認・版管理はすべて使えます。

## 使い方の流れ

1. **システム作成**: テンプレートから作成すると、レイヤー・パラメータ・ルールがコピーされます
2. **関連図**: 上流（左）から下流（右）へのつながりを確認。ノードをクリックで詳細、Shift + クリックで影響範囲
3. **値の変更**: 上流の値を変えると、下流に「⟳ 要再計算」、承認済みの下流に「▲ 承認後に上流変更」が付きます
4. **再計算**: 「再計算」で再計算案を作成し、確認してから適用します（承認済みの値は下書きに戻ります）
5. **AI 支援**: 妥当性評価・値の提案・ルールの提案・根拠の作成。提案は採用するまで反映されません
6. **承認**: 下書き → AI評価済み（スキップ可） → レビュー中 → 承認済み。差し戻しにはコメントが必要です
7. **版管理**: 全パラメータが承認済みで error 違反がなければ版を作成できます。warning が残る場合は承認時にコメントが必要です
8. **書き出し**: 設計書（HTML）、パラメータシート（CSV）、全体／ルールのみ（JSON、Git での差分管理向け）

## ルールの書き方

| 種別 | 例 |
|---|---|
| 算出式 | `ceil(biz.peak_tps * biz.avg_response_sec * nfr.safety_factor)` |
| 条件分岐 | `0.5 if biz.business_type == 'online' else 0.2` |
| 制約 | `db.pool_per_server * app.server_count + db.reserved_connections <= db.max_connections` |
| 自然言語 | 「バッチ中心の業務では、DB のプール数を抑えること」（AI の評価基準） |

参照は `レイヤーキー.パラメータキー`。使える関数は `ceil floor round min max abs sum`。
式は `ast` によるホワイトリスト方式で評価します（`eval` は使いません）。

## 開発

```bash
uv sync
uv run pytest
```

構成:

```
reqviz/
  engine/     式評価・依存グラフ・再計算・制約・レイアウト（DB 非依存の純粋関数）
  repo/       DB 読み出し、変更履歴・監査ログ
  workflow/   編集・再計算の適用・状態遷移・版・テンプレート・スナップショット
  ai/         OpenAI 互換クライアント、プロンプト、ジョブ、提案の採用
  io/         CSV / HTML 書き出し
  api/        REST API のルーティング
  static/     画面（Vanilla JS / SVG）
templates/    同梱テンプレート
tests/        pytest（engine / workflow / api / ai）
```

## MVP の制限（フェーズ 2 で対応予定）

- ログイン認証・ロール（設計者／承認者）は未実装です。操作者名はブラウザごとの自己申告で、自己承認も可能です（監査ログには記録されます）
- 共有サーバーで使う場合は、信頼できるネットワーク内に限定してください
