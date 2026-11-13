import json
import shutil
import subprocess
import time

from datetime import datetime, timezone
from pathlib import Path


class ItyFuzzRunner:
    """
    Executa uma campanha off-chain do ItyFuzz sobre um diretório que já
    contém pares de arquivos <contrato>.abi e <contrato>.bin.

    Este módulo não coleta nem transforma os artefatos de compilação.
    Essa preparação é responsabilidade de fuzzer.py.
    """

    def __init__(
        self,
        timeout=1800,
        concolic=True,
        concolic_caller=True,
        executable="ityfuzz",
    ):
        self.timeout = timeout
        self.concolic = concolic
        self.concolic_caller = concolic_caller
        self.executable = executable

    def get_version(self):
        executable_path = shutil.which(self.executable)

        if executable_path is None:
            return None

        try:
            result = subprocess.run(
                [executable_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return (result.stdout or result.stderr).strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    @staticmethod
    def _write_json(path, data):
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def execute(self, target_dir, run_dir, contracts_manifest=None):
        target_dir = Path(target_dir).resolve()
        run_dir = Path(run_dir).resolve()

        if not target_dir.is_dir():
            raise FileNotFoundError(f"Diretório de targets inexistente: {target_dir}")

        run_dir.mkdir(parents=True, exist_ok=True)

        # A ferramenta pode criar corpus, traces e outros arquivos.
        # Mantemos esses arquivos separados dos logs do nosso runner.
        tool_output_dir = run_dir / "tool_output"
        tool_output_dir.mkdir(parents=True, exist_ok=True)

        abi_files = list(target_dir.glob("*.abi"))
        bin_files = list(target_dir.glob("*.bin"))

        if not abi_files or not bin_files:
            raise RuntimeError(
                "O diretório de targets precisa conter arquivos .abi e .bin."
            )

        executable_path = shutil.which(self.executable)
        started_at = datetime.now(timezone.utc)
        start_time = time.monotonic()

        command = [
            executable_path or self.executable,
            "evm",
            "-t",
            str(target_dir / "*"),
        ]

        if self.concolic:
            command.append("--concolic")

            if self.concolic_caller:
                command.append("--concolic-caller")

        self._write_json(run_dir / "command.json", command)

        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"

        status = "failed"
        exit_code = None
        error_records = []
        version = self.get_version()

        try:
            if executable_path is None:
                raise FileNotFoundError(
                    f"Executável '{self.executable}' não encontrado no PATH."
                )

            # Redirecionar diretamente para arquivos evita acumular toda a
            # saída em memória e preserva logs mesmo em caso de timeout.
            with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_file, \
                 stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_file:

                result = subprocess.run(
                    command,
                    cwd=tool_output_dir,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=self.timeout,
                    check=False,
                )

            exit_code = result.returncode
            status = "completed" if exit_code == 0 else "failed"

            if exit_code != 0:
                error_records.append({
                    "type": "process_error",
                    "message": "ItyFuzz terminou com código de saída diferente de zero.",
                    "exit_code": exit_code,
                })

        except subprocess.TimeoutExpired:
            status = "timeout"
            error_records.append({
                "type": "timeout",
                "message": f"A execução excedeu o limite de {self.timeout} segundos.",
                "timeout_seconds": self.timeout,
            })

        except Exception as exc:
            status = "failed"
            error_records.append({
                "type": "execution_error",
                "message": str(exc),
                "exception": type(exc).__name__,
            })

        duration = time.monotonic() - start_time
        finished_at = datetime.now(timezone.utc)

        # Registra os arquivos criados pela ferramenta, sem assumir que
        # todos os tipos de saída possuem um formato fixo entre versões.
        generated_files = []
        for path in sorted(tool_output_dir.rglob("*")):
            if path.is_file():
                generated_files.append({
                    "path": str(path.relative_to(run_dir)),
                    "size_bytes": path.stat().st_size,
                })

        self._write_json(run_dir / "contracts.json", contracts_manifest or [])

        self._write_json(run_dir / "errors.json", error_records)

        # Não inventamos findings a partir de mensagens arbitrárias.
        # stdout, stderr e tool_output preservam as evidências brutas.
        self._write_json(run_dir / "findings.json", {
            "status": "not_parsed",
            "items": [],
            "note": (
                "Os findings ainda não foram extraídos para JSON estruturado. "
                "Consulte stdout.log, stderr.log e tool_output."
            ),
        })

        metadata = {
            "tool": "ityfuzz",
            "version": version,
            "execution_mode": "offchain",
            "execution_status": status,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(duration, 3),
            "timeout_seconds": self.timeout,
            "configuration": {
                "concolic": self.concolic,
                "concolic_caller": self.concolic_caller if self.concolic else False,
            },
            "target_directory": str(target_dir),
            "contracts_count": len(contracts_manifest or []),
            "abi_files_count": len(abi_files),
            "bytecode_files_count": len(bin_files),
            "exit_code": exit_code,
            "generated_files": generated_files,
        }

        self._write_json(run_dir / "metadata.json", metadata)

        return metadata
