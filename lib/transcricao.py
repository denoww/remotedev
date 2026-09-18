"""
Transcrição de áudio com fallback.

Provedor primário: OpenAI (gpt-4o-mini-transcribe) — rápido e barato.
Fallback: AWS Transcribe (batch, via S3) — usado quando o OpenAI falha,
principalmente por falta de créditos (insufficient_quota / 429).

A receita do AWS Transcribe segue a skill `transcrever_video`:
- comprime para opus mono 24 kbps (fala cabe folgado e o upload fica pequeno)
- media-format é 'ogg' (opus vem em container Ogg; formato errado = job falha)
- nome de job ÚNICO (a AWS guarda o nome por >=90 dias e devolve ConflictException)
- bucket dedicado `sc-transcricoes-temp` (privado, lifecycle de 1 dia)
- o áudio é apagado do S3 ao terminar; o lifecycle é só a rede de segurança
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid

BUCKET = os.environ.get("TRANSCRICAO_BUCKET", "sc-transcricoes-temp")
REGIAO = os.environ.get("TRANSCRICAO_REGIAO", "us-east-1")
PERFIL = os.environ.get("AWS_PROFILE", "default")

# Áudio de Telegram é curto; o Transcribe leva ~1/10 da duração + overhead fixo.
POLL_INTERVALO = 3
POLL_TENTATIVAS = 60  # ~3 min de teto


def openai_disponivel() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def aws_disponivel() -> bool:
    """Tem CLI e credencial? (não valida permissão — isso só o job dirá)"""
    if not shutil.which("aws"):
        return False
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return True
    return os.path.exists(os.path.expanduser("~/.aws/credentials"))


def provedor_disponivel() -> bool:
    return openai_disponivel() or aws_disponivel()


def _motivo_openai(erro: Exception) -> str:
    """Classifica o erro só para o log — o fallback acontece em qualquer falha."""
    msg = str(erro).lower()
    if "insufficient_quota" in msg or "no credits remaining" in msg or "exceeded your current quota" in msg:
        return "sem créditos"
    if "invalid_api_key" in msg or "incorrect api key" in msg or "401" in msg:
        return "chave inválida"
    if "rate limit" in msg or "429" in msg:
        return "rate limit"
    return "erro"


# --------------------------------------------------------------- OpenAI

def _transcrever_openai_sync(caminho: str) -> str:
    from openai import OpenAI
    client = OpenAI()
    with open(caminho, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
        )
    return transcription.text


# ---------------------------------------------------------- AWS Transcribe

async def _aws(*args: str, timeout: int = 120) -> str:
    """Roda o aws CLI e devolve stdout. Levanta RuntimeError com o stderr real."""
    proc = await asyncio.create_subprocess_exec(
        "aws", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"aws {args[0]} {args[1]} excedeu {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode(errors="replace").strip() or "aws falhou")
    return (out or b"").decode(errors="replace").strip()


async def _comprimir_opus(caminho: str, destino: str) -> str:
    """Converte para opus mono 24k. Sem ffmpeg, usa o arquivo original (voice já é ogg)."""
    if not shutil.which("ffmpeg"):
        if caminho.lower().endswith((".ogg", ".oga", ".opus")):
            return caminho
        raise RuntimeError("ffmpeg não instalado e o áudio não é ogg — não dá para enviar ao Transcribe")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-nostdin", "-y",
        "-i", caminho, "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "24k", destino,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0 or not os.path.exists(destino) or os.path.getsize(destino) == 0:
        raise RuntimeError("ffmpeg não conseguiu converter o áudio: "
                           + (err or b"").decode(errors="replace").strip()[:200])
    return destino


async def _transcrever_aws(caminho: str) -> str:
    job = f"remotedev-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    chave = f"entrada/{job}.opus"
    uri = f"s3://{BUCKET}/{chave}"

    tmp_dir = tempfile.mkdtemp(prefix="remotedev_transcribe_")
    enviado = False
    try:
        opus = await _comprimir_opus(caminho, os.path.join(tmp_dir, f"{job}.opus"))

        await _aws("s3", "cp", opus, uri, "--profile", PERFIL, "--only-show-errors")
        enviado = True

        await _aws(
            "transcribe", "start-transcription-job",
            "--profile", PERFIL, "--region", REGIAO,
            "--transcription-job-name", job,
            "--language-code", "pt-BR",
            "--media-format", "ogg",
            "--media", f"MediaFileUri={uri}",
            "--output-bucket-name", BUCKET,
            "--output-key", f"saida/{job}.json",
            "--query", "TranscriptionJob.TranscriptionJobStatus", "--output", "text",
        )

        status = "IN_PROGRESS"
        for _ in range(POLL_TENTATIVAS):
            await asyncio.sleep(POLL_INTERVALO)
            status = await _aws(
                "transcribe", "get-transcription-job",
                "--profile", PERFIL, "--region", REGIAO,
                "--transcription-job-name", job,
                "--query", "TranscriptionJob.TranscriptionJobStatus", "--output", "text",
            )
            if status in ("COMPLETED", "FAILED"):
                break

        if status == "FAILED":
            motivo = await _aws(
                "transcribe", "get-transcription-job",
                "--profile", PERFIL, "--region", REGIAO,
                "--transcription-job-name", job,
                "--query", "TranscriptionJob.FailureReason", "--output", "text",
            )
            raise RuntimeError(f"AWS Transcribe falhou: {motivo}")
        if status != "COMPLETED":
            raise RuntimeError("AWS Transcribe demorou demais (job segue rodando na AWS)")

        saida = os.path.join(tmp_dir, "saida.json")
        await _aws("s3", "cp", f"s3://{BUCKET}/saida/{job}.json", saida,
                   "--profile", PERFIL, "--only-show-errors")
        with open(saida) as f:
            dados = json.load(f)
        return dados["results"]["transcripts"][0]["transcript"]

    finally:
        # Áudio e transcrição são conteúdo do usuário — saem do S3 assim que usados.
        # O lifecycle de 1 dia do bucket é a rede de segurança, não o plano A.
        if enviado:
            for alvo in (uri, f"s3://{BUCKET}/saida/{job}.json"):
                try:
                    await _aws("s3", "rm", alvo, "--profile", PERFIL, "--only-show-errors", timeout=30)
                except Exception as e:
                    print(f"[transcricao] não consegui apagar {alvo}: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ público

async def transcrever(caminho: str) -> tuple[str, str]:
    """
    Transcreve o áudio e devolve (texto, provedor).

    Tenta OpenAI; se falhar (tipicamente sem créditos), cai para AWS Transcribe.
    Levanta RuntimeError se nenhum provedor entregar texto.
    """
    erro_openai = None

    if openai_disponivel():
        try:
            texto = await asyncio.to_thread(_transcrever_openai_sync, caminho)
            if texto and texto.strip():
                return texto.strip(), "openai"
            erro_openai = RuntimeError("OpenAI devolveu transcrição vazia")
        except Exception as e:
            erro_openai = e
            print(f"[transcricao] OpenAI falhou ({_motivo_openai(e)}): {e} — tentando AWS Transcribe")

    if aws_disponivel():
        try:
            texto = await _transcrever_aws(caminho)
            if texto and texto.strip():
                return texto.strip(), "aws"
            raise RuntimeError("AWS Transcribe devolveu transcrição vazia")
        except Exception as e:
            if erro_openai:
                raise RuntimeError(f"OpenAI: {erro_openai} | AWS: {e}") from e
            raise

    if erro_openai:
        raise RuntimeError(f"{erro_openai} (AWS Transcribe indisponível: sem aws CLI ou credencial)")
    raise RuntimeError("Nenhum provedor de transcrição configurado "
                       "(defina OPENAI_API_KEY ou configure credenciais AWS)")
