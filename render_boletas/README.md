# Descargador de Boletas PDF — Render + Docker

Proyecto listo para desplegar en Render usando Docker.

## Qué cambia respecto a la versión local

- Se elimina el selector de carpeta local con `tkinter`.
- Los archivos se procesan temporalmente en el servidor.
- El usuario descarga un ZIP al finalizar.
- Se agrega fallback real para ChromeDriver:
  1. `webdriver-manager`
  2. `CHROMEDRIVER_PATH`
  3. rutas comunes de Linux/Windows
  4. arranque sin `Service(...)`
- Se agrega soporte para `CHROME_BINARY`.

## Archivos

- `app.py`: app Streamlit adaptada para servidor.
- `Dockerfile`: imagen con Chromium y ChromeDriver.
- `requirements.txt`: dependencias Python.
- `render.yaml`: despliegue como servicio web en Render.
- `.dockerignore`: exclusiones del build.

## Despliegue en Render

1. Sube esta carpeta a un repositorio de GitHub.
2. En Render, crea un **New Web Service**.
3. Elige **Build and deploy from a Git repository**.
4. Conecta el repositorio.
5. Render detectará el `render.yaml` o el `Dockerfile`.
6. Despliega.

## Variables útiles

No son obligatorias porque el Dockerfile ya las deja configuradas, pero puedes sobreescribir:

- `CHROME_BINARY=/usr/bin/chromium`
- `CHROMEDRIVER_PATH=/usr/bin/chromedriver`
- `APP_OUTPUT_DIR=/tmp/boletas_app`
- `KEEP_JOB_DIR=0`

## Nota

El plan free de Render puede “dormir” el servicio cuando no se usa. Para una app de uso ocasional por 1–2 usuarios esto suele ser suficiente.
