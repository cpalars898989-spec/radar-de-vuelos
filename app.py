"""Servidor web local y API interna del radar de vuelos."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import radar


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "radar.db"
INDEX_PATH = ROOT / "index.html"
LOCAL_TIMEZONE = ZoneInfo("America/Lima")


def load_dashboard() -> dict[str, Any]:
    if not DB_PATH.is_file():
        raise RuntimeError("Todavía no existe radar.db. Ejecuta radar.py primero.")

    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        routes = connection.execute(
            """
            SELECT id, origen, destino, activa, precio_objetivo
            FROM rutas
            ORDER BY activa DESC, origen, destino
            """
        ).fetchall()
        prices = connection.execute(
            """
            SELECT p.id, p.ruta_id, r.origen, r.destino, p.precio,
                   p.moneda, p.aerolinea, p.fecha_vuelo, p.fecha_consulta
            FROM precios AS p
            JOIN rutas AS r ON r.id = p.ruta_id
            ORDER BY p.fecha_consulta, p.id
            """
        ).fetchall()
    finally:
        connection.close()

    price_data = [dict(row) for row in prices]
    route_data: list[dict[str, Any]] = []
    for route in routes:
        observations = [row for row in price_data if row["ruta_id"] == route["id"]]
        latest = observations[-1] if observations else None
        average = (
            sum(float(row["precio"]) for row in observations) / len(observations)
            if observations
            else None
        )
        variation = (
            ((float(latest["precio"]) - average) / average) * 100
            if latest and average
            else None
        )
        target = route["precio_objetivo"]
        opportunity = bool(
            latest
            and (
                (target is not None and float(latest["precio"]) <= float(target))
                or (variation is not None and variation <= -15)
            )
        )
        route_data.append(
            {
                **dict(route),
                "observaciones": len(observations),
                "promedio": round(average, 2) if average is not None else None,
                "variacion_pct": round(variation, 2) if variation is not None else None,
                "oportunidad": opportunity,
                "ultimo": latest,
            }
        )

    return {
        "generated_at": datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds"),
        "routes": route_data,
        "prices": price_data,
    }


def refresh_prices(route_id: int | None = None) -> list[dict[str, Any]]:
    config = radar.load_env()
    token = config.get("FLIGHT_API_TOKEN", "")
    if not token:
        raise RuntimeError("FLIGHT_API_TOKEN está vacío en .env")

    results: list[dict[str, Any]] = []
    connection = radar.connect_database()
    try:
        query = (
            "SELECT id, origen, destino, precio_objetivo "
            "FROM rutas WHERE activa = 1"
        )
        parameters: tuple[Any, ...] = ()
        if route_id is not None:
            query += " AND id = ?"
            parameters = (route_id,)
        routes = connection.execute(query + " ORDER BY id", parameters).fetchall()
        if not routes:
            raise RuntimeError("No existe la ruta activa seleccionada")
        for current_id, origin, destination, target in routes:
            results.append(
                radar.process_route(
                    connection, config, current_id, origin, destination, target
                )
            )
    finally:
        connection.close()
    return results


def upsert_route(origin: str, destination: str, target: Any) -> dict[str, Any]:
    origin = origin.strip().upper()
    destination = destination.strip().upper()
    if len(origin) != 3 or not origin.isalpha():
        raise RuntimeError("El origen debe ser un código IATA de 3 letras")
    if len(destination) != 3 or not destination.isalpha():
        raise RuntimeError("El destino debe ser un código IATA de 3 letras")
    if origin == destination:
        raise RuntimeError("El origen y el destino deben ser diferentes")
    if target in (None, ""):
        target_value = None
    else:
        try:
            target_value = float(target)
        except (TypeError, ValueError) as error:
            raise RuntimeError("El precio objetivo debe ser numérico") from error
        if target_value <= 0:
            raise RuntimeError("El precio objetivo debe ser mayor que cero")

    connection = radar.connect_database()
    try:
        connection.execute(
            """
            INSERT INTO rutas (origen, destino, activa, precio_objetivo)
            VALUES (?, ?, 1, ?)
            ON CONFLICT (origen, destino) DO UPDATE SET
                activa = 1,
                precio_objetivo = excluded.precio_objetivo
            """,
            (origin, destination, target_value),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT id, origen, destino, activa, precio_objetivo
            FROM rutas WHERE origen = ? AND destino = ?
            """,
            (origin, destination),
        ).fetchone()
    finally:
        connection.close()
    return {
        "id": row[0],
        "origen": row[1],
        "destino": row[2],
        "activa": row[3],
        "precio_objetivo": row[4],
    }


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "RadarVuelos/1.0"

    def send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, "application/json; charset=utf-8", body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("El cuerpo debe ser JSON válido") from error
        if not isinstance(payload, dict):
            raise RuntimeError("El cuerpo JSON debe ser un objeto")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            if not INDEX_PATH.is_file():
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Falta index.html"})
                return
            self.send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                INDEX_PATH.read_bytes(),
            )
            return

        if path == "/api/dashboard":
            try:
                self.send_json(HTTPStatus.OK, load_dashboard())
            except RuntimeError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return

        if path == "/api/health":
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, "database": DB_PATH.is_file()},
            )
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/routes":
            try:
                payload = self.read_json()
                route = upsert_route(
                    str(payload.get("origin", "")),
                    str(payload.get("destination", "")),
                    payload.get("target"),
                )
                results = refresh_prices(route["id"])
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "ok": True,
                        "route": route,
                        "results": results,
                        "dashboard": load_dashboard(),
                    },
                )
            except RuntimeError as error:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(error)})
            return

        if path == "/api/refresh":
            try:
                payload = self.read_json()
                selected_route = payload.get("route_id")
                route_id = int(selected_route) if selected_route is not None else None
                results = refresh_prices(route_id)
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True, "results": results, "dashboard": load_dashboard()},
                )
            except RuntimeError as error:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(error)})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="Abre el panel en el navegador")
    parser.add_argument("--with-bot", action="store_true", help="Inicia también el bot")
    args = parser.parse_args()

    server = None
    selected_port = args.port
    for candidate in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer((args.host, candidate), RadarHandler)
            selected_port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(
            f"No hay un puerto disponible entre {args.port} y {args.port + 9}"
        )

    url = f"http://{args.host}:{selected_port}"
    print(f"Panel disponible en {url}")
    print("Mantén esta ventana abierta. Presiona Ctrl+C para detener el radar.")
    if args.open:
        webbrowser.open(url)
    if args.with_bot:
        import bot

        config = bot.load_env()
        thread = threading.Thread(
            target=bot.poll,
            args=(config.get("TELEGRAM_TOKEN", ""), config.get("TELEGRAM_CHAT_ID", "")),
            daemon=True,
            name="telegram-bot",
        )
        thread.start()
        print("Bot de Telegram escuchando mensajes.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Servidor detenido")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

