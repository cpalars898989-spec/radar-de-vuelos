# RADAR DE VUELOS

Sistema local que consulta precios reales en Travelpayouts, conserva una observación diaria por ruta en SQLite, muestra el histórico en un panel web y envía alertas o responde preguntas por Telegram.

## Arquitectura

```text
radar.py -> Travelpayouts -> radar.db -> app.py + index.html
                                  |
                                  +-> bot.py -> Telegram -> codex exec
```

Todo el código está en la raíz. No usa frameworks. Python utiliza la librería estándar y el panel es un único archivo HTML sin dependencias externas.

## Archivos

- `radar.py`: consulta Travelpayouts y guarda la oferta más barata del día.
- `radar.db`: histórico local excluido de Git.
- `app.py`: servidor HTTP y API interna.
- `index.html`: panel de rutas, precios e histórico.
- `bot.py`: alertas, comandos de Telegram y preguntas mediante `codex exec`.
- `.env`: credenciales locales excluidas de Git.
- `.env.example`: plantilla sin credenciales.
- `AGENTS.md`: alcance y reglas del proyecto.

## Configuración

Se requiere Python 3.11 o posterior. Copia `.env.example` como `.env` y completa:

```dotenv
FLIGHT_API_TOKEN=
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

Nunca publiques `.env`. Si un token aparece en una captura, revócalo y genera uno nuevo antes de entregar el repositorio.

## Uso

En Windows, la forma más sencilla es hacer doble clic en `INICIAR_RADAR.cmd`.
El lanzador detecta Python, elige un puerto disponible y abre el panel en el navegador.

Consultar Travelpayouts y guardar el precio diario:

```powershell
python radar.py
```

Iniciar el panel:

```powershell
python app.py
```

Después abre [http://127.0.0.1:8000](http://127.0.0.1:8000).

Desde el panel puedes agregar varias rutas con códigos IATA, asignarles un precio
objetivo, seleccionar cuál visualizar y consultar los precios de todas las rutas activas.

Enviar una alerta con la última observación:

```powershell
python bot.py --alert
```

Escuchar mensajes del bot:

```powershell
python bot.py
```

Comandos disponibles en Telegram:

- `/start`: muestra la ayuda.
- `/precio`: devuelve la última observación guardada.
- Cualquier pregunta libre: se envía a `codex exec`, que consulta `radar.db` en modo de solo lectura.

También se puede probar Codex sin Telegram:

```powershell
python bot.py --ask "¿Cuál es el último precio registrado?"
```

## Tarea diaria

En Windows, crea una tarea en el Programador de tareas que ejecute `python radar.py` una vez al día desde esta carpeta. Usa una cuenta que tenga acceso a `.env` y conexión a Internet.

## Datos y oportunidad

SQLite contiene:

- `rutas`: origen, destino, estado y precio objetivo.
- `precios`: precio, moneda, aerolínea, fecha de vuelo y fecha de consulta.

La restricción `UNIQUE (ruta_id, fecha_consulta)` impide guardar dos observaciones de la misma ruta el mismo día. El panel marca una oportunidad cuando el precio alcanza el objetivo configurado o cae al menos 15 % frente al promedio histórico.

## APIs validadas

Travelpayouts se probó con:

```text
GET /aviasales/v3/prices_for_dates?origin=LIM&destination=CUZ&currency=usd
HTTP 200
success: true
currency: usd
data: lista de ofertas
```

Campos utilizados: `price`, `airline`, `departure_at` y `transfers`.

Telegram se probó con `getMe`: respondió HTTP 200 con `ok: true`. También se realizó un envío real mediante `sendMessage`.

## Limitación conocida

La sesión integrada de Codex no permitió iniciar una segunda instancia de `codex exec` y devolvió `Access denied` al inicializar su cliente interno. `bot.py` conserva la integración solicitada; debe verificarse desde una terminal externa con una instalación autenticada de Codex CLI.

## Guion de demostración

1. Ejecutar `python radar.py` y mostrar el precio real.
2. Abrir el panel y mostrar la observación en SQLite.
3. Ejecutar `python bot.py --alert` y enseñar el mensaje recibido.
4. Mantener `python bot.py` activo y hacer una pregunta por Telegram.
5. Confirmar una propuesta antes de crear un evento en Google Calendar.

La entrega debe contener el enlace al repositorio y un video de aproximadamente tres minutos. El video debe mostrar un precio real, el histórico, una pregunta por Telegram y la propuesta de evento.

