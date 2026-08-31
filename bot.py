"""Bot de Telegram para alertas y preguntas sobre el radar de vuelos."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "radar.db"
TELEGRAM_API = "https://api.telegram.org"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        raise RuntimeError(f"No existe {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def telegram_request(
    token: str, method: str, data: dict[str, str] | None = None
) -> dict[str, Any]:
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram respondió HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"No se pudo conectar con Telegram: {error.reason}") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"Telegram devolvió un error: {payload}")
    return payload


def latest_price() -> dict[str, Any]:
    if not DB_PATH.is_file():
        raise RuntimeError("Todavía no existe radar.db")
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT r.origen, r.destino, p.precio, p.moneda, p.aerolinea,
                   p.fecha_vuelo, p.fecha_consulta
            FROM precios AS p
            JOIN rutas AS r ON r.id = p.ruta_id
            ORDER BY p.fecha_consulta DESC, p.id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("El histórico todavía no contiene precios")
    return dict(row)


def price_message() -> str:
    price = latest_price()
    return (
        f"Vuelo {price['origen']} -> {price['destino']}\n"
        f"Precio: {price['precio']:.2f} {price['moneda'].upper()}\n"
        f"Aerolínea: {price['aerolinea']}\n"
        f"Salida: {price['fecha_vuelo']}\n"
        f"Consultado: {price['fecha_consulta']}"
    )


def find_codex() -> Path:
    command = shutil.which("codex")
    if command:
        return Path(command)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates = list(
            Path(local_app_data).glob("OpenAI/Codex/bin/*/codex.exe")
        )
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)
    raise RuntimeError("No se encontró la CLI de Codex")


def ask_codex(question: str) -> str:
    prompt = f"""
Responde la pregunta del usuario usando únicamente evidencia de radar.db.
Puedes inspeccionar el esquema y consultar SQLite con Python en modo de solo lectura.
No leas .env, no muestres secretos y no modifiques ningún archivo.
Si no hay datos suficientes, dilo claramente. Responde en español, de forma breve,
incluyendo cifras y fechas que sostengan la conclusión.

Pregunta: {question}
""".strip()
    command = [
        str(find_codex()),
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-C",
        str(ROOT),
        prompt,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Codex tardó demasiado en responder") from error
    answer = result.stdout.strip()
    if result.returncode != 0 or not answer:
        detail = result.stderr.strip().splitlines()
        last_line = detail[-1] if detail else "sin detalle"
        raise RuntimeError(f"Codex no pudo responder: {last_line}")
    return answer[-4000:]


def send_message(token: str, chat_id: str, text: str) -> None:
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})


def handle_message(token: str, allowed_chat_id: str, message: dict[str, Any]) -> None:
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return
    chat_id = str(chat.get("id", ""))
    if chat_id != allowed_chat_id:
        return

    command = text.strip()
    try:
        if command == "/start":
            answer = (
                "Radar de vuelos activo.\n"
                "Usa /precio para ver la última observación o escribe una pregunta."
            )
        elif command == "/precio":
            answer = price_message()
        else:
            answer = ask_codex(command)
    except RuntimeError as error:
        answer = f"No pude completar la consulta: {error}"
    send_message(token, chat_id, answer)


def poll(token: str, chat_id: str, once: bool = False) -> None:
    offset = 0
    while True:
        payload = telegram_request(
            token,
            "getUpdates",
            {"timeout": "25", "offset": str(offset), "allowed_updates": '["message"]'},
        )
        updates = payload.get("result", [])
        if not isinstance(updates, list):
            raise RuntimeError("Cambió el esquema de getUpdates")
        for update in updates:
            if not isinstance(update, dict) or "update_id" not in update:
                raise RuntimeError("Cambió el esquema de una actualización de Telegram")
            offset = int(update["update_id"]) + 1
            message = update.get("message")
            if isinstance(message, dict):
                handle_message(token, chat_id, message)
        if once:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert", action="store_true", help="Envía el último precio")
    parser.add_argument("--once", action="store_true", help="Consulta Telegram una vez")
    parser.add_argument("--ask", metavar="QUESTION", help="Prueba Codex sin Telegram")
    args = parser.parse_args()

    config = load_env()
    token = config.get("TELEGRAM_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en .env")

    if args.ask:
        print(ask_codex(args.ask))
    elif args.alert:
        send_message(token, chat_id, price_message())
        print("Alerta enviada por Telegram")
    else:
        poll(token, chat_id, once=args.once)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}")
        raise SystemExit(1)

