"""Consulta precios de vuelos y guarda el histórico diario en SQLite."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "radar.db"
API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
LOCAL_TIMEZONE = ZoneInfo("America/Lima")
REQUIRED_OFFER_FIELDS = {
    "price",
    "airline",
    "departure_at",
    "transfers",
}


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Lee variables simples KEY=VALUE sin modificar el entorno del proceso."""
    if not path.is_file():
        raise RuntimeError(f"No existe el archivo de configuración: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def connect_database(path: Path = DB_PATH) -> sqlite3.Connection:
    """Abre SQLite y crea el esquema requerido si todavía no existe."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS rutas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origen TEXT NOT NULL,
            destino TEXT NOT NULL,
            activa INTEGER NOT NULL DEFAULT 1 CHECK (activa IN (0, 1)),
            precio_objetivo REAL,
            UNIQUE (origen, destino)
        );

        CREATE TABLE IF NOT EXISTS precios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ruta_id INTEGER NOT NULL,
            precio REAL NOT NULL,
            moneda TEXT NOT NULL,
            aerolinea TEXT NOT NULL,
            fecha_vuelo TEXT NOT NULL,
            fecha_consulta TEXT NOT NULL,
            FOREIGN KEY (ruta_id) REFERENCES rutas(id),
            UNIQUE (ruta_id, fecha_consulta)
        );
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO rutas (origen, destino, activa, precio_objetivo)
        VALUES (?, ?, 1, NULL)
        """,
        ("LIM", "CUZ"),
    )
    connection.commit()
    return connection


def fetch_offers(token: str, origin: str, destination: str) -> tuple[str, list[dict[str, Any]]]:
    """Consulta Travelpayouts y valida el esquema confirmado."""
    query = urllib.parse.urlencode(
        {"origin": origin, "destination": destination, "currency": "usd"}
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"X-Access-Token": token, "Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Travelpayouts respondió HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"No se pudo conectar con Travelpayouts: {error.reason}") from error

    if status != 200:
        raise RuntimeError(f"Travelpayouts respondió HTTP {status}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("Travelpayouts no devolvió JSON válido") from error

    if not isinstance(payload, dict):
        raise RuntimeError("El JSON de Travelpayouts no es un objeto")
    if payload.get("success") is not True:
        raise RuntimeError(f"Travelpayouts indicó un error: {payload.get('error')}")

    currency = payload.get("currency")
    offers = payload.get("data")
    if not isinstance(currency, str) or not isinstance(offers, list):
        raise RuntimeError("Cambió el esquema: se esperaban 'currency' y 'data' como lista")
    if not offers:
        raise RuntimeError(f"Travelpayouts no devolvió ofertas para {origin} -> {destination}")

    for offer in offers:
        if not isinstance(offer, dict) or not REQUIRED_OFFER_FIELDS.issubset(offer):
            raise RuntimeError("Cambió el esquema de las ofertas de Travelpayouts")
        if not isinstance(offer["price"], (int, float)):
            raise RuntimeError("Cambió el tipo del campo 'price'")
        if not isinstance(offer["airline"], str):
            raise RuntimeError("Cambió el tipo del campo 'airline'")
        if not isinstance(offer["departure_at"], str):
            raise RuntimeError("Cambió el tipo del campo 'departure_at'")
        if not isinstance(offer["transfers"], int):
            raise RuntimeError("Cambió el tipo del campo 'transfers'")

    return currency.lower(), offers


def send_telegram_alert(
    config: dict[str, str],
    origin: str,
    destination: str,
    offer: dict[str, Any],
    currency: str,
    reason: str,
) -> None:
    token = config.get("TELEGRAM_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en .env")
    text = (
        f"Oportunidad {origin} -> {destination}\n"
        f"Precio: {offer['price']} {currency.upper()}\n"
        f"Aerolínea: {offer['airline']}\n"
        f"Salida: {offer['departure_at']}\n"
        f"Escalas: {offer['transfers']}\n"
        f"Motivo: {reason}"
    )
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram respondió HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"No se pudo conectar con Telegram: {error.reason}") from error
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram devolvió un esquema o estado inesperado")


def save_cheapest_offer(
    connection: sqlite3.Connection,
    route_id: int,
    currency: str,
    offers: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Guarda la oferta mínima, respetando una observación diaria por ruta."""
    cheapest = min(offers, key=lambda offer: offer["price"])
    consultation_date = datetime.now(LOCAL_TIMEZONE).date().isoformat()
    already_exists = connection.execute(
        "SELECT 1 FROM precios WHERE ruta_id = ? AND fecha_consulta = ?",
        (route_id, consultation_date),
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO precios
            (ruta_id, precio, moneda, aerolinea, fecha_vuelo, fecha_consulta)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (ruta_id, fecha_consulta) DO UPDATE SET
            precio = excluded.precio,
            moneda = excluded.moneda,
            aerolinea = excluded.aerolinea,
            fecha_vuelo = excluded.fecha_vuelo
        """,
        (
            route_id,
            cheapest["price"],
            currency,
            cheapest["airline"],
            cheapest["departure_at"],
            consultation_date,
        ),
    )
    connection.commit()
    return cheapest, not already_exists


def process_route(
    connection: sqlite3.Connection,
    config: dict[str, str],
    route_id: int,
    origin: str,
    destination: str,
    target: float | None,
) -> dict[str, Any]:
    consultation_date = datetime.now(LOCAL_TIMEZONE).date().isoformat()
    previous_average = connection.execute(
        "SELECT AVG(precio) FROM precios WHERE ruta_id = ? AND fecha_consulta <> ?",
        (route_id, consultation_date),
    ).fetchone()[0]
    currency, offers = fetch_offers(config["FLIGHT_API_TOKEN"], origin, destination)
    cheapest, inserted = save_cheapest_offer(connection, route_id, currency, offers)

    reason = None
    if target is not None and float(cheapest["price"]) <= float(target):
        reason = f"precio igual o menor al objetivo de {target:.2f} {currency.upper()}"
    elif previous_average and float(cheapest["price"]) <= float(previous_average) * 0.85:
        drop = (1 - float(cheapest["price"]) / float(previous_average)) * 100
        reason = f"bajó {drop:.1f}% frente al promedio anterior"

    alert_sent = False
    if inserted and reason:
        send_telegram_alert(config, origin, destination, cheapest, currency, reason)
        alert_sent = True

    return {
        "route_id": route_id,
        "origin": origin,
        "destination": destination,
        "price": cheapest["price"],
        "currency": currency,
        "airline": cheapest["airline"],
        "departure_at": cheapest["departure_at"],
        "transfers": cheapest["transfers"],
        "action": "inserted" if inserted else "updated",
        "opportunity": reason is not None,
        "reason": reason,
        "alert_sent": alert_sent,
    }


def main() -> int:
    config = load_env()
    token = config.get("FLIGHT_API_TOKEN", "")
    if not token:
        raise RuntimeError("FLIGHT_API_TOKEN está vacío en .env")

    connection = connect_database()
    try:
        routes = connection.execute(
            "SELECT id, origen, destino, precio_objetivo "
            "FROM rutas WHERE activa = 1 ORDER BY id"
        ).fetchall()

        for route_id, origin, destination, target in routes:
            result = process_route(
                connection, config, route_id, origin, destination, target
            )
            action = "guardado" if result["action"] == "inserted" else "actualizado hoy"
            print(
                f"{origin} -> {destination}: {result['price']} "
                f"{result['currency'].upper()}, {result['airline']}, "
                f"{result['departure_at']}, escalas={result['transfers']} "
                f"({action})"
            )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}")
        raise SystemExit(1)

