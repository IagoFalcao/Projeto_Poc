from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Sequence

import openai
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from openai import OpenAI

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = PROJECT_ROOT / "data" / "reports" / "18.md"
CONTRACTS_DIR = PROJECT_ROOT / "data" / "18" / "contracts"
DEFAULT_SESSIONS = PROJECT_ROOT / "outputs" / "json_sessions.json"
DEFAULT_ANALYSIS = PROJECT_ROOT / "outputs" / "analysis_18.json"

severity_titles = {
    "High Risk Findings": "high", "Medium Risk Findings": "medium",
    "Low Risk Findings": "low", "Non-Critical Findings": "non_critical",
    "Gas Optimizations": "gas",
}
FINDING_RE = re.compile(r"\[(H|M|L|N|G)-\d+\]")
FINDING_TITLE_RE = re.compile(r"\[\[(?P<id>[A-Z]-\d+)\]\s*(?P<title>.*?)\]")
FUNCTION_RE = re.compile(r"`([A-Za-z0-9_]+\.[A-Za-z0-9_]+)`")
CONTRACT_RE = re.compile(r"`([A-Z][A-Za-z0-9_]+\.sol)`")
URL_RE = re.compile(r"https?://[^\s)]+")
SYSTEM_PROMPT = "You are a smart contract security analyst. Return only valid JSON."


class ReportReaderError(Exception):
    """Base class for report reader failures."""


class ReportFileError(ReportReaderError):
    """A report or output file could not be used."""


class AnalysisError(ReportReaderError):
    """A complete analysis could not be produced."""


def _project_path(path: str | Path) -> Path:
    """Resolve project paths and reject traversal outside the repository."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("O caminho precisa permanecer dentro da pasta do projeto.") from exc
    return resolved


def convert_to_html(filepath: str | Path = DEFAULT_REPORT) -> tuple[list[Token], str]:
    try:
        path = _project_path(filepath)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReportFileError(f"Relatório não encontrado: {filepath}") from exc
    except PermissionError as exc:
        raise ReportFileError(f"Sem permissão para ler o relatório: {filepath}") from exc
    except UnicodeDecodeError as exc:
        raise ReportFileError(f"Relatório não está em UTF-8: {filepath}") from exc
    except OSError as exc:
        raise ReportFileError(f"Falha de leitura do relatório: {filepath}") from exc
    except ValueError as exc:
        raise ReportFileError(f"Caminho de relatório inválido: {filepath}") from exc
    try:
        md = (MarkdownIt("commonmark", {"breaks": True, "html": True})
              .use(front_matter_plugin).use(footnote_plugin).enable("table"))
        tokens, html = md.parse(text), md.render(text)
    except (TypeError, ValueError) as exc:
        raise ReportReaderError("Não foi possível interpretar o Markdown.") from exc
    if not tokens or not html.strip():
        raise ReportReaderError(f"Relatório vazio: {path}")
    return tokens, html

def walk(node: Any, depth: int = 0) -> None:
    if not isinstance(depth, int) or depth < 0:
        raise ValueError("depth precisa ser um inteiro não negativo.")
    if node is None or not hasattr(node, "type"):
        raise TypeError("node precisa ter o atributo type.")
    try:
        logger.debug("%s%s", "  " * depth, node.type)
        for child in getattr(node, "children", None) or []:
            walk(child, depth + 1)
    except (AttributeError, TypeError) as exc:
        raise ReportReaderError("Falha ao percorrer árvore sintática.") from exc



def extract_text_sessions(node: SyntaxTreeNode) -> list[dict[str, Any]]:
    if node is None or not hasattr(node, "children"):
        raise TypeError("node precisa ser uma árvore Markdown com children.")
    sessions: list[dict[str, Any]] = []
    current: dict[str, Any] = {"title": None, "content": []}
    try:
        for child in node.children:
            if child.type == "heading":
                if current["title"] or current["content"]:
                    sessions.append(current)
                title = "".join(
                    token.content for token in (child.children or [])
                    if getattr(token, "content", None)
                ).strip()
                current = {"title": title, "content": []}
            elif child.type == "paragraph":
                text = "".join(
                    token.content for token in (child.children or [])
                    if hasattr(token, "content")
                ).strip()
                if text:
                    current["content"].append(text)
        if current["title"] or current["content"]:
            sessions.append(current)
    except (AttributeError, TypeError, KeyError) as exc:
        raise ReportReaderError("Estrutura inválida ao extrair seções.") from exc
    return sessions


def get_findings_from_sessions(sessions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(sessions, (list, tuple)):
        raise TypeError("sessions precisa ser uma lista ou tupla.")
    findings: list[dict[str, Any]] = []
    severity = None
    try:
        for session in sessions:
            if not isinstance(session, dict) or "title" not in session:
                raise ValueError("Cada seção precisa ter o campo title.")
            title = session["title"] or ""
            if not isinstance(title, str):
                raise TypeError("O título da seção precisa ser texto.")
            if title in severity_titles:
                severity = severity_titles[title]
            if FINDING_RE.search(title):
                match = FINDING_TITLE_RE.search(title)
                if not match:
                    raise ValueError(f"Título de finding inválido: {title}")
                content = session.get("content", [])
                if not isinstance(content, list) or not all(isinstance(x, str) for x in content):
                    raise ValueError(f"Conteúdo inválido no finding {match.group('id')}.")
                findings.append({"severity": severity, "id": match.group("id"),
                                 "title": match.group("title").strip(), "content": content})
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportReaderError(f"Falha ao extrair findings: {exc}") from exc
    return findings


def build_finding_message(finding: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(finding, dict):
        raise TypeError("finding precisa ser um dicionário.")
    try:
        title, content = finding["title"], finding["content"]
    except KeyError as exc:
        raise ValueError(f"Campo obrigatório ausente: {exc.args[0]}") from exc
    if not isinstance(title, str) or not isinstance(content, list):
        raise TypeError("title deve ser texto e content, uma lista.")
    if not all(isinstance(item, str) for item in content):
        raise TypeError("Os itens de content precisam ser textos.")
    prompt = f"""Extract structured vulnerability information as JSON.
Include vulnerability_type, affected_functions, affected_contracts, root_cause,
impact, recommendation, attack_vector, prerequisites, cwe, and swc.

Title: {title}

Description:
{" ".join(content)}
"""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}]


def call_llm_agent_api(messages: Sequence[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise TypeError("messages precisa ser uma lista não vazia.")
    try:
        client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama",
                        timeout=180.0, max_retries=5)
        response = client.chat.completions.create(
            model="qwen3:4b-instruct-2507-q4_K_M", messages=list(messages), temperature=0, top_p=1,
            response_format={"type": "json_object"})
        content = response.choices[0].message.content
        if not content:
            raise AnalysisError("O serviço local retornou resposta vazia.")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise AnalysisError("A resposta do serviço não é um objeto JSON.")
        return result
    except openai.APIConnectionError as exc:
        raise AnalysisError("Ollama indisponível em http://localhost:11434/v1.") from exc
    except openai.APIStatusError as exc:
        raise AnalysisError(f"Serviço de análise respondeu HTTP {exc.status_code}.") from exc
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
        raise AnalysisError("Resposta do serviço de análise em formato inválido.") from exc
    except openai.APIError as exc:
        raise AnalysisError("O serviço local retornou um erro de API.") from exc


def analyse_findings(findings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(findings, (list, tuple)):
        raise TypeError("findings precisa ser uma lista ou tupla.")
    results = []
    for finding in findings:
        finding_id = finding.get("id", "desconhecido") if isinstance(finding, dict) else "desconhecido"
        try:
            results.append(call_llm_agent_api(build_finding_message(finding)))
        except (ReportReaderError, TypeError, ValueError) as exc:
            raise AnalysisError(f"Falha ao analisar o finding {finding_id}: {exc}") from exc
    return results


def load_ast_from_json(filepath: str | Path = DEFAULT_SESSIONS) -> Any:
    try:
        path = _project_path(filepath)
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportFileError(f"JSON não encontrado: {filepath}") from exc
    except PermissionError as exc:
        raise ReportFileError(f"Sem permissão para ler JSON: {filepath}") from exc
    except UnicodeDecodeError as exc:
        raise ReportFileError(f"JSON não está em UTF-8: {filepath}") from exc
    except json.JSONDecodeError as exc:
        raise ReportReaderError(f"JSON inválido na linha {exc.lineno}, coluna {exc.colno}.") from exc
    except OSError as exc:
        raise ReportFileError(f"Falha ao ler JSON: {filepath}") from exc


def save_sessions(sessions: Any, filepath: str | Path = DEFAULT_SESSIONS) -> bool:
    try:
        path = _project_path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except (TypeError, ValueError) as exc:
        raise ReportReaderError("Os dados não podem ser serializados como JSON.") from exc
    except PermissionError as exc:
        raise ReportFileError(f"Sem permissão para gravar: {filepath}") from exc
    except OSError as exc:
        raise ReportFileError(f"Falha ao gravar: {filepath}") from exc

#orquestra leitura dos reports, e a analise do conteudo e retorna um dicionario da analise
def build_analysis(
    report_path: str | Path = DEFAULT_REPORT,
    contracts_dir: str | Path = CONTRACTS_DIR,
    analysis_path: str | Path | None = DEFAULT_ANALYSIS,
) -> dict[str, Any]:

    try:
        report, contracts = _project_path(report_path), _project_path(contracts_dir)
        if not contracts.is_dir():
            raise ReportFileError(f"Pasta de contratos não encontrada: {contracts}")
        tokens, _html = convert_to_html(report)
        sessions = extract_text_sessions(SyntaxTreeNode(tokens))
        findings = get_findings_from_sessions(sessions)
        results = analyse_findings(findings)
        analysis = {
            "report_path": str(report.relative_to(PROJECT_ROOT)),
            "contracts_dir": str(contracts.relative_to(PROJECT_ROOT)),
            "contract_files": sorted(p.name for p in contracts.glob("*.sol")),
            "findings_count": len(findings),
            "analysis_results": results,
        }
        if analysis_path is not None:
            save_sessions(analysis, analysis_path)
        return analysis
    except ReportReaderError:
        raise
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise AnalysisError("Não foi possível concluir a análise do relatório.") from exc


if __name__ == "__main__":
    try:
        build_analysis()
    except ReportReaderError as exc:
        logger.error("Análise interrompida: %s", exc)
        raise SystemExit(1) from exc
