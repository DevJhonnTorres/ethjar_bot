# ethjar_bot

Bot de Telegram en Python con:
- Respuestas por IA (OpenClaw o fallback remoto).
- Consulta de precio de criptomonedas en tiempo real usando CoinMarketCap.
- Respuesta rápida para "quiénes somos" con `https://www.ethcali.org/`.

## Requisitos

- Python 3.9+
- `pip`
- Token de Telegram de BotFather
- API Key de CoinMarketCap (para precios cripto)

Instalar dependencias:

```bash
pip3 install python-telegram-bot requests
```

## Variables de entorno

Mínimas:

```bash
export TELEGRAM_TOKEN="TU_TOKEN_DE_BOTFATHER"
export COINMARKETCAP_API_KEY="TU_CMC_API_KEY"
```

Opcionales:

```bash
export BOT_NAME="OpenClaw"
export COINMARKETCAP_CONVERT="USD"

# OpenClaw por HTTP (si tienes backend local en :8000)
export USE_OPENCLAW_HTTP=1
export OPENCLAW_API="http://localhost:8000/chat"

# OpenClaw por CLI (si tienes binario openclaw instalado)
export USE_OPENCLAW_CLI=1

# Fallback remoto
export POLLINATIONS_OPENAI_URL="https://text.pollinations.ai/openai"
export POLLINATIONS_URL="https://text.pollinations.ai"
```

Nota: si activas `USE_OPENCLAW_CLI=1`, ese modo tiene prioridad sobre `USE_OPENCLAW_HTTP`.

## Ejecutar local

```bash
python3 -B telegram_jarvis.py
```

## Funcionalidades

1. Precio de cripto:
   - Ejemplos: `precio de btc`, `cuánto vale ethereum`, `precio SOL`.
   - Consulta CoinMarketCap y devuelve precio + cambio 24h.
2. Quiénes somos:
   - Ejemplos: `quienes somos`, `about us`.
   - Responde: `https://www.ethcali.org/`
3. Chat general:
   - Usa OpenClaw (si está habilitado y disponible) o fallback remoto.
4. Resistencia a fallos:
   - Cooldown por backend ante `429`, `timeout`, `connection refused`.
   - Fallback local si proveedores externos están saturados.

## Usar OpenClaw

### Opción A: OpenClaw HTTP

Usa tu propio servicio local:

```bash
export USE_OPENCLAW_HTTP=1
export OPENCLAW_API="http://localhost:8000/chat"
python3 -B telegram_jarvis.py
```

### Opción B: OpenClaw CLI

Si tienes `openclaw` instalado y disponible en PATH:

```bash
export USE_OPENCLAW_CLI=1
python3 -B telegram_jarvis.py
```

## Despliegue recomendado

Para esta arquitectura (polling), usa una VM persistente (ej. EC2).

### Ejecución en segundo plano

```bash
nohup python3 -B telegram_jarvis.py >/tmp/telegram_jarvis.log 2>&1 &
tail -f /tmp/telegram_jarvis.log
```

## Troubleshooting

1. "No responde":
   - Verifica que el proceso esté vivo.
   - Revisa logs en `/tmp/telegram_jarvis.log`.
2. "COINMARKETCAP_API_KEY no configurada":
   - Exporta la variable antes de arrancar.
3. "openclaw-http connection refused":
   - El backend en `OPENCLAW_API` no está arriba.
   - O desactiva `USE_OPENCLAW_HTTP`.
4. Respuestas limitadas por rate-limit:
   - El bot ya aplica cooldown y fallback, pero intenta mensajes más cortos.

## Seguridad

- Nunca subas tokens o API keys al repo.
- Usa variables de entorno en local/servidor.
- Si un token se expone, rótalo de inmediato.
