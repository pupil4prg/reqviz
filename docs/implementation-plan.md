# システム要件可視化ツール 実装計画（v1.0）

対象の要件: [requirements.md](requirements.md) v0.2

## 1. 技術スタック

| 区分 | 採用 | 備考 |
|---|---|---|
| 言語 | Python 3.12 | 実行時は標準ライブラリのみ |
| Web サーバー | `http.server.ThreadingHTTPServer` | ルーティングは自作 |
| DB | `sqlite3`（WAL モード） | マイグレーションは SQL ファイルを連番で適用 |
| 式評価 | `ast`（ホワイトリスト方式） | `eval` / `exec` は使わない |
| LLM 呼び出し | `urllib.request` | OpenAI 互換 API（LiteLLM Proxy / vLLM / Ollama） |
| フロントエンド | Vanilla JS（ES Modules）、SVG、CSS | ビルド工程なし。外部ライブラリと CDN は使わない |
| テスト | pytest（dev 依存のみ） | `uv run pytest` で実行 |
| 環境管理 | uv | `package = false`（ビルドバックエンドを使わないため、閉域網でも起動できる） |

## 2. ディレクトリ構成

```
パラメータ管理/
├── pyproject.toml              # 実行時の依存なし。dev グループ = pytest
├── .python-version             # 3.12
├── README.md
├── docs/
│   ├── requirements.md
│   └── implementation-plan.md
├── reqviz/
│   ├── __main__.py             # 起動: uv run --no-dev python -m reqviz
│   ├── config.py               # 環境変数と設定ファイルの読み込み
│   ├── server.py               # ThreadingHTTPServer、静的ファイル配信
│   ├── router.py               # パスパターン → ハンドラ、JSON の入出力、例外 → HTTP ステータス
│   ├── db.py                   # 接続、トランザクション、マイグレーション適用
│   ├── migrations/
│   │   └── 001_init.sql
│   ├── repo/                   # DB アクセス層（SQL はここに閉じ込める）
│   │   ├── systems.py  layers.py  params.py  relations.py  rules.py
│   │   ├── baselines.py  templates.py  revisions.py  audit.py  ai_runs.py
│   ├── engine/                 # 純粋関数。DB に依存しない
│   │   ├── expr.py             # 式のパース、検証、評価
│   │   ├── graph.py            # 依存グラフ、トポロジカルソート、循環検出、上流・下流の探索
│   │   ├── calc.py             # 再計算案の生成
│   │   ├── constraints.py      # 制約の評価 → 違反リスト
│   │   └── flags.py            # 要再計算・承認後上流変更の伝播
│   ├── workflow/
│   │   ├── param_state.py      # パラメータの状態遷移表とガード条件
│   │   └── baseline.py         # 版の作成、レビュー条件、承認、復元、差分
│   ├── ai/
│   │   ├── client.py           # OpenAI 互換クライアント（タイムアウト、リトライ）
│   │   ├── prompts.py          # 用途別のプロンプト組み立て
│   │   ├── schema.py           # 応答 JSON の簡易スキーマ検証（自作）
│   │   └── jobs.py             # バックグラウンドスレッドで実行し、状態を ai_runs に保存
│   ├── io/
│   │   ├── export_csv.py  export_json.py  export_html.py  import_json.py
│   ├── api/                    # HTTP ハンドラ（薄く保ち、処理は repo / engine / workflow に委ねる）
│   │   ├── systems.py  params.py  rules.py  graph.py  workflow.py
│   │   ├── baselines.py  ai.py  templates.py  io.py
│   ├── auth/                   # フェーズ2
│   │   └── (sessions.py, passwords.py, csrf.py)
│   └── static/
│       ├── index.html
│       ├── css/app.css
│       └── js/
│           ├── main.js  api.js  router.js  store.js
│           ├── graph/layout.js  graph/render.js  graph/interact.js
│           └── views/  systems.js  graph-view.js  params.js  rules.js
│                       layers.js  approvals.js  baselines.js  history.js
│                       templates.js  settings.js
├── templates/
│   └── web-3tier.json          # 同梱のサンプルテンプレート（§9）
└── tests/
    ├── conftest.py             # 一時 DB、テスト用サーバー（ポート 0）、偽の LLM サーバー
    ├── engine/  repo/  workflow/  ai/  io/  api/
```

**層の依存方向**: `api → workflow → engine / repo`。`engine` は DB と HTTP に依存しない純粋な層とし、テストの中心にする。

## 3. DB スキーマ（001_init.sql の概要）

更新可能なテーブルには、楽観ロック用の `row_version INTEGER` を持たせる。

```sql
CREATE TABLE systems (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
  template_id INTEGER, template_version INTEGER,
  current_baseline_id INTEGER,          -- 最新の承認済みの版
  row_version INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT);

CREATE TABLE layers (
  id INTEGER PRIMARY KEY, system_id INTEGER NOT NULL REFERENCES systems ON DELETE CASCADE,
  key TEXT NOT NULL, name TEXT NOT NULL, sort_order INTEGER NOT NULL,
  row_version INTEGER NOT NULL DEFAULT 1, UNIQUE(system_id, key));

CREATE TABLE params (
  id INTEGER PRIMARY KEY, system_id INTEGER NOT NULL REFERENCES systems ON DELETE CASCADE,
  layer_id INTEGER NOT NULL REFERENCES layers,
  key TEXT NOT NULL,                     -- 式での参照名は「layer.key」
  name TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('number','string','enum','bool')),
  enum_options TEXT,                     -- JSON 配列
  value TEXT,                            -- JSON でエンコードした値
  unit TEXT, source TEXT CHECK(source IN ('manual','formula','ai')),
  rationale TEXT, ref_doc TEXT, owner TEXT,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','ai_evaluated','in_review','approved')),
  ai_skipped INTEGER NOT NULL DEFAULT 0,
  flag_stale INTEGER NOT NULL DEFAULT 0,             -- 要再計算
  flag_upstream_changed INTEGER NOT NULL DEFAULT 0,  -- 承認後に上流変更あり
  proposed_value TEXT,                   -- 再計算案（JSON）、なければ NULL
  row_version INTEGER NOT NULL DEFAULT 1, updated_at TEXT,
  UNIQUE(system_id, layer_id, key));

CREATE TABLE relations (
  id INTEGER PRIMARY KEY, system_id INTEGER NOT NULL REFERENCES systems ON DELETE CASCADE,
  from_param INTEGER NOT NULL REFERENCES params ON DELETE CASCADE,
  to_param INTEGER NOT NULL REFERENCES params ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('derives','constrains','influences')),
  origin TEXT NOT NULL CHECK(origin IN ('auto','manual')),  -- auto = ルールから自動生成
  rule_id INTEGER REFERENCES rules ON DELETE CASCADE, note TEXT);

CREATE TABLE rules (
  id INTEGER PRIMARY KEY, system_id INTEGER NOT NULL REFERENCES systems ON DELETE CASCADE,
  key TEXT NOT NULL,                     -- Git での差分管理用の安定 ID
  kind TEXT NOT NULL CHECK(kind IN ('formula','constraint','nl')),
  target_param INTEGER REFERENCES params,  -- formula の代入先 / nl の対象
  expression TEXT,                       -- 条件分岐は三項演算子で表す
  nl_text TEXT, severity TEXT CHECK(severity IN ('error','warning')),
  description TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  row_version INTEGER NOT NULL DEFAULT 1, UNIQUE(system_id, key));

CREATE TABLE revisions (                 -- 変更履歴（params / rules / layers / relations）
  id INTEGER PRIMARY KEY, system_id INTEGER, entity TEXT, entity_id INTEGER,
  op TEXT CHECK(op IN ('create','update','delete','restore')),
  before_json TEXT, after_json TEXT, actor TEXT, comment TEXT, at TEXT);

CREATE TABLE baselines (
  id INTEGER PRIMARY KEY, system_id INTEGER NOT NULL REFERENCES systems ON DELETE CASCADE,
  version_no INTEGER NOT NULL, label TEXT,
  status TEXT NOT NULL CHECK(status IN ('in_review','approved','rejected')),
  payload TEXT NOT NULL,                 -- 全体の凍結 JSON（§5.5 と同じ形式）
  created_by TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, decision_comment TEXT,
  UNIQUE(system_id, version_no));

CREATE TABLE templates (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
  description TEXT, payload TEXT NOT NULL, created_at TEXT, UNIQUE(name, version));

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY, system_id INTEGER, actor TEXT NOT NULL, action TEXT NOT NULL,
  target_type TEXT, target_id INTEGER, detail_json TEXT, comment TEXT, at TEXT NOT NULL);

CREATE TABLE ai_runs (
  id INTEGER PRIMARY KEY, system_id INTEGER,
  purpose TEXT CHECK(purpose IN ('evaluate','suggest_values','suggest_rules','rationale')),
  status TEXT CHECK(status IN ('queued','running','done','failed')),
  model TEXT, request_json TEXT, response_json TEXT, error TEXT,
  created_at TEXT, finished_at TEXT);

CREATE TABLE ai_suggestions (            -- 提案は自動で反映せず、採用・不採用を記録する
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES ai_runs ON DELETE CASCADE,
  kind TEXT CHECK(kind IN ('finding','value','rule','relation','rationale')),
  target_param INTEGER, payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','adopted','rejected')),
  decided_by TEXT, decided_at TEXT);

CREATE TABLE users (                     -- MVP では既定ユーザー 1 名。本格利用はフェーズ2
  id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, display_name TEXT,
  role TEXT CHECK(role IN ('designer','approver','admin')),
  password_hash TEXT, created_at TEXT);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT);
```

## 4. API 一覧（JSON / REST）

更新系の API には `row_version` を必ず渡し、一致しなければ `409 Conflict` を返す。エラーは `{"error": {"code", "message", "details"}}` の形式で統一する。

| メソッド | パス | 用途 | 対応 FR |
|---|---|---|---|
| GET / POST | `/api/systems` | 一覧・作成（`template_id` 指定でテンプレートから作成） | FR-01, 70 |
| GET / PUT / DELETE | `/api/systems/{sid}` | 取得・更新・削除 | FR-01 |
| POST | `/api/systems/{sid}/clone` | 複製 | FR-01 |
| GET / POST | `/api/systems/{sid}/layers` | レイヤー | FR-02 |
| PUT / DELETE | `/api/layers/{id}` | | FR-02 |
| POST | `/api/systems/{sid}/layers/reorder` | 並べ替え | FR-02 |
| GET / POST | `/api/systems/{sid}/params` | パラメータ | FR-03 |
| GET / PUT / DELETE | `/api/params/{id}` | 値の更新時に下流へフラグを伝播する | FR-03, 20 |
| GET / POST | `/api/systems/{sid}/relations` | 手動の関連 | FR-04 |
| DELETE | `/api/relations/{id}` | | FR-04 |
| GET / POST | `/api/systems/{sid}/rules` | ルール | FR-10 |
| PUT / DELETE | `/api/rules/{id}` | 保存時に検証し、関連を自動生成し直す | FR-10, 11 |
| POST | `/api/systems/{sid}/rules/validate` | 保存前の式検証（エディタからの即時チェック） | FR-11 |
| GET | `/api/systems/{sid}/graph` | ノード、エッジ、フラグ、違反をまとめて返す | FR-60, 63 |
| GET | `/api/params/{id}/impact?dir=up\|down` | 影響範囲 | FR-62 |
| GET | `/api/systems/{sid}/violations` | 制約違反の一覧 | FR-22 |
| POST | `/api/systems/{sid}/recalc` | 再計算案を生成（body で対象: 単体 / 下流 / 全体） | FR-21 |
| POST | `/api/params/{id}/apply-proposal` | 再計算案を適用（承認済みなら下書きに戻す） | FR-21, 45 |
| POST | `/api/params/{id}/transition` | 状態遷移（`action`: ai_evaluate / skip_ai / request_review / approve / reject / ack_upstream） | FR-40〜43 |
| GET | `/api/systems/{sid}/approvals` | 承認キュー | FR-40 |
| GET / POST | `/api/systems/{sid}/baselines` | 版の一覧・作成（作成と同時にレビューに出す。条件を満たさなければ 422 で理由を返す） | FR-44 |
| GET | `/api/baselines/{id}` | 版の内容 | FR-44 |
| POST | `/api/baselines/{id}/approve` / `reject` | 版の承認・差し戻し（warning 違反があればコメント必須） | FR-44 |
| POST | `/api/baselines/{id}/restore` | 作業中の状態をこの版に戻す | FR-51 |
| GET | `/api/systems/{sid}/diff?from=&to=` | 版同士、または版と作業中（`to=working`）の差分 | FR-52 |
| GET | `/api/systems/{sid}/revisions` | 変更履歴（絞り込み: entity / entity_id） | FR-50 |
| GET | `/api/systems/{sid}/audit` | 監査ログ | FR-42 |
| POST | `/api/systems/{sid}/ai/{purpose}` | AI ジョブを起動し、`run_id` を返す | FR-30〜33 |
| GET | `/api/ai/runs/{id}` | ジョブの状態と結果（ポーリング用） | FR-30〜33 |
| POST | `/api/ai/suggestions/{id}/adopt` / `reject` | 提案の採用・不採用 | FR-34 |
| GET / POST | `/api/templates` | 一覧・作成（システムから作成 / JSON から取り込み） | FR-70, 72 |
| GET | `/api/templates/{id}/export` | テンプレートの JSON 書き出し | FR-72 |
| GET | `/api/systems/{sid}/export?format=csv\|json\|rules-json\|html` | 書き出し | FR-12, 71 |
| POST | `/api/systems/{sid}/import` | JSON の取り込み（ルールのみ、または全体） | FR-12, 72 |
| GET / PUT | `/api/settings/llm` | LLM 接続設定（API キーは扱わない） | §7 |

## 5. 主要ロジックの設計

### 5.1 式評価器（engine/expr.py）
- 処理の流れ: `parse(text) → ast.Expression` → `validate(tree, symbols)` で許可されたノードか確認し、参照キーを抽出して型を検査する → `evaluate(tree, env)`
- 許可するノード: `Constant`, `Name`, `Attribute`（`layer.key` の形だけ）, `BinOp`, `UnaryOp`, `BoolOp`, `Compare`, `IfExp`, `Call`（関数ホワイトリストのみ）
- 安全対策: 式の長さ上限（1,000文字）、AST の深さ上限、`**` の指数上限（例: 100）、0除算は評価エラーとして扱う
- `references(text) -> set[str]` で参照キーを抽出する。関連の自動生成と依存グラフの構築に使う

### 5.2 依存グラフ（engine/graph.py）
- formula ルールから「参照元 → target」のエッジを作る。トポロジカルソートは Kahn 法で行い、循環があれば循環しているパスを返してエラーにする
- 影響範囲の探索（`upstream` / `downstream`）は BFS で行い、formula・constraint・influences のすべての関連を対象にする

### 5.3 フラグ伝播と再計算（engine/flags.py, engine/calc.py）
1. パラメータの値を更新すると、formula 依存の下流に**推移的に** `flag_stale = 1` を付ける。下流が承認済みなら `flag_upstream_changed = 1` も付ける
2. 再計算を指示すると、トポロジカル順に評価する。上流に未適用の案がある場合は、その案の値を使って連鎖的に計算し、`proposed_value` に保存する（この時点では値を変えない）
3. 案を適用すると、`value` を更新し、`stale` を解除して、変更履歴を記録する。承認済みだったパラメータは `draft` に戻し、監査ログに記録する（FR-45）
4. 再計算の結果が現在の値と同じ場合は、案を作らずに `stale` だけ解除する

### 5.4 状態遷移（workflow/param_state.py）
遷移表で定義し、表にない遷移は 422 エラーにする。

| 現在の状態 | action | 次の状態 | ガード条件・副作用 |
|---|---|---|---|
| draft | ai_evaluate | ai_evaluated | AI 評価ジョブが完了していること |
| draft | skip_ai | ai_evaluated | `ai_skipped = 1`、監査ログに記録 |
| ai_evaluated | request_review | in_review | `stale = 0`、error 違反に関与していないこと |
| in_review | approve | approved | 監査ログに記録。`upstream_changed` を解除 |
| in_review | reject | draft | コメント必須 |
| approved | （値の編集 / 案の適用） | draft | 監査ログに記録 |
| approved | ack_upstream | approved | 上流の変更を確認済みとして `upstream_changed` を解除（コメント必須） |

### 5.5 版（workflow/baseline.py）
- **凍結形式**: layers / params / rules / relations をキーの順に並べた JSON。ルールの JSON 書き出し形式と共通にする
- **作成時の条件**: 全パラメータが approved、error 違反が 0 件、`stale` と `upstream_changed` が 0 件。満たさなければ 422 で理由を一覧にして返す
- **承認**: warning 違反が残っていればコメント必須。承認したら `systems.current_baseline_id` を更新する
- **復元**: 1つのトランザクションで作業中の状態を payload に置き換える。行ごとに `op = 'restore'` の変更履歴を残す
- **差分**: パラメータとルールを `key` で突き合わせ、追加・削除・変更をフィールド単位で比較する

### 5.6 AI 連携（ai/）
- **クライアント**: `POST {base_url}/chat/completions` を呼ぶ。`response_format: json_object` を指定し、使えないモデルではプロンプトで JSON 出力を指示する。タイムアウトは設定可能（既定 60 秒）で、リトライは 1 回
- **ジョブ**: `threading.Thread` で実行し、`ai_runs` の状態を更新する。画面側は 1.5 秒ごとにポーリングする
- **入力**: 対象サブグラフ（対象ノードとその上流・下流）、パラメータの値・単位・根拠、関連ルール（自然言語ルールを含む）、制約違反の状況
- **出力スキーマ**（例: evaluate）: `{"findings": [{"param": "db.pool_size", "severity": "warning", "message": "...", "rule": "nl-003"}]}`
- 検証に失敗した応答は `failed` とし、生の応答を保存してエラー表示する
- 設定項目は `REQVIZ_LLM_BASE_URL`、`REQVIZ_LLM_MODEL`、`REQVIZ_LLM_API_KEY`（環境変数）。API キーは DB にも画面にも出さない

### 5.7 グラフ描画（static/js/graph/）
- **レイアウト**（簡易な Sugiyama 法）:
  1. x 座標はレイヤーの順序で決める
  2. 各レイヤー内の y 方向の並びは、重心法で上下方向に 4 往復スイープして交差を減らす
  3. 同じレイヤー内のエッジは弧で描く
  4. ユーザーがドラッグで動かした位置はブラウザ側（localStorage）に保存する
- **描画**: SVG。エッジは3次ベジェ曲線で描き、種別ごとに線種を変える（derives = 実線、constrains = 破線、influences = 点線）
- **操作**: viewBox の書き換えでパン・ズーム。クリックで詳細パネル、Shift + クリックで影響範囲を強調、検索とレイヤー絞り込み
- **色分け**: 違反 = 赤、要再計算 = 橙、承認後に上流変更あり = 紫、承認済み = 緑、レビュー中 = 青、下書き = 灰。CSS 変数で定義する
- **版との差分表示モード**: 現行ベースラインから変わったノードに枠線を付ける

### 5.8 書き出し（io/）
- **CSV**: パラメータシート（レイヤー、キー、名前、値、単位、状態、根拠、算出式）。Excel で開けるよう UTF-8 BOM 付きにする
- **JSON**: `indent=2`、`ensure_ascii=False`、`sort_keys=True` で書き出し、Git の差分を安定させる。ID ではなく `key` で参照する
- **HTML**: 単一ファイルの設計書。グラフの SVG はサーバー側で生成して埋め込み、レイアウトアルゴリズムは Python 版も実装する

## 6. 実装マイルストーン

各マイルストーンで、テストが通る状態を保ったまま進める。

| # | マイルストーン | 内容 | 完了条件 |
|---|---|---|---|
| M0 | 基盤 | `uv init`、pyproject（`package = false`、dev = pytest）、サーバーの骨組み、ルーター、静的ファイル配信、マイグレーション、`conftest.py` | `uv run python -m reqviz` で空の画面が表示される。`uv run pytest` が通る |
| M1 | エンジン | expr / graph / calc / constraints / flags | 単体テストで分岐網羅率 90% 以上。循環検出、0除算、不正な構文を網羅する |
| M2 | データ API | systems / layers / params / relations / rules の CRUD、変更履歴、楽観ロック | API テスト（実サーバーをポート 0 で起動） |
| M3 | グラフ表示 | レイアウト、描画、詳細パネル、影響範囲、色分け、パン・ズーム | 100 ノードのサンプルで描画が 1 秒以内 |
| M4 | 編集画面 | パラメータ一覧の編集、ルールエディタ（即時検証）、レイヤー設定、再計算と案の適用、違反一覧 | §9 のサンプルを画面から作れる |
| M5 | パラメータの承認 | 状態遷移、承認キュー、監査ログ | 遷移表の全パターンをテスト |
| M6 | 版管理 | 版の作成と条件チェック、承認・差し戻し、差分、復元 | 条件の各分岐、復元後の整合性をテスト |
| M7 | AI 連携 | クライアント、ジョブ、4種類の用途、提案の採用・不採用 | 偽の LLM サーバー（正常 / 不正な JSON / タイムアウト）でテスト |
| M8 | テンプレートと入出力 | テンプレートの作成とシステム作成、CSV / JSON / HTML の書き出し、JSON の取り込み | 書き出し → 取り込みで内容が一致する |
| M9 | 仕上げ | 性能確認、同梱テンプレート、README（閉域網への導入手順）、エラー表示の改善 | 受け入れシナリオ（§8）を通しで確認 |
| P2 | フェーズ2 | ログイン（PBKDF2、セッション Cookie、CSRF）、ロールによる認可、承認者と設計者の分離、競合時の画面表示 | 認可マトリクスのテスト |

## 7. テスト方針

| 層 | 方法 |
|---|---|
| engine | 純粋関数の単体テスト。`pytest.mark.parametrize` で境界値を網羅する |
| repo / workflow | `tmp_path` に作った SQLite を使う |
| api | `ThreadingHTTPServer` をポート 0 で別スレッドに起動し、`urllib` で叩く |
| ai | `http.server` で作った偽の OpenAI 互換サーバーで応答を差し替える |
| io | ゴールデンファイルとの比較、および書き出し → 取り込みの往復テスト |
| フロントエンド | M3 以降、ブラウザでの手動確認チェックリスト（自動 E2E は対象外） |

実行は `uv run pytest`（カバレッジは必要に応じて `uv run --with pytest-cov pytest --cov=reqviz`）。

## 8. 受け入れシナリオ（MVP）

1. テンプレート「Web三層」からシステムを作り、グラフを表示する
2. 上流の `biz.peak_tps` を変えると、下流が橙（要再計算）になり、承認済みのノードは紫になる
3. 再計算を指示して案を確認し、適用すると値が更新され、承認済みだったノードは下書きに戻る
4. 制約違反が赤く表示され、違反一覧に出る
5. AI 評価を実行すると指摘が表示され、提案を採用すると反映されて source=ai として記録される
6. AI 評価をスキップしてレビューに出し、承認すると監査ログに残る
7. 全パラメータを承認して版を作成し、承認する（warning 違反が残っていればコメント必須）
8. 値を変更してから過去の版との差分を見て、復元する
9. ルールを JSON に書き出し、別のシステムに取り込む。CSV と HTML でも書き出せる

## 9. 同梱テンプレート「Web三層」（テスト用データを兼ねる）

| レイヤー | パラメータ | ルール |
|---|---|---|
| 業務要件 | `biz.business_type`（enum: online / batch）、`biz.peak_tps`、`biz.avg_response_sec` | — |
| 非機能要件 | `nfr.safety_factor` | — |
| アプリ | `app.concurrent_requests` | `ceil(biz.peak_tps * biz.avg_response_sec * nfr.safety_factor)`（リトルの法則） |
| | `app.server_count`、`app.threads_per_server` | `ceil(app.concurrent_requests / app.server_count)` |
| ミドルウェア / DB | `db.pool_ratio` | `0.5 if biz.business_type == "online" else 0.2`（条件分岐） |
| | `db.pool_per_server` | `ceil(app.threads_per_server * db.pool_ratio)` |
| | `db.max_connections`、`db.reserved_connections` | 制約(error): `db.pool_per_server * app.server_count + db.reserved_connections <= db.max_connections` |
| OS | `os.max_user_processes` | 制約(warning): `os.max_user_processes >= app.threads_per_server * 2` |
| | — | 自然言語: 「バッチ中心の業務では、DB のプール数を抑えて DB 側のリソース競合を避けること」 |

## 10. 運用・導入（閉域網）

- 必要なもの: Python 3.12 と uv。実行時はサードパーティの依存がないため、`uv run --no-dev python -m reqviz` はネットワークなしで起動できる
- pytest はインターネットに接続できる開発環境でのみ使う。閉域網でテストする場合は wheel を持ち込むか、`uv` のキャッシュを持ち込む
- 起動時の設定: `REQVIZ_HOST`、`REQVIZ_PORT`、`REQVIZ_DB_PATH`、`REQVIZ_LLM_*`
- バックアップ: SQLite の DB ファイルをコピーする（`sqlite3` の backup API を使うコマンドも用意する）

## 11. リスクと対策

| リスク | 対策 |
|---|---|
| 自作のグラフレイアウトの品質 | 100 ノード以下に絞る。手動で位置を調整・保存できるようにする。M3 で早めに確認する |
| LLM の JSON 出力が不安定 | スキーマ検証とリトライ1回。失敗時は生の応答を保存して表示する |
| 式言語の表現力不足 | 関数のホワイトリストを設定で拡張できるようにする（追加は Python のコード側で行う） |
| 共有サーバーでの同時編集（フェーズ2） | MVP の段階から楽観ロックと `row_version` を組み込んでおく |

## 12. 実装時の変更点（v1.0 → 実装）

| 項目 | 計画 | 実装 | 理由 |
|---|---|---|---|
| グラフのレイアウト計算 | JS で実装 | Python（`engine/layout.py`）で計算し、API で座標を返す | HTML 設計書の SVG と画面で同じ配置を使うため。実装が 1 か所で済む |
| 関連（relations） | ルールから自動生成した関連も保存 | 手動の関連だけを保存。算出式・制約の関連は表示時にルールから導出 | 二重管理をなくし、ルール変更時の整合性問題を避けるため |
| 制約の表示 | エッジのみ | 制約を小さな「ルールノード」として描画し、関与するパラメータからエッジを引く | 3 つ以上のパラメータが関わる制約を表現するため |
| 影響範囲 API | `GET /api/params/{id}/impact` | 画面側でエッジをたどって計算 | 100 ノード以下では即時に計算でき、通信が不要なため |
| モジュール構成 | `repo/` を細かく分割 | `repo/model.py`（読み出し）と `repo/history.py` に集約し、更新処理は `workflow/` に置く | 更新時にフラグ伝播と履歴記録を必ず通すため |
| 一括操作 | なし | `POST /api/systems/{sid}/transition`（複数パラメータの状態遷移）、`POST /api/systems/{sid}/apply-proposals` | 一覧画面での一括処理のため |
| AI 評価の状態遷移 | 手動の `ai_evaluate` 操作 | 妥当性評価ジョブの完了時に、対象の下書きを自動で「AI評価済み」にする | 操作の手間を減らすため |
