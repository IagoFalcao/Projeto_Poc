from pathlib import Path
import json
import re
import solcx
from semantic_version import NpmSpec, Version

#patterns
IMPORT_RE = re.compile(
    r"""\bimport\s+(?:(?:[^"']+?)\s+from\s+)?["']([^"']+)["']\s*;""",
    re.DOTALL,
)
PRAGMA_RE = re.compile(r"\bpragma\s+solidity\s+([^;]+);")
DECLARATION_RE = re.compile(
    r"\b(contract|interface|library)\s+([A-Za-z_]\w*)"
)

#get general info about the contract dataset, amount, size, files, paths
def catalog(fp: str | Path) -> dict:
    root = Path(fp).expanduser().resolve()

    if not root.exists():
        raise FileNotFoundError(f"Provided folder {fp} not found")

    if not root.is_dir():
        raise NotADirectoryError(f"Path {fp} is not a valid directory")

    solidity_files = sorted(root.rglob("*.sol"))

    result = {
        "folder": {
            "abs_path": str(root),
            "sol_files_count": len(solidity_files),
        },
        "brief": {
            "contracts": [],
            "interfaces": [],
            "libraries": [],
            "others": [],
        },
        "files": [],
        "imports": [],
        "pragmas_solidity": [],
        "errors": [],
    }

    for path in solidity_files:
        abs_path = path.resolve()
        rel_path = path.relative_to(root).as_posix()

        contract_data = {
            "filename": path.name,
            "cname": path.stem,
            "abs_path": str(abs_path),
            "rel_path": rel_path,
            "extension": path.suffix,
            "declarations": [],
            "imports": [],
            "sol_version": [],
            "size_bytes": path.stat().st_size,
        }

        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            error_info = {
                "file": rel_path,
                "error": str(error),
            }
            result["errors"].append(error_info)
            contract_data["error"] = str(error)
            result["files"].append(contract_data)
            continue

        declarations = DECLARATION_RE.findall(content)

        for file_type, name in declarations:
            contract_data["declarations"].append({
                "type": file_type,
                "name": name,
            })

            category = {
                "contract": "contracts",
                "interface": "interfaces",
                "library": "libraries",
            }[file_type]

            result["brief"][category].append({
                "name": name,
                "file": rel_path,
            })

        if not declarations:
            result["brief"]["others"].append(rel_path)

        # collect imports
        for import_path in IMPORT_RE.findall(content):
            import_info = {
                "caller_file": rel_path,
                "import_path": import_path,
                "type": (
                    "relative"
                    if import_path.startswith(".")
                    else "package_or_path"
                ),
            }
            contract_data["imports"].append(import_info)
            result["imports"].append(import_info)

        # collect versions
        for pragma in PRAGMA_RE.findall(content):
            version_info = {
                "file": rel_path,
                "version": pragma.strip(),
            }
            contract_data["sol_version"].append(version_info)
            result["pragmas_solidity"].append(version_info)

        result["files"].append(contract_data)

    return result


def save_result_json(
    res: dict,
    output_dir: str | Path,
    filename: str = "catalog.json",
) -> Path:
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    file_path = output_path / filename
    file_path.write_text(
        json.dumps(res, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )

    return file_path

def resolve_imports(catalog_data: dict) -> dict:
    """
    Resolve imports relativos dentro da pasta catalogada.

    Atualiza catalog_data com:
      - resolved_imports: relação completa dos imports e seus resultados
      - status: "resolved", "not_found" ou "external"

    Imports de pacote, como @openzeppelin/... ou nome sem ./ e ../,
    ficam marcados como "external" para uma etapa posterior de remappings.
    """
    root = Path(catalog_data["folder"]["abs_path"]).resolve()
    resolved_imports = []

    for import_info in catalog_data["imports"]:
        caller_rel = Path(import_info["caller_file"])
        caller_abs = (root / caller_rel).resolve()
        import_path = import_info["import_path"]

        record = {
            "caller_file": import_info["caller_file"],
            "import_path": import_path,
            "resolved_file": None,
            "resolved_abs_path": None,
            "status": None,
            "error": None,
        }

        # Por enquanto, resolve somente imports relativos.
        if not import_path.startswith(("./", "../")):
            record["status"] = "external"
            resolved_imports.append(record)
            continue

        candidate = (caller_abs.parent / import_path).resolve()

        # Impede que um import relativo saia da pasta catalogada.
        try:
            candidate.relative_to(root)
        except ValueError:
            record["status"] = "not_found"
            record["error"] = "Import aponta para fora da pasta catalogada."
            resolved_imports.append(record)
            continue

        if candidate.is_file() and candidate.suffix == ".sol":
            record["resolved_file"] = candidate.relative_to(root).as_posix()
            record["resolved_abs_path"] = str(candidate)
            record["status"] = "resolved"
        else:
            record["status"] = "not_found"
            record["error"] = "Arquivo importado não encontrado."

        resolved_imports.append(record)

    catalog_data["resolved_imports"] = resolved_imports

    # Também deixa cada import associado ao seu arquivo no catálogo.
    resolved_by_caller = {}
    for record in resolved_imports:
        resolved_by_caller.setdefault(record["caller_file"], []).append(record)

    for file_info in catalog_data["files"]:
        file_info["resolved_imports"] = resolved_by_caller.get(
            file_info["rel_path"], []
        )

    return catalog_data

def check_solc_version(
    catalog_data: dict,
    solc_version: str,
) -> dict:
    """
    Verifica se a versão concreta de solc satisfaz todos os pragmas
    encontrados pelo catalog().
    """
    version_text = solc_version.strip().removeprefix("v")

    try:
        version = Version(version_text)
    except ValueError as error:
        raise ValueError(
            f"Versão inválida de solc: {solc_version}"
        ) from error

    checks = []

    for pragma_info in catalog_data["pragmas_solidity"]:
        expression = pragma_info["version"].strip()

        try:
            spec = NpmSpec(expression)
            compatible = spec.match(version)
            error = None
        except ValueError as exc:
            compatible = False
            error = f"Não foi possível interpretar o pragma: {exc}"

        checks.append({
            "file": pragma_info["file"],
            "pragma": expression,
            "solc_version": str(version),
            "compatible": compatible,
            "error": error,
        })

    incompatible = [item for item in checks if not item["compatible"]]

    return {
        "solc_version": str(version),
        "compatible": not incompatible,
        "checks": checks,
        "incompatible_pragmas": incompatible,
    }

#generate a config file to setup solc compilation
def build_solc_input(
    catalog_data: dict,
    optimize: bool = True,
    optimizer_runs: int = 200,
    evm_version: str | None = None,
) -> dict:
    """
    Monta o Standard JSON Input aceito pelo solc.
    """
    root = Path(catalog_data["folder"]["abs_path"]).resolve()
    sources = {}

    for file_info in catalog_data["files"]:
        if file_info.get("error"):
            continue

        path = root / file_info["rel_path"]
        source_name = Path(file_info["rel_path"]).as_posix()

        sources[source_name] = {
            "content": path.read_text(encoding="utf-8")
        }

    settings = {
        "optimizer": {
            "enabled": optimize,
            "runs": optimizer_runs,
        },
        "outputSelection": {
            "*": {
                "*": [
                    "abi",
                    "evm.bytecode.object",
                    "evm.deployedBytecode.object",
                ],
                "": ["ast"],
            }
        },
    }

    if evm_version:
        settings["evmVersion"] = evm_version

    return {
        "language": "Solidity",
        "sources": sources,
        "settings": settings,
    }

def save_solc_input(solc_input: dict, output_path: str | Path) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(solc_input, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path

def compile_with_solc(input_path, output_path, solc_version="0.8.24"):
    solcx.set_solc_version(solc_version)

    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()

    compiler_output = solcx.compile_standard(
        json.loads(input_path.read_text(encoding="utf-8")),
        solc_version=solc_version,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(compiler_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    errors = compiler_output.get("errors", [])
    for item in errors:
        severity = item.get("severity", "unknown")
        message = item.get("formattedMessage", item.get("message", ""))
        print(f"[{severity}] {message}")

    if any(item.get("severity") == "error" for item in errors):
        raise RuntimeError("A compilação terminou com erros do Solidity.")

    return compiler_output

def extract_artifacts(solc_output_path: str, output_dir: str) -> dict:
    """
    Extrai ABI, bytecode e AST da saída Standard JSON do solc.

    Salva cada artefato como JSON em:
      output_dir/abi/
      output_dir/bytecode/
      output_dir/ast/

    Retorna um resumo dos artefatos extraídos e diagnósticos.
    """
    solc_output_path = Path(solc_output_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()

    with solc_output_path.open("r", encoding="utf-8") as file:
        compiler_output = json.load(file)

    diagnostics = compiler_output.get("errors", [])
    errors = [item for item in diagnostics if item.get("severity") == "error"]
    warnings = [item for item in diagnostics if item.get("severity") == "warning"]

    # Não tenta extrair artefatos como se a compilação tivesse sucesso.
    if errors:
        messages = "\n".join(
            item.get("formattedMessage", item.get("message", "Erro sem mensagem"))
            for item in errors
        )
        raise RuntimeError(f"Compilação contém erros:\n{messages}")

    abi_dir = output_dir / "abi"
    bytecode_dir = output_dir / "bytecode"
    ast_dir = output_dir / "ast"

    abi_dir.mkdir(parents=True, exist_ok=True)
    bytecode_dir.mkdir(parents=True, exist_ok=True)
    ast_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "abi_files": [],
        "bytecode_files": [],
        "ast_files": [],
        "warnings": [
            item.get("formattedMessage", item.get("message", "Aviso sem mensagem"))
            for item in warnings
        ],
    }

    # Cada arquivo-fonte pode declarar mais de um contrato.
    for source_name, contracts in compiler_output.get("contracts", {}).items():
        for contract_name, artifact in contracts.items():
            safe_source = source_name.replace("/", "__").replace("\\", "__")
            artifact_name = f"{safe_source}__{contract_name}.json"

            if "abi" in artifact:
                path = abi_dir / artifact_name
                path.write_text(
                    json.dumps(artifact["abi"], indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                summary["abi_files"].append(str(path))

            # O bytecode de implantação fica em evm.bytecode.object.
            bytecode = (
                artifact.get("evm", {})
                .get("bytecode", {})
                .get("object")
            )
            if bytecode is not None:
                path = bytecode_dir / artifact_name
                path.write_text(
                    json.dumps(
                        {
                            "source": source_name,
                            "contract": contract_name,
                            "bytecode": bytecode,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                summary["bytecode_files"].append(str(path))

    # O AST é associado ao arquivo-fonte, não a cada contrato.
    for source_name, source_info in compiler_output.get("sources", {}).items():
        ast = source_info.get("ast")
        if ast is None:
            continue

        safe_source = source_name.replace("/", "__").replace("\\", "__")
        path = ast_dir / f"{safe_source}.json"
        path.write_text(
            json.dumps(ast, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        summary["ast_files"].append(str(path))

    summary["counts"] = {
        "abis": len(summary["abi_files"]),
        "bytecodes": len(summary["bytecode_files"]),
        "asts": len(summary["ast_files"]),
        "errors": len(errors),
        "warnings": len(warnings),
    }
    summary_path = Path("../outputs/artifacts/summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return summary