# Noah Bot

Noah es un bot de Discord diseñado para el servidor **Chill de Cojines**, con funcionalidades como:
- Waifuracer (timers, leaderboards, rankings)
- Reacciones personalizadas por usuario
- Comandos de IA (preguntas, resúmenes, ayuda)
- Personalidad propia 😈🖤

Este proyecto utiliza **uv** para la gestión del entorno y dependencias.

---

## 🧩 Requisitos

- Python **3.10 o superior**
- Tener instalado **uv**

Si no tienes `uv`, instálalo siguiendo la guía oficial:  
👉 https://docs.astral.sh/uv/

---

## 📦 Instalación

1. Clona el repositorio:

```bash
git clone https://github.com/SamthinkGit/noah-bot.git
cd noah-bot
````

2. Sincroniza el entorno y las dependencias con `uv`:

```bash
uv sync
```

Esto creará automáticamente el entorno virtual y descargará las dependencias necesarias usando `uv.lock`.

---

## 🔐 Variables de entorno

Debes crear un fichero **`.env`** en la raíz del proyecto con el token de Discord.

### 📄 `.env`

```env
DISCORD_BOT_TOKEN="YourBotToken"
# Opcional: la API key de OpenAI también se puede poner desde `.noah ai config`
OPENAI_API_KEY="sk-..."
# Opcional: registrar las llamadas de IA en LangSmith (también desde `.noah ai config`)
LANGCHAIN_API_KEY="lsv2_..."
```

---

## ▶️ Ejecutar el bot

Una vez instalado todo y creado el `.env`, ejecuta el bot con:

```bash
uv run noah-bot
```

Si todo está correcto, deberías ver algo como:

```
Bot connected as Noah
```



---

## 🖤 Noah AI

Chats **privados** con Noah (hilos privados) en los que puede investigar el servidor con
herramientas de **solo lectura**: leer canales, buscar mensajes, presentaciones, actividad
de voz, con quién habla o coincide alguien en VC, etc.

- `.noah ai config` (admins): modelo (por defecto `gpt-6-luna`), API key de OpenAI, canales
  (panel, chats privados y avisos al staff), límite diario, canales que Noah puede leer,
  temas que puede comentar y publicar el panel.
- `.noah ai chats` (admins): últimos chats abiertos.
- La misma API key y modelo los usan `.noah ask`, `.noah summary` y `.noah behonest`.
- LangSmith (opcional): con su API key, cada pregunta queda registrada con sus llamadas al
  modelo y a las herramientas. Se envían las preguntas, los mensajes leídos y las respuestas.

**Sandbox**
- Ninguna herramienta escribe ni borra nada.
- Qué canales puede leer cada usuario se comprueba en código: solo los de la lista del admin
  que además puede ver ese usuario. Nunca hilos privados, ni el canal de chats ni el de avisos.
- Los admins pueden pedirle que lea cualquier canal, los chats de Noah, la auditoría y los eventos.
- Si alguien intenta sacar información restringida, Noah le avisa; al segundo intento cierra
  el chat y avisa al staff.
- Los niveles de voz nunca se comentan.

**Permisos del canal de chats**: todos con *Ver canal* y *Enviar mensajes en hilos* (sin
*Enviar mensajes* ni *Crear hilos*). El bot necesita *Crear hilos privados*, *Enviar mensajes
en hilos* y *Gestionar hilos*. Para la auditoría, *Ver registro de auditoría*.

**Datos**: `noah_ai.json` (config, preferencias, uso y registro de chats; contiene la API key,
está en `.gitignore`) y `voice_social.json` (medias de voz y coincidencias en VC; es aparte
de `voice_stats.json` y no afecta a los niveles).

---

## 🛡️ Moderación

Comandos de **clic derecho → Aplicaciones**, solo para administradores (Discord los oculta al resto):

- En un mensaje: **Marcar mensaje** (infracción, se queda), **Eliminar y marcar** (guarda copia y
  lo borra) y **Guardar evidencia** (sin contar como infracción). Abren un formulario con motivo
  y gravedad opcionales.
- En un usuario: **Ver expediente** y **Añadir nota** (texto, referencia y hasta 10 capturas).

Cada caso se publica en el canal de logs con los adjuntos resubidos, así se conservan aunque el
mensaje se borre. Al usuario no se le avisa. Primero: `.noah mod config #canal-staff`.

Consultas (`.noah mod help`): `flags`, `flags @user…`, `user @user`, `notes @user`, `case <nº>`,
`note @user <texto>` (con capturas o respondiendo a un mensaje) y `remove <nº>`.

**Datos**: `modlog.json` (en `.gitignore`).
