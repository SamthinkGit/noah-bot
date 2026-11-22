# Archivo

Comandos retirados de Noah. No se importan ni se registran: el código se guarda
aquí por si algún día se quieren recuperar.

| Comando | Archivos | Datos (no versionados) |
| --- | --- | --- |
| `.noah gochi` | `commands/gochi.py`, `modules/noah_gochi.py`, `modules/noah_gochi_ai.py` | `noah_gochi.json` |
| `.noah relics` | `commands/relics.py`, `modules/relics_game.py` | `noah_relics.json` |

Para reactivar uno: devuelve sus archivos a `src/noah_bot/commands` y
`src/noah_bot/modules`, vuelve a añadir su manager en `modules/bot_context.py` y su
`register_*_commands` en `commands/noah.py` (y la fila en `NOAH_HELP_ROWS`).
