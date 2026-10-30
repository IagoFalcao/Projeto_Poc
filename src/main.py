from pathlib import Path
import json
import solcx
import compiler


def compile_contracts(
    contracts_path: str = "../data/18/",
    output_dir: str = "../outputs",
    solc_version: str = "0.8.24",
    optimize: bool = True,
    optimizer_runs: int = 200,
    evm_version: str = "paris",
) -> dict:
    output_dir = Path(output_dir)

    # Instala e seleciona a versão concreta do compilador.
    installed_versions = {
        str(version) for version in solcx.get_installed_solc_versions()
    }
    if solc_version not in installed_versions:
        solcx.install_solc(solc_version)

    solcx.set_solc_version(solc_version)
    print(f"Usando solc {solcx.get_solc_version()}")

    # Cataloga os contratos e salva o catálogo.
    result = compiler.catalog(contracts_path)
    catalog_path = compiler.save_result_json(result, output_dir)
    print(f"Catálogo salvo em: {catalog_path}")

    # Resolve imports.
    result = compiler.resolve_imports(result)

    unresolved = [
        item for item in result.get("resolved_imports", [])
        if item.get("status") == "not_found"
    ]
    if unresolved:
        details = "\n".join(
            f"  {item['caller_file']} -> {item['import_path']}"
            for item in unresolved
        )
        raise RuntimeError(f"Imports relativos não encontrados:\n{details}")

    external = [
        item for item in result.get("resolved_imports", [])
        if item.get("status") == "external"
    ]
    if external:
        details = "\n".join(
            f"  {item['caller_file']} -> {item['import_path']}"
            for item in external
        )
        raise RuntimeError(
            "Imports externos precisam ser resolvidos antes de compilar:\n"
            f"{details}"
        )

    # Verifica compatibilidade dos pragmas com a versão escolhida.
    version_check = compiler.check_solc_version(result, solc_version)
    if not version_check["compatible"]:
        details = "\n".join(
            f"  {item['file']}: pragma solidity {item['pragma']}"
            for item in version_check["incompatible_pragmas"]
        )
        raise RuntimeError(
            f"solc {solc_version} não satisfaz todos os pragmas:\n{details}"
        )

    print(f"solc {solc_version} satisfaz todos os pragmas.")

    # Gera o Standard JSON Input.
    solc_input = compiler.build_solc_input(
        result,
        optimize=optimize,
        optimizer_runs=optimizer_runs,
        evm_version=evm_version,
    )

    input_path = output_dir / "solc-input.json"
    saved_input_path = compiler.save_solc_input(solc_input, input_path)
    print(f"Input do solc salvo em: {saved_input_path}")

    # Compila e salva a resposta do solc.
    output_path = output_dir / "solc-output.json"
    compiler_output = compiler.compile_with_solc(input_path, output_path)

    # Exibe diagnósticos do compilador.
    diagnostics = compiler_output.get("errors", [])
    for item in diagnostics:
        severity = item.get("severity", "unknown")
        message = item.get("formattedMessage", item.get("message", ""))
        print(f"[{severity}] {message}")

    errors = [
        item for item in diagnostics
        if item.get("severity") == "error"
    ]
    if errors:
        raise RuntimeError(
            f"Compilação falhou com {len(errors)} erro(s). "
            f"Veja os detalhes acima e em {output_path}."
        )

    # Extrai ABI, bytecode e AST. A função também salva o sumário,
    # conforme a implementação atual de extract_artifacts().
    summary = compiler.extract_artifacts(
        output_path,
        output_dir / "artifacts",
    )

    print("Artefatos extraídos:")
    print(json.dumps(summary["counts"], indent=2))

    return {
        "catalog": result,
        "version_check": version_check,
        "solc_input_path": str(saved_input_path),
        "solc_output_path": str(output_path.resolve()),
        "summary": summary,
    }


if __name__ == "__main__":
    compile_contracts()