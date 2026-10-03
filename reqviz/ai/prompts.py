"""用途別のプロンプトと応答スキーマ。"""

from __future__ import annotations

import json
from typing import Any

from ..engine.expr import FUNCTIONS

PURPOSES = ("evaluate", "suggest_values", "suggest_rules", "rationale")

PURPOSE_LABELS = {
    "evaluate": "妥当性評価",
    "suggest_values": "設計値の提案",
    "suggest_rules": "ルール・関連の提案",
    "rationale": "設計根拠の作成",
}

_SEVERITY = {"type": "string", "enum": ["error", "warning", "info"]}

SCHEMAS: dict[str, dict[str, Any]] = {
    "evaluate": {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "message"],
                    "properties": {
                        "param": {"type": ["string", "null"]},
                        "severity": _SEVERITY,
                        "message": {"type": "string"},
                        "rule": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },
    "suggest_values": {
        "type": "object",
        "required": ["suggestions"],
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["param", "value", "rationale"],
                    "properties": {
                        "param": {"type": "string"},
                        "value": {"type": ["number", "string", "boolean"]},
                        "rationale": {"type": "string"},
                    },
                },
            }
        },
    },
    "suggest_rules": {
        "type": "object",
        "required": ["rules"],
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key", "kind", "reason"],
                    "properties": {
                        "key": {"type": "string"},
                        "kind": {"type": "string", "enum": ["formula", "constraint", "nl"]},
                        "target": {"type": ["string", "null"]},
                        "expression": {"type": ["string", "null"]},
                        "nl_text": {"type": ["string", "null"]},
                        "severity": {"type": "string", "enum": ["error", "warning"]},
                        "description": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to"],
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "note": {"type": "string"}},
                },
            },
        },
    },
    "rationale": {
        "type": "object",
        "required": ["rationales"],
        "properties": {
            "rationales": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["param", "text"],
                    "properties": {"param": {"type": "string"}, "text": {"type": "string"}},
                },
            }
        },
    },
}

SYSTEM_PROMPT = """あなたはシステムアーキテクトとして、システムの設計パラメータとその算出ルールをレビューする。
パラメータは「レイヤー.キー」で参照され、上流（業務要件）から下流（インフラ設計）へ依存している。
算出式（formula）と制約（constraint）はツールが確定的に計算・検証済みである。あなたは主に、
自然言語ルール（nl）と一般的な設計知識に照らした定性的な妥当性を判断する。
回答は必ず指定された JSON 形式のみで、日本語で記述すること。"""

INSTRUCTIONS = {
    "evaluate": """対象パラメータの値が、ルール（特に自然言語ルール）や一般的な設計の常識に照らして妥当か評価せよ。
問題がなければ findings は空配列でよい。
出力形式: {"summary": "全体所見", "findings": [{"param": "layer.key または null", "severity": "error|warning|info", "message": "指摘内容", "rule": "関連ルールキー または null"}]}""",
    "suggest_values": """対象パラメータのうち、算出式で決まらないものについて、上流要件から妥当な設計値を提案せよ。
値は型（number/string/enum/bool）に合わせ、enum は選択肢から選ぶこと。
出力形式: {"suggestions": [{"param": "layer.key", "value": 値, "rationale": "算出根拠"}]}""",
    "suggest_rules": """パラメータ間で不足している算出式・制約・自然言語ルール、または関連を提案せよ。
式は Python の式構文のサブセットで、参照は「レイヤー.キー」、使用可能な関数は {functions}、
条件分岐は「A if 条件 else B」で書く。既存のルールキーと重複しないキーを付けること。
出力形式: {{"rules": [{{"key": "...", "kind": "formula|constraint|nl", "target": "formula/nl の対象 または null", "expression": "式 または null", "nl_text": "自然言語ルール または null", "severity": "error|warning", "description": "説明", "reason": "提案理由"}}], "relations": [{{"from": "layer.key", "to": "layer.key", "note": "関係の説明"}}]}}""",
    "rationale": """対象パラメータについて、設計書に記載できる設計根拠の文章（2～4 文）を作成せよ。
算出式があればその意味、上流要件とのつながり、関連する制約に触れること。
出力形式: {"rationales": [{"param": "layer.key", "text": "根拠の文章"}]}""",
}


def build_messages(purpose: str, context: dict[str, Any], targets: list[str]) -> list[dict[str, str]]:
    instruction = INSTRUCTIONS[purpose]
    if purpose == "suggest_rules":
        instruction = instruction.format(functions=", ".join(sorted(FUNCTIONS)))
    target_text = "、".join(targets) if targets else "（全パラメータ）"
    user = (
        f"## 依頼\n{instruction}\n\n## 対象パラメータ\n{target_text}\n\n"
        f"## システムの現状（JSON）\n{json.dumps(context, ensure_ascii=False, indent=1)}"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
