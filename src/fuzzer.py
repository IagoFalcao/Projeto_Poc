import argparse
import json
import re

from datetime import datetime
from pathlib import Path

from tool_runner import ItyFuzzRunner


class Fuzzer:
    """
    Coordena a preparação dos inputs e a execução do ItyFuzz.

    Estrutura esperada:
      data/fuzzing/inputs/abi/*_abi.abi
      data/fuzzing/inputs/bytecode/*_bytecode.bin

    Os pares são associados pelo nome-base anterior aos sufixos
    _abi.abi e _bytecode.bin.
    """

    ABI_SUFFIX = "_abi.abi"
    BYTECODE_SUFFIX = "_bytecode.bin"

    def __init__(
        self,
        project_root=None,
        timeout=1800,
        concolic=True,
        concolic_caller=True,
        ityfuzz_executable="ityfuzz",
    ):
        if project_root:
            self.project_root = Path(project_root).expanduser().resolve()
        else:
            # fuzzer.py fica em Projeto_Poc/src/, mas os dados ficam em
            # Projeto_Poc/data/. Procura a raiz do projeto subindo os pais.
            script_dir = Path(__file__).resolve().parent
            self.project_root = next(
                (
                    parent
                    for parent in (script_dir, *script_dir.parents)
                    if (parent / "data" / "fuzzing" / "inputs" / "abi").is_dir()
                    and (parent / "data" / "fuzzing" / "inputs" / "bytecode").is_dir()
                ),
                script_dir.parent if script_dir.name == "src" else script_dir,
            )

        self.abi_dir = self.project_root / "data" / "fuzzing" / "inputs" / "abi"
        self.bytecode_dir = self.project_root / "data" / "fuzzing" / "inputs" / "bytecode"
        self.output_root = self.project_root / "data" / "fuzzing" / "ityfuzz"

        self.runner = ItyFuzzRunner(
            timeout=timeout,
            concolic=concolic,
            concolic_caller=concolic_caller,
            executable=ityfuzz_executable,
        )

    @staticmethod
    def _write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _safe_filename(name):
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)

    @staticmethod
    def _read_abi(path):
        text = path.read_text(encoding="utf-8").strip()
        abi = json.loads(text)

        if not isinstance(abi, list):
            raise ValueError("A ABI precisa ser um array JSON.")

        return abi

    @staticmethod
    def _read_bytecode(path):
        text = path.read_text(encoding="utf-8").strip()

        # Aceita tanto bytecode hexadecimal puro quanto JSON de artefato
        # contendo bytecode/object.
        if text.startswith("{"):
            data = json.loads(text)

            if isinstance(data, dict):
                bytecode = data.get("object")

                if bytecode is None:
                    bytecode = data.get("bytecode")

                if isinstance(bytecode, dict):
                    bytecode = bytecode.get("object")

                if bytecode is None and isinstance(data.get("evm"), dict):
                    bytecode = (
                        data["evm"]
                        .get("bytecode", {})
                        .get("object")
                    )

                text = bytecode or ""

        if not isinstance(text, str):
            raise ValueError("Formato de bytecode não reconhecido.")

        bytecode = "".join(text.split())

        if bytecode.startswith("0x"):
            bytecode = bytecode[2:]

        if not bytecode:
            raise ValueError("Bytecode vazio.")

        if len(bytecode) % 2 != 0:
            raise ValueError("Bytecode hexadecimal com quantidade ímpar de caracteres.")

        if not re.fullmatch(r"[0-9a-fA-F]+", bytecode):
            raise ValueError(
                "Bytecode contém caracteres inválidos ou placeholders de bibliotecas."
            )

        return bytecode.lower()

    def _index_files(self, directory, suffix):
        indexed = {}

        if not directory.is_dir():
            raise FileNotFoundError(f"Diretório não encontrado: {directory}")

        for path in sorted(directory.glob(f"*{suffix}")):
            base_name = path.name[:-len(suffix)]
            indexed[base_name] = path

        return indexed

    def prepare_targets(self, target_dir):
        abi_files = self._index_files(self.abi_dir, self.ABI_SUFFIX)
        bytecode_files = self._index_files(
            self.bytecode_dir,
            self.BYTECODE_SUFFIX,
        )

        target_dir.mkdir(parents=True, exist_ok=True)

        abi_names = set(abi_files)
        bytecode_names = set(bytecode_files)
        common_names = sorted(abi_names & bytecode_names)

        skipped = {
            "abi_without_bytecode": sorted(abi_names - bytecode_names),
            "bytecode_without_abi": sorted(bytecode_names - abi_names),
            "invalid_pairs": [],
        }

        manifest = []

        for base_name in common_names:
            abi_source = abi_files[base_name]
            bytecode_source = bytecode_files[base_name]

            try:
                abi = self._read_abi(abi_source)
                bytecode = self._read_bytecode(bytecode_source)

                if not bytecode:
                    raise ValueError("Bytecode vazio.")

                target_name = self._safe_filename(base_name)

                abi_target = target_dir / f"{target_name}.abi"
                bin_target = target_dir / f"{target_name}.bin"

                abi_target.write_text(
                    json.dumps(abi, ensure_ascii=False),
                    encoding="utf-8",
                )
                bin_target.write_text(bytecode, encoding="utf-8")

                manifest.append({
                    "name": base_name,
                    "abi_source": str(abi_source),
                    "bytecode_source": str(bytecode_source),
                    "abi_target": str(abi_target),
                    "bytecode_target": str(bin_target),
                })

            except (OSError, ValueError, json.JSONDecodeError) as exc:
                skipped["invalid_pairs"].append({
                    "name": base_name,
                    "abi": str(abi_source),
                    "bytecode": str(bytecode_source),
                    "error": str(exc),
                })

        self._write_json(target_dir.parent / "input_manifest.json", manifest)
        self._write_json(target_dir.parent / "skipped_inputs.json", skipped)

        return manifest, skipped

    def run(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_id = f"run_{timestamp}"

        run_dir = self.output_root / "runs" / run_id
        target_dir = run_dir / "targets"

        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            manifest, skipped = self.prepare_targets(target_dir)

            if not manifest:
                error = (
                    "Nenhum par ABI/bytecode válido foi encontrado. "
                    "Verifique os diretórios de inputs e os arquivos ignorados."
                )

                self._write_json(run_dir / "preparation_error.json", {
                    "error": error,
                    "abi_directory": str(self.abi_dir),
                    "bytecode_directory": str(self.bytecode_dir),
                    "skipped": skipped,
                })

                raise RuntimeError(error)

            self._write_json(run_dir / "preparation.json", {
                "contracts_prepared": len(manifest),
                "contracts_skipped": (
                    len(skipped["abi_without_bytecode"])
                    + len(skipped["bytecode_without_abi"])
                    + len(skipped["invalid_pairs"])
                ),
                "inputs": manifest,
                "skipped": skipped,
            })

            metadata = self.runner.execute(
                target_dir=target_dir,
                run_dir=run_dir,
                contracts_manifest=manifest,
            )

            metadata["execution_id"] = run_id
            self._write_json(run_dir / "metadata.json", metadata)

            print(f"\nCampanha: {run_id}")
            print(f"Status: {metadata['execution_status']}")
            print(f"Contratos preparados: {len(manifest)}")
            print(f"Resultados: {run_dir}")

            return metadata

        except Exception as exc:
            # Preserva o erro de preparação/orquestração sem apagar
            # os arquivos que já tenham sido gerados.
            if not (run_dir / "preparation_error.json").exists():
                self._write_json(run_dir / "preparation_error.json", {
                    "error": str(exc),
                    "exception": type(exc).__name__,
                })
            raise


def main():
    parser = argparse.ArgumentParser(
        description="Executa campanhas off-chain do ItyFuzz."
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Raiz do Projeto_Poc. Por padrão, detecta automaticamente a raiz a partir de src/fuzzer.py.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Tempo máximo da campanha em segundos (padrão: 1800).",
    )
    parser.add_argument(
        "--no-concolic",
        action="store_true",
        help="Desativa a execução concolic.",
    )
    parser.add_argument(
        "--no-concolic-caller",
        action="store_true",
        help="Desativa a exploração concolic de diferentes callers.",
    )
    parser.add_argument(
        "--ityfuzz",
        default="ityfuzz",
        help="Nome ou caminho do executável ItyFuzz.",
    )

    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout precisa ser maior que zero.")

    fuzzer = Fuzzer(
        project_root=args.project_root,
        timeout=args.timeout,
        concolic=not args.no_concolic,
        concolic_caller=not args.no_concolic_caller,
        ityfuzz_executable=args.ityfuzz,
    )

    fuzzer.run()


if __name__ == "__main__":
    main()
