# Secretaria Virtual v6.0

Bot personal en Telegram que usa Gemini como router NLP para manejar Google Calendar y una base local SQLite, con un resumen diario programado.

## Requisitos
- Python 3.13
- Credenciales de Google OAuth2 (`credentials.json` + `token.json` generado en la primera ejecucion)
- Tokens de Telegram y Gemini en variables de entorno

## Instalacion local (Windows 11)
1. Crear y activar entorno: `python -m venv entorno_bot; .\entorno_bot\Scripts\activate`
2. Instalar dependencias: `pip install -r requirements.txt`
3. Colocar `credentials.json` en la raiz del proyecto
4. Exportar variables: `set TELEGRAM_TOKEN=...`, `set GEMINI_API_KEY=...`, `set ID_SEGUNDO_CALENDARIO=...`, `set MI_CHAT_ID=...`
5. Ejecutar: `python bot.py`. La primera vez abrira un localhost para autorizar Google Calendar y guardara `token.json`
6. La base local SQLite se crea automaticamente como `cerebro.db` cuando arranca el bot

## Despliegue en Orange Pi (Armbian + systemd)
1. Copiar el repo y `credentials.json` a la placa
2. Crear venv: `python3 -m venv entorno_bot && source entorno_bot/bin/activate`
3. `pip install -r requirements.txt`
4. Exportar las mismas variables en `/etc/environment` o en un archivo `.env` leido por el servicio
5. Archivo de servicio ejemplo `/etc/systemd/system/secretaria.service`:

```ini
[Unit]
Description=Secretaria Virtual Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/opi/secretaria_local
EnvironmentFile=/home/opi/secretaria_local/.env
ExecStart=/home/opi/secretaria_local/entorno_bot/bin/python /home/opi/secretaria_local/bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

6. `sudo systemctl daemon-reload && sudo systemctl enable --now secretaria.service`

## Notas de seguridad
- No commitees `token.json` ni `credentials.json`
- Si cambias de entorno o usuario, elimina `token.json` para regenerar credenciales de Google

## Proximos pasos
- Refactor modular para separar Gemini router, Calendar y SQLite
- Agregar metricas de hardware para el despliegue en Orange Pi
