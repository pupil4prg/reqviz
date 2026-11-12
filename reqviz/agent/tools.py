"""エージェント向けツール定義。MCP と A2A の両方が、この表を通じて REST API を呼び出す。

各ツールは REST API の 1 操作に対応する。引数のうち、パスの ``{name}`` に現れるものはパスに、
``query`` に挙げたものはクエリ文字列に、残りは JSON 本文に入る。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlencode

# ---------------------------------------------------------------- API クライアント


@dataclass
class ApiResult:
    status: int
    data: Any  # JSON として解釈できればその値、できなければ文字列
    content_type: str = "application/json"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error_message(self) -> str:
        if isinstance(self.data, dict) and isinstance(self.data.get("error"), dict):
            err = self.data["error"]
            msg = str(err.get("message") or "")
            if err.get("details"):
                msg += " / " + json.dumps(err["details"], ensure_ascii=False)
            return msg
        return f"HTTP {self.status}"


class ApiClient(Protocol):
    def request(self, method: str, path: str, body: Any = None, actor: str | None = None) -> ApiResult: ...


def _decode(status: int, content_type: str, raw: bytes) -> ApiResult:
    text = raw.decode("utf-8", "replace")
    if "json" in content_type:
        try:
            return ApiResult(status, json.loads(text) if text else None, content_type)
        except ValueError:
            pass
    return ApiResult(status, text, content_type)


class InProcessClient:
    """同じプロセス内の REST API を、HTTP を経由せずに呼び出す。"""

    def __init__(self, app: Any):
        self.app = app

    def request(self, method: str, path: str, body: Any = None, actor: str | None = None) -> ApiResult:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            principal = self.app.internal_principal(actor)
        except Exception as e:  # アカウントが無効化された場合など
            return ApiResult(401, {"error": {"code": "unauthorized", "message": str(e), "details": None}})
        res = self.app.handle(method, path, {}, raw, principal)
        return _decode(res.status, res.content_type, res.body)


class HttpClient:
    """起動中の reqviz サーバーの REST API を urllib で呼び出す。"""

    def __init__(self, base_url: str, timeout: float = 30.0, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token  # API トークン（サーバーでログイン認証が有効な場合に必要）

    def request(self, method: str, path: str, body: Any = None, actor: str | None = None) -> ApiResult:
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Reqviz-User"] = quote(actor)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return _decode(res.status, res.headers.get("Content-Type", ""), res.read())
        except urllib.error.HTTPError as e:
            return _decode(e.code, e.headers.get("Content-Type", ""), e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return ApiResult(503, {"error": {"code": "unreachable", "message": f"reqviz サーバーに接続できません（{self.base_url}）: {e}", "details": None}})


# ---------------------------------------------------------------- ツール定義


@dataclass
class Tool:
    name: str
    description: str
    method: str
    path: str
    props: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    query: tuple[str, ...] = ()
    read_only: bool = True
    destructive: bool = False

    @property
    def path_args(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.path)

    @property
    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": self.props}
        req = [*self.path_args, *[r for r in self.required if r not in self.path_args]]
        if req:
            schema["required"] = req
        return schema


def _s(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def _i(desc: str) -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _b(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _arr(desc: str, items: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "array", "description": desc, "items": items or {"type": "string"}}


SID = {"system_id": _i("システム ID（list_systems で確認）")}
COMMENT = {"comment": _s("変更理由。履歴・監査ログに残る")}
ROWV = {"row_version": _i("楽観ロック用。取得時の row_version を渡すと、他者の更新と衝突したときに 409 になる")}
REF_HELP = "参照名は「レイヤーキー.キー」（例: app.server_count）"

_ALIAS = {
    "type": "object",
    "properties": {"label": _s("別名の種類（例: 実装名、論理名、英語名）"), "value": _s("別名（例: max_connections）")},
    "required": ["value"],
}
_PARAM_BODY = {
    "name": _s("表示名"),
    "aliases": _arr("別名の一覧。実装上のパラメータ名や論理名など（指定すると全体を置き換える）", _ALIAS),
    "type": _s("型", enum=["number", "string", "enum", "bool"]),
    "enum_options": _arr("type=enum のときの選択肢"),
    "value": {"description": "値（型に合わせる。未設定は null）"},
    "unit": _s("単位"),
    "rationale": _s("根拠"),
    "ref_doc": _s("出典"),
    "owner": _s("担当"),
}
_OPTION = {
    "type": "object",
    "properties": {
        "key": _s("選択肢のキー（式から比較に使う値）"),
        "label": _s("表示名"),
        "description": _s("説明"),
        "pros": _arr("長所"),
        "cons": _arr("短所"),
        "attributes": {"type": "object", "description": "任意の付加情報"},
    },
    "required": ["key"],
}
_ITEM_BODY = {
    "name": _s("表示名"),
    "options": _arr("kind=choice: 選択肢の一覧", _OPTION),
    "selected": {"type": ["string", "null"], "description": "kind=choice: 採用する選択肢のキー（未決定は null）"},
    "content_type": _s("kind=document: 本文の形式", enum=["text", "markdown", "json", "yaml"]),
    "content": {"description": "kind=document: 本文。json 形式なら任意の JSON 値、それ以外は文字列"},
    "tags": _arr("タグ"),
    "rationale": _s("根拠・選定理由"),
    "ref_doc": _s("出典"),
    "owner": _s("担当"),
}
_RULE_BODY = {
    "kind": _s("種別", enum=["formula", "constraint", "nl"]),
    "target": _s("formula の代入先 / nl の対象。" + REF_HELP),
    "expression": _s("formula / constraint の式。例: ceil(biz.peak_tps * nfr.safety_factor)、app.session_store == 'redis'"),
    "nl_text": _s("kind=nl: 自然言語のルール本文"),
    "severity": _s("制約の重要度", enum=["error", "warning"]),
    "description": _s("説明"),
    "enabled": _b("有効か"),
}
_POINT = "時点。ISO 8601 の日時（例: 2026-11-01T09:00:00+09:00）または rev:<リビジョンID>"

TOOLS: list[Tool] = [
    # ---- 参照
    Tool("list_systems", "管理対象のシステム一覧（ID・名前・パラメータ数・現行の版）", "GET", "/api/systems"),
    Tool("get_system", "システム 1 件の情報", "GET", "/api/systems/{system_id}", {**SID}),
    Tool("list_layers", "レイヤー（上流→下流の階層）の一覧", "GET", "/api/systems/{system_id}/layers", {**SID}),
    Tool("list_params", "パラメータ一覧（値・単位・状態・フラグ・再計算案）", "GET", "/api/systems/{system_id}/params", {**SID}),
    Tool("get_param", "パラメータ 1 件の詳細（変更履歴・監査ログ・レビュー可否つき）", "GET", "/api/params/{param_id}", {"param_id": _i("パラメータ ID")}),
    Tool("get_param_by_ref", "参照名でパラメータを取得する。" + REF_HELP, "GET", "/api/systems/{system_id}/params/by-ref/{ref}", {**SID, "ref": _s("参照名")}),
    Tool("lookup_params", "名前からパラメータを引く。参照名・キー・表示名・別名（実装上のパラメータ名など）のいずれかに完全一致するものをすべて返す",
         "GET", "/api/systems/{system_id}/params/lookup", {**SID, "name": _s("探す名前（例: max_connections）")}, required=("name",), query=("name",)),
    Tool("list_items", "構成項目の一覧。choice = 実装方式などの選択肢（列挙データ）、document = 不定形データ", "GET",
         "/api/systems/{system_id}/items", {**SID, "kind": _s("種別で絞り込む", enum=["choice", "document"])}, query=("kind",)),
    Tool("get_item", "構成項目 1 件の詳細（版の系列つき）", "GET", "/api/items/{item_id}", {"item_id": _i("構成項目 ID")}),
    Tool("get_item_by_ref", "参照名で構成項目を取得する。" + REF_HELP, "GET", "/api/systems/{system_id}/items/by-ref/{ref}", {**SID, "ref": _s("参照名")}),
    Tool("list_rules", "ルール一覧（算出式・制約・自然言語）と検証エラー", "GET", "/api/systems/{system_id}/rules", {**SID}),
    Tool("list_relations", "手動で登録した関連（影響関係）の一覧", "GET", "/api/systems/{system_id}/relations", {**SID}),
    Tool("get_graph", "関連図のデータ（ノード・エッジ・違反・座標）。依存関係をたどるときに使う", "GET", "/api/systems/{system_id}/graph", {**SID}),
    Tool("get_violations", "制約違反の一覧", "GET", "/api/systems/{system_id}/violations", {**SID}),
    Tool("get_approvals", "承認待ち（レビュー中のパラメータ・構成項目・版、承認後に上流が変わったもの）", "GET", "/api/systems/{system_id}/approvals", {**SID}),
    Tool("list_baselines", "版（ベースライン）の一覧と、新しい版を作成できるかの判定", "GET", "/api/systems/{system_id}/baselines", {**SID}),
    Tool("get_baseline", "版 1 件（凍結した全内容つき）", "GET", "/api/baselines/{baseline_id}", {"baseline_id": _i("版 ID")}),
    Tool("diff_baselines", "版同士、または版と作業中の状態の差分", "GET", "/api/systems/{system_id}/diff",
         {**SID, "from": _s("比較元: working / current / 版 ID"), "to": _s("比較先: working / current / 版 ID")}, query=("from", "to")),
    Tool("list_revisions", "変更履歴（新しい順）", "GET", "/api/systems/{system_id}/revisions",
         {**SID, "entity": _s("対象の種類", enum=["param", "item", "rule", "layer", "relation", "system"]),
          "key": _s("参照名またはルールキー"), "limit": _i("件数（既定 200）")}, query=("entity", "key", "limit")),
    Tool("get_revision", "リビジョン 1 件（変更前後の内容）", "GET", "/api/revisions/{revision_id}", {"revision_id": _i("リビジョン ID")}),
    Tool("get_timeline", "1 つのパラメータ・構成項目・ルールの版の系列（版番号つき、名前変更もさかのぼる）", "GET",
         "/api/systems/{system_id}/history/timeline",
         {**SID, "entity": _s("対象の種類", enum=["param", "item", "rule"]), "key": _s("参照名またはルールキー")},
         required=("entity", "key"), query=("entity", "key")),
    Tool("get_state_at", "過去の時点のパラメータ・構成項目・ルールの内容を再現する", "GET", "/api/systems/{system_id}/history/state",
         {**SID, "at": _s(_POINT)}, required=("at",), query=("at",)),
    Tool("diff_history", "2 つの時点の間で変わった内容（to を省略すると現在まで）", "GET", "/api/systems/{system_id}/history/diff",
         {**SID, "from": _s(_POINT), "to": _s(_POINT)}, required=("from",), query=("from", "to")),
    Tool("list_audit", "監査ログ（承認・差し戻し・再計算・復元などの操作記録）", "GET", "/api/systems/{system_id}/audit",
         {**SID, "key": _s("対象の参照名で絞り込む"), "limit": _i("件数（既定 200）")}, query=("key", "limit")),
    Tool("export_system", "システム全体を書き出す（json = 全体、rules-json = ルールのみ、csv = パラメータ表、html = 設計書）", "GET",
         "/api/systems/{system_id}/export", {**SID, "format": _s("形式", enum=["json", "rules-json", "csv", "html"])}, query=("format",)),
    Tool("list_templates", "テンプレート（ひな形）の一覧", "GET", "/api/templates"),
    Tool("validate_rule", "ルール案を保存せずに検証する", "POST", "/api/systems/{system_id}/rules/validate",
         {**SID, "key": _s("ルールキー"), **_RULE_BODY}, required=("key", "kind")),
    Tool("api_catalog", "REST API の全ルート一覧。専用ツールがない操作は api_request で呼べる", "GET", "/api"),

    # ---- システム・レイヤー
    Tool("create_system", "システムを作成する（template_id を渡すとテンプレートから作成）", "POST", "/api/systems",
         {"name": _s("システム名"), "description": _s("説明"), "template_id": _i("テンプレート ID")}, required=("name",), read_only=False),
    Tool("update_system", "システムの名前・説明を変更する", "PUT", "/api/systems/{system_id}",
         {**SID, "name": _s("システム名"), "description": _s("説明")}, read_only=False),
    Tool("clone_system", "システムを複製する", "POST", "/api/systems/{system_id}/clone", {**SID, "name": _s("複製先の名前")}, read_only=False),
    Tool("delete_system", "システムを削除する（配下のデータと版もすべて消える。取り消せない）", "DELETE", "/api/systems/{system_id}", {**SID},
         read_only=False, destructive=True),
    Tool("create_layer", "レイヤーを追加する", "POST", "/api/systems/{system_id}/layers",
         {**SID, "key": _s("レイヤーキー（英数字）"), "name": _s("表示名")}, required=("key", "name"), read_only=False),
    Tool("update_layer", "レイヤーのキー・名前を変更する（キーを変えるとルールの式も追従する）", "PUT", "/api/layers/{layer_id}",
         {"layer_id": _i("レイヤー ID"), "key": _s("レイヤーキー"), "name": _s("表示名")}, read_only=False),
    Tool("delete_layer", "空のレイヤーを削除する", "DELETE", "/api/layers/{layer_id}", {"layer_id": _i("レイヤー ID")}, read_only=False, destructive=True),

    # ---- パラメータ
    Tool("create_param", "パラメータを作成する", "POST", "/api/systems/{system_id}/params",
         {**SID, "layer_id": _i("レイヤー ID"), "key": _s("キー（英数字）"), **_PARAM_BODY}, required=("layer_id", "key", "name"), read_only=False),
    Tool("update_param", "パラメータを更新する。値を変えると下書きに戻り、下流に要再計算が付く", "PUT", "/api/params/{param_id}",
         {"param_id": _i("パラメータ ID"), "key": _s("キー"), "layer_id": _i("レイヤー ID"), **_PARAM_BODY, **COMMENT, **ROWV}, read_only=False),
    Tool("delete_param", "パラメータを削除する（ルールから参照されていると失敗する）", "DELETE", "/api/params/{param_id}",
         {"param_id": _i("パラメータ ID")}, read_only=False, destructive=True),
    Tool("transition_param", "パラメータの状態遷移（下書き→AI評価済み→レビュー中→承認済み）", "POST", "/api/params/{param_id}/transition",
         {"param_id": _i("パラメータ ID"),
          "action": _s("操作", enum=["skip_ai", "request_review", "approve", "reject", "withdraw", "ack_upstream"]),
          "comment": _s("コメント（reject / ack_upstream では必須）")}, required=("action",), read_only=False),

    # ---- 構成項目
    Tool("create_item", "構成項目を作成する。kind=choice は選択肢と採用案、kind=document は不定形の本文を持つ", "POST",
         "/api/systems/{system_id}/items",
         {**SID, "layer": _s("レイヤーキー（layer_id の代わりに指定できる）"), "layer_id": _i("レイヤー ID"), "key": _s("キー（英数字）"),
          "kind": _s("種別", enum=["choice", "document"]), **_ITEM_BODY, **COMMENT},
         required=("key", "name", "kind"), read_only=False),
    Tool("update_item", "構成項目を更新する（採用案の変更は selected を渡す）。内容を変えると下書きに戻る", "PUT", "/api/items/{item_id}",
         {"item_id": _i("構成項目 ID"), "key": _s("キー"), "layer": _s("レイヤーキー"), **_ITEM_BODY, **COMMENT, **ROWV}, read_only=False),
    Tool("delete_item", "構成項目を削除する（ルールから参照されていると失敗する）", "DELETE", "/api/items/{item_id}",
         {"item_id": _i("構成項目 ID")}, read_only=False, destructive=True),
    Tool("transition_item", "構成項目の状態遷移（下書き→レビュー中→承認済み）", "POST", "/api/items/{item_id}/transition",
         {"item_id": _i("構成項目 ID"), "action": _s("操作", enum=["request_review", "approve", "reject", "withdraw"]),
          "comment": _s("コメント（reject では必須）")}, required=("action",), read_only=False),

    # ---- ルール・関連
    Tool("create_rule", "ルールを作成する", "POST", "/api/systems/{system_id}/rules",
         {**SID, "key": _s("ルールキー"), **_RULE_BODY}, required=("key", "kind"), read_only=False),
    Tool("update_rule", "ルールを更新する", "PUT", "/api/rules/{rule_id}", {"rule_id": _i("ルール ID"), "key": _s("ルールキー"), **_RULE_BODY, **ROWV}, read_only=False),
    Tool("delete_rule", "ルールを削除する", "DELETE", "/api/rules/{rule_id}", {"rule_id": _i("ルール ID")}, read_only=False, destructive=True),
    Tool("create_relation", "パラメータ間の関連（影響関係）を登録する", "POST", "/api/systems/{system_id}/relations",
         {**SID, "from_ref": _s("影響元の参照名"), "to_ref": _s("影響先の参照名"), "note": _s("メモ")}, required=("from_ref", "to_ref"), read_only=False),
    Tool("delete_relation", "関連を削除する", "DELETE", "/api/relations/{relation_id}", {"relation_id": _i("関連 ID")}, read_only=False, destructive=True),

    # ---- 計算
    Tool("recalc", "算出式を評価して再計算案を作る（値は変えない）。mode: stale = 要再計算とその下流、downstream = refs の下流、all = すべて", "POST",
         "/api/systems/{system_id}/recalc",
         {**SID, "mode": _s("対象範囲", enum=["stale", "downstream", "all"]), "refs": _arr("mode=downstream の起点の参照名")}, read_only=False),
    Tool("apply_proposals", "再計算案を値に反映する（承認済みのものは下書きに戻る）。refs 省略時は全件", "POST",
         "/api/systems/{system_id}/apply-proposals", {**SID, "refs": _arr("反映する参照名"), **COMMENT}, read_only=False),

    # ---- 版・履歴
    Tool("create_baseline", "現在の状態を凍結して版を作り、レビューに出す（全項目が承認済みで違反がないこと）", "POST",
         "/api/systems/{system_id}/baselines", {**SID, "label": _s("版のラベル")}, read_only=False),
    Tool("approve_baseline", "版を承認する（warning の違反が残る場合はコメント必須）", "POST", "/api/baselines/{baseline_id}/approve",
         {"baseline_id": _i("版 ID"), **COMMENT}, read_only=False),
    Tool("reject_baseline", "版を差し戻す（コメント必須）", "POST", "/api/baselines/{baseline_id}/reject",
         {"baseline_id": _i("版 ID"), **COMMENT}, required=("comment",), read_only=False),
    Tool("restore_baseline", "作業中の状態を、過去の版の内容で丸ごと置き換える", "POST", "/api/baselines/{baseline_id}/restore",
         {"baseline_id": _i("版 ID"), **COMMENT}, read_only=False, destructive=True),
    Tool("restore_revision", "パラメータ・構成項目・ルール 1 件を、指定リビジョン直後の内容に戻す（削除のリビジョンなら作り直す）", "POST",
         "/api/revisions/{revision_id}/restore", {"revision_id": _i("リビジョン ID"), **COMMENT}, read_only=False),
    Tool("import_system", "書き出した JSON（reqviz/system または reqviz/rules 形式）を取り込む。system 形式は作業中の状態を置き換える", "POST",
         "/api/systems/{system_id}/import",
         {**SID, "payload": {"type": "object", "description": "取り込む JSON"}, "keep_status": _b("承認状態も取り込むか")},
         required=("payload",), read_only=False, destructive=True),

    # ---- 汎用
    Tool("api_request", "REST API を直接呼び出す（専用ツールがない操作用。ルートは api_catalog で確認）", "POST", "",
         {"method": _s("HTTP メソッド", enum=["GET", "POST", "PUT", "DELETE"]), "path": _s("パス。/api/ で始まる（クエリ文字列を含めてよい）"),
          "body": {"type": "object", "description": "JSON 本文"}}, required=("method", "path"), read_only=False, destructive=True),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def is_read_only(name: str, args: dict[str, Any] | None = None) -> bool:
    """データを変更しない呼び出しか（api_request は GET のときだけ読み取り専用とみなす）。"""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return False
    if name == "api_request":
        return str((args or {}).get("method", "")).upper() == "GET"
    return tool.read_only


class ToolError(Exception):
    """ツール名や引数の誤り（API を呼ぶ前に検出したもの）。"""


def call_tool(client: ApiClient, name: str, args: dict[str, Any] | None, actor: str | None = None) -> ApiResult:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"不明なツールです: {name}")
    if args is not None and not isinstance(args, dict):
        raise ToolError("引数はオブジェクトで指定してください")
    args = dict(args or {})

    if name == "api_request":
        method = str(args.get("method", "")).upper()
        path = args.get("path")
        if method not in ("GET", "POST", "PUT", "DELETE") or not isinstance(path, str) or not (path == "/api" or path.startswith(("/api/", "/api?"))):
            raise ToolError("method は GET/POST/PUT/DELETE、path は /api で始まるパスで指定してください")
        return client.request(method, path, args.get("body"), actor)

    missing = [k for k in tool.input_schema.get("required", []) if args.get(k) is None]
    if missing:
        raise ToolError(f"必須の引数がありません: {', '.join(missing)}")
    unknown = [k for k in args if k not in tool.props]
    if unknown:
        raise ToolError(f"不明な引数です: {', '.join(unknown)}（指定できるのは {', '.join(tool.props) or 'なし'}）")

    path = tool.path
    for k in tool.path_args:
        path = path.replace("{" + k + "}", quote(str(args.pop(k)), safe=""))
    q = {k: args.pop(k) for k in tool.query if args.get(k) is not None}
    for k in tool.query:
        args.pop(k, None)
    if q:
        path += "?" + urlencode(q)
    body = None if tool.method in ("GET", "DELETE") else args
    return client.request(tool.method, path, body, actor)
