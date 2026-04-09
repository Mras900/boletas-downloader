import csv
import logging
import os
import queue
import random
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests
import streamlit as st
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ──────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────
APP_TITLE = "Descargador de Boletas PDF"
DEFAULT_WORKERS = 2
DEFAULT_T_CARGA = 15
DEFAULT_T_DESC = 30
DEFAULT_REINTEN = 2
MSG_MIN_SEGUNDOS = 3.2
BASE_OUTPUT_DIR = Path(os.getenv("APP_OUTPUT_DIR", "/tmp/boletas_app"))
KEEP_JOB_DIR = os.getenv("KEEP_JOB_DIR", "0") == "1"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
# MODELOS
# ──────────────────────────────────────────────────────
@dataclass
class Tarea:
    indice: int
    url: str
    folio: str
    ruta_pdf: Path


@dataclass
class Resultado:
    tarea: Tarea
    ok: bool
    mensaje: str = ""


# ──────────────────────────────────────────────────────
# MENSAJES
# ──────────────────────────────────────────────────────
_MSGS_INICIO = [
    "🧠 Sabías que tu cerebro tiene más conexiones que estrellas en la Vía Láctea.",
    "⚡ Sabías que un rayo puede alcanzar temperaturas de hasta 30.000 °C.",
    "🌌 Sabías que el universo observable contiene miles de millones de galaxias.",
    "🌊 Sabías que más del 80 % del océano sigue sin explorarse por completo.",
    "🐙 Sabías que los pulpos tienen 3 corazones.",
    "☕ Sabías que una pausa breve puede mejorar la concentración.",
    "🛰️ Sabías que los satélites orbitan la Tierra a miles de kilómetros por hora.",
    "🌍 Sabías que la Tierra gira a más de 1.600 km/h sobre su eje.",
    "🔭 Sabías que la luz del Sol tarda cerca de 8 minutos en llegar a la Tierra.",
    "🦈 Sabías que los tiburones existen desde antes que los árboles.",
]

_MSGS_MEDIO = [
    "📊 Estás optimizando horas de trabajo en solo minutos.",
    "⚙️ Selenium está haciendo el trabajo repetitivo por ti.",
    "🚀 Automatizar tareas pequeñas genera grandes ahorros de tiempo.",
    "🤖 La automatización reduce errores y acelera procesos.",
    "🧘 Respira… el sistema está trabajando por ti.",
    "📁 Cada archivo procesado es una tarea menos por hacer manualmente.",
    "⏱️ Lo que antes tomaba mucho tiempo, ahora avanza solo.",
    "💡 Un buen flujo automatizado evita retrabajos innecesarios.",
    "📉 Menos tareas manuales, menos desgaste operativo.",
    "🤝 Mientras tú supervisas, la herramienta ejecuta.",
]

_MSGS_FINAL = [
    "🔥 Último tramo, ya queda poco.",
    "📦 Casi listo, los PDFs vienen en camino.",
    "🏁 Ya casi terminamos esta corrida.",
    "🎉 Ya se ve la meta cerca.",
    "✅ El proceso está entrando en su fase final.",
    "⏳ Estamos en la recta final.",
    "🌟 Ya casi puedes descargar todo el paquete final.",
    "🏆 Lo más pesado ya pasó.",
]

_MSGS_IDLE = [
    "Listo para comenzar. Configura el panel lateral.",
    "Carga tu archivo o pega una URL en el sidebar.",
    "Tip: usa 2 o 3 workers para mejor rendimiento.",
    "Esta herramienta acelera tareas repetitivas.",
    "📂 Sube un archivo para comenzar la descarga masiva.",
    "🔗 También puedes trabajar con una URL individual.",
    "⚙️ Ajusta los tiempos de espera según tu conexión o carga del sitio.",
    "📦 Al finalizar podrás descargar todos los PDFs en un ZIP.",
]


def _gen(msgs):
    cola = deque()

    def nxt():
        nonlocal cola
        if not cola:
            t = msgs.copy()
            random.shuffle(t)
            cola = deque(t)
        return cola.popleft()

    return nxt


_g_inicio = _gen(_MSGS_INICIO)
_g_medio = _gen(_MSGS_MEDIO)
_g_final = _gen(_MSGS_FINAL)


def obtener_mensaje_suave(completados: int, total: int, min_segundos: float = 3.2) -> str:
    ahora = time.time()

    if total <= 0:
        stage = "idle"
        pool = _MSGS_IDLE
    else:
        p = completados / total
        if p < 0.30:
            stage = "inicio"
            pool = _MSGS_INICIO
        elif p < 0.75:
            stage = "medio"
            pool = _MSGS_MEDIO
        else:
            stage = "final"
            pool = _MSGS_FINAL

    if st.session_state.msg_stage != stage:
        st.session_state.msg_stage = stage
        st.session_state.msg_actual = random.choice(pool)
        st.session_state.msg_last_change = ahora
        return st.session_state.msg_actual

    if st.session_state.msg_actual and (ahora - st.session_state.msg_last_change) < min_segundos:
        return st.session_state.msg_actual

    opciones = [m for m in pool if m != st.session_state.msg_actual]
    nuevo = random.choice(opciones if opciones else pool)

    st.session_state.msg_actual = nuevo
    st.session_state.msg_last_change = ahora
    return nuevo


# ──────────────────────────────────────────────────────
# UTILIDADES DATOS
# ──────────────────────────────────────────────────────
def limpiar_nombre(t: str) -> str:
    t = re.sub(r'[<>:"/\\|?*]+', "_", str(t).strip())
    return re.sub(r"\s+", "_", t) or "sin_folio"


def convertir_fecha(v) -> Optional[pd.Timestamp]:
    if v is None:
        return pd.NaT
    return pd.to_datetime(v, dayfirst=True, errors="coerce")


def normalizar_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).replace("\ufeff", "").replace("ï»¿", "").strip() for c in df.columns]
    return df


def normalizar_texto_columna(txt: str) -> str:
    txt = str(txt).strip().lower()
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
    txt = re.sub(r"[^a-z0-9]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def detectar_columna(columnas, candidatos):
    mapa = {c: normalizar_texto_columna(c) for c in columnas}

    for original, norm in mapa.items():
        if norm in candidatos:
            return original

    for original, norm in mapa.items():
        for cand in candidatos:
            if cand in norm or norm in cand:
                return original

    return None


def score_columna_fecha_por_contenido(df: pd.DataFrame, col: str, muestra: int = 30) -> int:
    try:
        serie = df[col].dropna().astype(str).head(muestra)
        if serie.empty:
            return 0
        validas = sum(pd.notna(convertir_fecha(v)) for v in serie)
        return validas
    except Exception:
        return 0


def detectar_columna_fecha_inteligente(df: pd.DataFrame):
    columnas = list(df.columns)

    fecha_auto = detectar_columna(columnas, {
        "fecha emision", "fecha de emision", "fecha emision dte", "fecha de emision dte",
        "fecha", "emision", "fecha doc", "fecha documento", "fecha de documento",
        "fec emision", "f emision"
    })
    if fecha_auto:
        return fecha_auto

    mejores = []
    for col in columnas:
        puntaje = score_columna_fecha_por_contenido(df, col)
        mejores.append((col, puntaje))

    mejores.sort(key=lambda x: x[1], reverse=True)
    if mejores and mejores[0][1] > 0:
        return mejores[0][0]

    return None


def leer_archivo(f) -> pd.DataFrame:
    nombre = f.name.lower()

    if nombre.endswith((".xlsx", ".xls")):
        return normalizar_cols(pd.read_excel(f, engine="openpyxl", dtype=str))

    contenido = f.getvalue()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(f.name).suffix)
    tmp.write(contenido)
    tmp.close()
    p = Path(tmp.name)

    try:
        for sep in ["\t", ";", ",", "|"]:
            try:
                df = pd.read_csv(p, sep=sep, dtype=str, encoding="utf-8-sig", engine="python")
                df = normalizar_cols(df)
                if len(df.columns) > 1:
                    return df
            except Exception:
                pass

        try:
            with open(p, "r", encoding="utf-8-sig", errors="ignore") as fh:
                dia = csv.Sniffer().sniff(fh.read(5000))
            return normalizar_cols(pd.read_csv(p, sep=dia.delimiter, dtype=str, encoding="utf-8-sig", engine="python"))
        except Exception:
            return normalizar_cols(pd.read_csv(p, sep="\t", dtype=str, encoding="utf-8-sig", engine="python"))
    finally:
        p.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────
# SELENIUM / CHROME
# ──────────────────────────────────────────────────────
def obtener_rutas_chromedriver_fallback() -> List[str]:
    candidatos = []
    env_path = os.getenv("CHROMEDRIVER_PATH")
    if env_path:
        candidatos.append(env_path)

    candidatos.extend([
        str(Path.cwd() / "drivers" / "chromedriver.exe"),
        str(Path.cwd() / "drivers" / "chromedriver"),
        str(Path.cwd() / "chromedriver.exe"),
        str(Path.cwd() / "chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ])

    vistos = set()
    salida = []
    for c in candidatos:
        if c and c not in vistos:
            vistos.add(c)
            salida.append(c)
    return salida


def resolver_driver_path() -> Optional[str]:
    try:
        path = ChromeDriverManager().install()
        if path and Path(path).exists():
            log.info("ChromeDriver obtenido con webdriver-manager: %s", path)
            return path
    except Exception as e:
        log.warning("webdriver-manager falló: %s", e)

    for path in obtener_rutas_chromedriver_fallback():
        try:
            if Path(path).exists():
                log.info("ChromeDriver encontrado en fallback: %s", path)
                return path
        except Exception:
            pass

    log.warning("No se encontró ChromeDriver explícito. Se intentará iniciar Chrome sin Service.")
    return None


def crear_driver(carpeta: Path, driver_path: Optional[str] = None) -> webdriver.Chrome:
    carpeta.mkdir(parents=True, exist_ok=True)

    opts = Options()
    for a in [
        "--headless=new",
        "--disable-gpu",
        "--window-size=1600,2200",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-popup-blocking",
        "--disable-extensions",
    ]:
        opts.add_argument(a)

    chrome_binary = os.getenv("CHROME_BINARY")
    if chrome_binary:
        opts.binary_location = chrome_binary

    opts.add_experimental_option("prefs", {
        "download.default_directory": str(carpeta.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "download.open_pdf_in_system_reader": False,
        "safebrowsing.enabled": True,
    })

    errores = []

    if driver_path:
        try:
            d = webdriver.Chrome(service=Service(driver_path), options=opts)
            d.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(carpeta.resolve())})
            return d
        except Exception as e:
            errores.append(f"Con Service('{driver_path}') falló: {e}")

    try:
        d = webdriver.Chrome(options=opts)
        d.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(carpeta.resolve())})
        return d
    except Exception as e:
        errores.append(f"Sin Service explícito falló: {e}")

    raise RuntimeError("No fue posible iniciar ChromeDriver.\n" + "\n".join(errores))


def cerrar_extras(driver):
    if not driver.window_handles:
        return
    principal = driver.window_handles[0]
    for h in driver.window_handles[1:]:
        driver.switch_to.window(h)
        driver.close()
    driver.switch_to.window(principal)


def click_descargar(driver, timeout):
    wait = WebDriverWait(driver, timeout)
    selectores = [
        (By.XPATH, "//button[contains(normalize-space(.), 'Descargar Archivo Actual')]"),
        (By.XPATH, "//a[contains(normalize-space(.), 'Descargar Archivo Actual')]"),
        (By.XPATH, "//*[@title='Descargar Archivo Actual']"),
        (By.XPATH, "//button[contains(normalize-space(.), 'PDF')]"),
        (By.XPATH, "//a[contains(normalize-space(.), 'PDF')]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'Descargar')]"),
        (By.XPATH, "//a[contains(normalize-space(.), 'Descargar')]"),
    ]

    err = None
    for by, sel in selectores:
        try:
            e = wait.until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", e)
            time.sleep(0.4)
            driver.execute_script("arguments[0].click();", e)
            return
        except Exception as ex:
            err = ex

    raise RuntimeError(f"Botón de descarga no encontrado: {err}")


def esperar_pdf(carpeta: Path, antes, timeout):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if list(carpeta.glob("*.crdownload")):
            time.sleep(0.3)
            continue

        nuevos = {p.name: p for p in carpeta.glob("*.pdf") if p.name not in antes}
        if nuevos:
            return max(nuevos.values(), key=lambda p: p.stat().st_mtime)

        time.sleep(0.3)
    return None


def sesion_desde_driver(driver):
    s = requests.Session()
    for c in driver.get_cookies():
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def payload_descarga(driver):
    return driver.execute_script("""
    const o={dteid:'',modulo:'',detalle:'',accion:''};
    for(const el of document.querySelectorAll('input[name],textarea[name],select[name]')){
        if(['dteid','modulo','detalle','accion'].includes(el.name)) o[el.name]=el.value||'';
    }
    return o;
    """) or {}


def descarga_directa(driver, tarea, timeout):
    p = payload_descarga(driver)
    if not p.get("dteid"):
        raise RuntimeError("Sin dteid")
    if not p.get("detalle"):
        raise RuntimeError("Sin detalle")

    p["accion"] = "DESCARGAR"
    p.setdefault("modulo", "")

    s = sesion_desde_driver(driver)
    r = s.post(
        "https://cantabria.getdte.cl/visor_accion_custodia.php",
        data=p,
        timeout=timeout,
        allow_redirects=True,
        headers={
            "Referer": tarea.url,
            "Origin": "https://cantabria.getdte.cl",
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    r.raise_for_status()

    ct = (r.headers.get("content-type") or "").lower()
    if "application/pdf" not in ct and not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"Respuesta no es PDF: {ct}")

    tarea.ruta_pdf.parent.mkdir(parents=True, exist_ok=True)
    tarea.ruta_pdf.write_bytes(r.content)


def descargar(driver, tarea, carpeta_temp, tc, td):
    cerrar_extras(driver)
    driver.get(tarea.url)

    WebDriverWait(driver, tc).until(
        lambda d: d.execute_script("const e=document.querySelector('input[name=\\\"dteid\\\"]');return !!(e&&e.value);")
    )

    try:
        descarga_directa(driver, tarea, td)
        return
    except Exception:
        pass

    antes = {p.name for p in carpeta_temp.glob("*.pdf")}
    ventanas_antes = set(driver.window_handles)

    click_descargar(driver, tc)

    nuevas = set(driver.window_handles) - ventanas_antes
    if nuevas:
        driver.switch_to.window(nuevas.pop())

    pdf = esperar_pdf(carpeta_temp, antes, td)
    if not pdf:
        raise RuntimeError("PDF no apareció en disco")

    tarea.ruta_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf), str(tarea.ruta_pdf))


def con_reintentos(driver, tarea, carpeta_temp, tc, td, reintentos, dp):
    msg = ""
    for i in range(1, reintentos + 2):
        try:
            descargar(driver, tarea, carpeta_temp, tc, td)
            return Resultado(tarea=tarea, ok=True, mensaje="OK"), driver
        except Exception as e:
            msg = str(e)
            try:
                driver.quit()
            except Exception:
                pass

            if i <= reintentos:
                time.sleep(2 ** i)
                driver = crear_driver(carpeta_temp, dp)

    return Resultado(tarea=tarea, ok=False, mensaje=msg), driver


def worker(wid, q_in, q_out, base, tc, td, reintentos, dp):
    carpeta = base / f"w{wid}"
    carpeta.mkdir(parents=True, exist_ok=True)

    d = None
    try:
        d = crear_driver(carpeta, dp)
        while True:
            t = q_in.get()
            if t is None:
                q_in.task_done()
                break

            res, d = con_reintentos(d, t, carpeta, tc, td, reintentos, dp)
            q_out.put(res)
            q_in.task_done()
    finally:
        if d:
            try:
                d.quit()
            except Exception:
                pass
        shutil.rmtree(carpeta, ignore_errors=True)


# ──────────────────────────────────────────────────────
# TAREAS / ZIP
# ──────────────────────────────────────────────────────
def construir_tareas(df, col_url, col_fecha, col_folio, salida):
    tareas, omitidos, warns = [], 0, []

    for i, row in df.iterrows():
        url = str(row.get(col_url, "")).strip()
        folio = limpiar_nombre(row.get(col_folio, f"fila_{i+1}"))
        fecha = convertir_fecha(row.get(col_fecha))

        if not url or url.lower() == "nan":
            warns.append(f"Fila {i+1}: sin URL")
            continue

        if pd.isna(fecha):
            warns.append(f"Fila {i+1}: fecha inválida")
            continue

        carpeta = salida / fecha.strftime("%Y") / fecha.strftime("%m") / fecha.strftime("%d")
        ruta = carpeta / f"boleta_{folio}.pdf"

        if ruta.exists():
            omitidos += 1
            continue

        tareas.append(Tarea(i + 1, url, folio, ruta))

    return tareas, omitidos, warns


def tarea_desde_url(url: str, salida: Path) -> Tarea:
    folio = limpiar_nombre(url.split("dteid=")[-1][:20] if "dteid=" in url else "boleta_url")
    ruta = salida / f"{folio}.pdf"
    return Tarea(indice=1, url=url, folio=folio, ruta_pdf=ruta)


def hacer_zip(resultados: List[Resultado]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in resultados:
            if r.ok and r.tarea.ruta_pdf.exists():
                arc = r.tarea.ruta_pdf.name
                try:
                    arc = "/".join(r.tarea.ruta_pdf.parts[-4:])
                except Exception:
                    pass
                zf.write(r.tarea.ruta_pdf, arc)
    return buf.getvalue()


def nombre_mes_es(numero_mes: int) -> str:
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
        7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    return meses.get(numero_mes, f"mes_{numero_mes}")


def construir_nombre_zip_desde_fechas(df: pd.DataFrame, col_fecha: str) -> str:
    if df is None or col_fecha not in df.columns:
        return "boletas_pdf.zip"

    fechas = df[col_fecha].apply(convertir_fecha).dropna()
    if fechas.empty:
        return "boletas_pdf.zip"

    fecha_min = fechas.min()
    fecha_max = fechas.max()
    dia_ini = fecha_min.strftime("%d")
    dia_fin = fecha_max.strftime("%d")
    mes_txt = nombre_mes_es(fecha_min.month)
    anio = fecha_min.strftime("%Y")

    if fecha_min.month == fecha_max.month and fecha_min.year == fecha_max.year:
        return f"boletas_{dia_ini}_al_{dia_fin}_{mes_txt}_{anio}.zip"

    mes_txt_fin = nombre_mes_es(fecha_max.month)
    anio_fin = fecha_max.strftime("%Y")
    return f"boletas_{dia_ini}_{mes_txt}_{anio}_al_{dia_fin}_{mes_txt_fin}_{anio_fin}.zip"


def crear_job_dir() -> Path:
    BASE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="job_", dir=str(BASE_OUTPUT_DIR)))


# ──────────────────────────────────────────────────────
# HTML / CSS
# ──────────────────────────────────────────────────────
def _kpi_html(proc, total, ok, err, vel) -> str:
    den = f"/{total}" if total else ""
    return (
        f"<div class='kpi-grid'>"
        f"<div class='kpi'><span class='kpi-lbl'>Procesadas</span><span class='kpi-val'>{proc}<span class='kpi-den'>{den}</span></span></div>"
        f"<div class='kpi'><span class='kpi-lbl'>Exitosas</span><span class='kpi-val kpi-ok'>{ok}</span></div>"
        f"<div class='kpi'><span class='kpi-lbl'>Errores</span><span class='kpi-val kpi-err'>{err}</span></div>"
        f"<div class='kpi'><span class='kpi-lbl'>Velocidad</span><span class='kpi-val'>{vel:.1f}<span class='kpi-den'> /min</span></span></div>"
        f"</div>"
    )


def _bateria(pct: float) -> str:
    color = "#22c55e" if pct >= 100 else "#3b82f6"
    return f"""
    <div class="bat-wrap">
      <div class="bat-header"><span>Progreso</span><span>{pct:.1f}%</span></div>
      <div class="bat-track"><div class="bat-fill" style="width:{pct}%;background:{color};"></div></div>
    </div>
    """


def ejecutar(tareas, workers_n, tc, td, reintentos, base_temp, log_sidebar, slot_kpi=None, slot_prog=None, slot_msg=None, slot_stat=None):
    total = len(tareas)
    if not total:
        return []

    slot_kpi = slot_kpi or st.empty()
    slot_prog = slot_prog or st.empty()
    slot_msg = slot_msg or st.empty()
    slot_stat = slot_stat or st.empty()

    tabla = None
    if log_sidebar:
        with st.sidebar:
            st.markdown("---")
            st.markdown("#### 📜 Actividad")
            tabla = st.empty()

    t0 = time.time()
    dp = resolver_driver_path()

    qi, qo = queue.Queue(), queue.Queue()
    for t in tareas:
        qi.put(t)
    for _ in range(workers_n):
        qi.put(None)

    hilos = []
    for wid in range(workers_n):
        h = threading.Thread(target=worker, args=(wid, qi, qo, base_temp, tc, td, reintentos, dp), daemon=True)
        h.start()
        hilos.append(h)

    resultados, log_rows = [], []

    while len(resultados) < total:
        try:
            res = qo.get(timeout=1)
        except queue.Empty:
            if not any(h.is_alive() for h in hilos):
                break
            continue

        resultados.append(res)
        n = len(resultados)
        vel = (n / max(time.time() - t0, 0.001)) * 60
        eta = (total - n) / (n / max(time.time() - t0, 0.001)) if n else 0
        ok = sum(1 for r in resultados if r.ok)
        err = sum(1 for r in resultados if not r.ok)
        pct = round(n / total * 100, 1)

        slot_kpi.markdown(_kpi_html(n, total, ok, err, vel), unsafe_allow_html=True)
        slot_prog.markdown(_bateria(pct), unsafe_allow_html=True)
        mensaje_ui = obtener_mensaje_suave(n, total, min_segundos=MSG_MIN_SEGUNDOS)
        slot_msg.markdown(f"<div class='msg-bubble msg-fade'>{mensaje_ui}</div>", unsafe_allow_html=True)
        slot_stat.markdown(
            f"<div class='status-pill'><b>{n}</b> / <b>{total}</b> procesadas &nbsp;·&nbsp; folio <code>{res.tarea.folio}</code> &nbsp;·&nbsp; ETA {eta:.0f}s</div>",
            unsafe_allow_html=True,
        )

        log_rows.append({
            "Hora": pd.Timestamp.now().strftime("%H:%M:%S"),
            "Folio": res.tarea.folio,
            "Estado": "✅" if res.ok else "❌",
            "Mensaje": res.mensaje,
        })

        if tabla is not None:
            tabla.dataframe(pd.DataFrame(log_rows[::-1]), use_container_width=True, height=260)

    for h in hilos:
        h.join(timeout=10)
    return resultados


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, .stApp {font-family:'Inter',sans-serif!important;background:#080d18!important;color:#e2e8f0!important;}
.block-container {max-width:820px!important;margin:0 auto!important;padding:1.5rem 1rem 1.8rem!important;width:100%!important;}
.main-shell {width:100%;max-width:820px;margin:0 auto;}
[data-testid="stSidebar"] {background:#0d1424!important;border-right:1px solid rgba(255,255,255,.06)!important;min-width:240px!important;max-width:240px!important;}
[data-testid="stSidebar"] > div {padding:1rem .85rem!important;}
[data-testid="stSidebar"] label,[data-testid="stSidebar"] p,[data-testid="stSidebar"] span:not(.stSelectbox span),[data-testid="stSidebar"] small {color:#94a3b8!important;font-size:.80rem!important;}
[data-testid="stSidebar"] h2 {font-size:1.05rem!important;font-weight:700!important;letter-spacing:0!important;color:#f8fafc!important;margin:0 0 .35rem!important;}
[data-testid="stSidebar"] h4 {font-size:.64rem!important;font-weight:700!important;text-transform:uppercase;letter-spacing:.05em!important;color:#475569!important;margin:.75rem 0 .35rem!important;}
[data-testid="stSidebar"] hr {border-color:rgba(255,255,255,.07)!important;margin:.65rem 0!important;}
[data-testid="stSidebar"] input[type="text"],[data-testid="stSidebar"] textarea {background:#111827!important;border:1px solid #1f2a44!important;color:#e2e8f0!important;border-radius:10px!important;font-size:.82rem!important;}
[data-testid="stSidebar"] [data-testid="stFileUploader"] > div > div {background:#111827!important;border:1px dashed #263657!important;border-radius:10px!important;padding:.7rem!important;}
[data-testid="stSidebar"] [data-testid="stSelectbox"] > div > div {background:#111827!important;border:1px solid #1f2a44!important;border-radius:10px!important;min-height:38px!important;}
[data-testid="stSidebar"] [data-testid="stSelectbox"] span {font-size:.82rem!important;color:#e2e8f0!important;}
.file-badge {display:flex;align-items:center;gap:7px;background:#0f213d;border:1px solid #214b90;border-radius:8px;padding:8px 10px;margin-top:6px;font-size:.75rem;color:#93c5fd;font-weight:500;}
.file-dot {width:7px;height:7px;background:#22c55e;border-radius:50%;flex-shrink:0;}
.hero {background:#0d1f38;border:1px solid #1a2f50;border-radius:14px;padding:14px 16px;margin-bottom:10px;}
.hero-tag {display:inline-block;background:rgba(37,99,235,.15);color:#60a5fa;border:1px solid rgba(37,99,235,.25);border-radius:20px;padding:3px 9px;font-size:.62rem;font-weight:700;letter-spacing:.05em!important;text-transform:uppercase;margin-bottom:8px;}
.hero-title {font-size:1.3rem!important;font-weight:700!important;letter-spacing:0!important;color:#f8fafc;line-height:1.2;margin:0 0 6px;}
.hero-sub {color:#94a3b8;font-size:.80rem;line-height:1.5;margin:0;}
.kpi-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:8px auto;}
.kpi {background:#0d1424;border:1px solid #1a2540;border-radius:12px;padding:10px 12px;min-height:70px;display:flex;flex-direction:column;justify-content:space-between;}
.kpi-lbl {font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em!important;color:#64748b;}
.kpi-val {font-size:1.4rem;font-weight:700;letter-spacing:0!important;color:#f8fafc;line-height:1.05;}
.kpi-den {font-size:.85rem;font-weight:500;color:#64748b;}.kpi-ok{color:#4ade80!important;}.kpi-err{color:#f87171!important;}
.bat-wrap {background:#0d1424;border:1px solid #1a2540;border-radius:10px;padding:8px 12px;margin:6px auto;}
.bat-header {display:flex;justify-content:space-between;font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em!important;color:#64748b;margin-bottom:6px;}
.bat-track {height:6px;background:#080d18;border-radius:999px;overflow:hidden;}.bat-fill{height:100%;border-radius:999px;transition:width .35s ease;}
.msg-bubble {background:#0d1424;border:1px solid #1a2540;border-radius:10px;padding:8px 12px;font-size:.80rem;text-align:center;color:#cbd5e1;margin:6px auto;transition:opacity .45s ease, transform .45s ease;}
.msg-fade {animation:fadeMessage .45s ease;}
@keyframes fadeMessage {0%{opacity:0;transform:translateY(4px);}100%{opacity:1;transform:translateY(0);}}
.status-pill {text-align:center;background:rgba(37,99,235,.07);border:1px solid rgba(37,99,235,.15);border-radius:10px;padding:7px 10px;font-size:.78rem;color:#93c5fd;margin:4px auto 6px auto;}
.status-pill code {background:rgba(255,255,255,.07);border-radius:4px;padding:1px 5px;font-size:.73rem;color:#f8fafc;}
div[data-testid="stButton"] {display:flex!important;justify-content:center!important;}
div[data-testid="stButton"] > button[kind="primary"] {max-width:260px!important;width:100%!important;height:44px!important;border-radius:10px!important;background:#2563eb!important;color:#ffffff!important;font-size:.90rem!important;font-weight:700!important;letter-spacing:0!important;border:none!important;}
.stDownloadButton > button {width:100%!important;border-radius:10px!important;background:#14532d!important;color:#86efac!important;border:1px solid #166534!important;font-weight:700!important;height:42px!important;}
#MainMenu, footer, header {visibility:hidden!important;}
@media (max-width:950px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media (max-width:700px){.block-container{max-width:100%!important;padding:1rem .8rem 1.4rem!important;}[data-testid="stSidebar"]{min-width:100%!important;max-width:100%!important;}.main-shell{max-width:100%!important;}.kpi-grid{grid-template-columns:1fr;}.hero-title{font-size:1.15rem!important;}.kpi-val{font-size:1.25rem!important;}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────
# SESSION STATE
# ──────────────────────────────────────────────────────
for k, v in [
    ("df", None),
    ("resultados", []),
    ("archivo_nombre", ""),
    ("url_individual", ""),
    ("folio_manual", ""),
    ("msg_actual", ""),
    ("msg_stage", ""),
    ("msg_last_change", 0.0),
]:
    if k not in st.session_state:
        st.session_state[k] = v


# ──────────────────────────────────────────────────────
# SIDEBAR
# ──────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📄 Configuración")

    st.markdown("#### Modo de entrada")
    modo = st.radio("Modo", ["📂  Archivo CSV / Excel", "🔗  URL individual"], label_visibility="collapsed")
    modo_archivo = modo.startswith("📂")
    st.markdown("---")

    df = st.session_state.df
    col_url = None
    col_fecha = None
    col_folio = None

    if modo_archivo:
        st.markdown("#### Archivo de entrada")
        archivo = st.file_uploader("Carga archivo", type=["csv", "txt", "xlsx", "xls"], label_visibility="collapsed")

        if archivo is not None:
            try:
                if st.session_state.archivo_nombre != archivo.name:
                    with st.spinner("Leyendo…"):
                        df_leido = leer_archivo(archivo)
                        st.session_state.df = df_leido
                        st.session_state.archivo_nombre = archivo.name
                        st.session_state.resultados = []

                df = st.session_state.df
                if df is not None:
                    st.markdown(f"<div class='file-badge'><span class='file-dot'></span>{archivo.name} · {len(df)} filas</div>", unsafe_allow_html=True)
                    columnas = list(df.columns)
                    url_auto = detectar_columna(columnas, {"url", "link", "enlace", "direccion url", "direccion", "link dte"})
                    folio_auto = detectar_columna(columnas, {"folio", "numero folio", "n folio", "num folio", "id", "numero documento", "n documento", "folio dte"})
                    fecha_auto = detectar_columna_fecha_inteligente(df)
                    idx_url = columnas.index(url_auto) if url_auto in columnas else 0
                    idx_fecha = columnas.index(fecha_auto) if fecha_auto in columnas else 0
                    idx_folio = columnas.index(folio_auto) if folio_auto in columnas else 0
                    st.markdown("#### Mapeo de columnas")
                    col_url = st.selectbox("Columna URL", columnas, index=idx_url)
                    col_fecha = st.selectbox("Columna fecha", columnas, index=idx_fecha)
                    col_folio = st.selectbox("Columna folio / ID", columnas, index=idx_folio)
            except Exception as e:
                st.error(f"No se pudo leer el archivo: {e}")
                df = None
                st.session_state.df = None
    else:
        st.markdown("#### URL de la boleta")
        st.session_state.url_individual = st.text_area(
            "Pega la URL completa de la boleta",
            value=st.session_state.get("url_individual", ""),
            placeholder="https://cantabria.getdte.cl/visor...",
            height=90,
            label_visibility="collapsed",
        )
        st.session_state.folio_manual = st.text_input(
            "Folio / nombre del archivo (opcional)",
            value=st.session_state.get("folio_manual", ""),
            placeholder="ej: 123456",
        )

    st.markdown("---")
    st.markdown("#### Parámetros de descarga")
    workers_n = st.slider("Workers paralelos", 1, 4, DEFAULT_WORKERS)
    t_carga = st.slider("Timeout carga (s)", 5, 60, DEFAULT_T_CARGA)
    t_descarga = st.slider("Timeout descarga (s)", 10, 120, DEFAULT_T_DESC)
    reintentos = st.slider("Reintentos máximos", 0, 5, DEFAULT_REINTEN)
    st.markdown("---")
    log_sidebar = st.checkbox("Actividad en tiempo real", value=True)
    st.caption("En servidor no se usa carpeta local del usuario. Los archivos se generan temporalmente y se entregan en un ZIP.")


# ──────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────
st.markdown("<div class='main-shell'>", unsafe_allow_html=True)
st.markdown(
    "<div class='hero'>"
    "<div class='hero-tag'>Render · Docker · Selenium</div>"
    "<p class='hero-title'>Descargador masivo de boletas PDF</p>"
    "<p class='hero-sub'>Sube un archivo o pega una URL. La app procesa en el servidor y te entrega un ZIP al finalizar.</p>"
    "</div>",
    unsafe_allow_html=True,
)

if modo_archivo:
    puede_iniciar = df is not None and col_url is not None and col_fecha is not None and col_folio is not None
    hint = "← Carga tu archivo CSV / Excel en el panel lateral para continuar."
else:
    url_ind = st.session_state.get("url_individual", "").strip()
    puede_iniciar = bool(url_ind)
    hint = "← Pega la URL de la boleta en el panel lateral para continuar."

kpi_slot = st.empty()
prog_slot = st.empty()
msg_slot = st.empty()
stat_slot = st.empty()

_rs = st.session_state.resultados
if _rs:
    _ok = sum(1 for r in _rs if r.ok)
    _err = sum(1 for r in _rs if not r.ok)
    _tot = len(_rs)
    _pct = round(_ok / _tot * 100, 1) if _tot else 0
    kpi_slot.markdown(_kpi_html(_tot, _tot, _ok, _err, 0.0), unsafe_allow_html=True)
    prog_slot.markdown(_bateria(_pct), unsafe_allow_html=True)
    msg_slot.markdown("<div class='msg-bubble msg-fade' style='color:#4ade80;'>🎉 Proceso completado</div>", unsafe_allow_html=True)
else:
    kpi_slot.markdown(_kpi_html(0, 0, 0, 0, 0.0), unsafe_allow_html=True)
    prog_slot.markdown(_bateria(0), unsafe_allow_html=True)
    if not puede_iniciar:
        msg_slot.markdown(f"<div class='msg-bubble msg-fade' style='color:#64748b;font-style:italic;'>{hint}</div>", unsafe_allow_html=True)
    else:
        mensaje_idle = obtener_mensaje_suave(0, 0, min_segundos=MSG_MIN_SEGUNDOS)
        msg_slot.markdown(f"<div class='msg-bubble msg-fade'>{mensaje_idle}</div>", unsafe_allow_html=True)

iniciar = st.button("🚀  Iniciar descarga", type="primary", disabled=not puede_iniciar)

if iniciar:
    st.session_state.msg_actual = ""
    st.session_state.msg_stage = ""
    st.session_state.msg_last_change = 0.0

    try:
        job_dir = crear_job_dir()
        salida = job_dir / "output"
        salida.mkdir(parents=True, exist_ok=True)

        if modo_archivo:
            tareas, omitidos, warns = construir_tareas(df, col_url, col_fecha, col_folio, salida)
            for w in warns[:10]:
                st.warning(w)
            if len(warns) > 10:
                st.warning(f"Se omitieron {len(warns) - 10} advertencias adicionales.")
            if omitidos:
                st.info(f"Se omitieron {omitidos} archivos porque ya existían en esta ejecución.")
        else:
            tarea = tarea_desde_url(st.session_state.url_individual.strip(), salida)
            if st.session_state.folio_manual.strip():
                nombre = limpiar_nombre(st.session_state.folio_manual.strip())
                tarea.folio = nombre
                tarea.ruta_pdf = salida / f"{nombre}.pdf"
            tareas = [tarea]

        if not tareas:
            st.warning("No hay tareas para procesar.")
        else:
            base_temp = job_dir / "workers"
            base_temp.mkdir(parents=True, exist_ok=True)
            resultados = ejecutar(tareas, workers_n, t_carga, t_descarga, reintentos, base_temp, log_sidebar, kpi_slot, prog_slot, msg_slot, stat_slot)
            st.session_state.resultados = resultados

            ok = [r for r in resultados if r.ok]
            err = [r for r in resultados if not r.ok]

            if ok:
                zip_bytes = hacer_zip(ok)
                st.success(f"Proceso finalizado. PDFs descargados: {len(ok)}")
                nombre_zip = construir_nombre_zip_desde_fechas(df, col_fecha) if modo_archivo else "boleta_individual.zip"
                st.download_button("📦 Descargar ZIP con PDFs", data=zip_bytes, file_name=nombre_zip, mime="application/zip")

            if err:
                with st.expander(f"Ver errores ({len(err)})"):
                    for r in err:
                        st.error(f"{r.tarea.folio}: {r.mensaje}")
    except WebDriverException as e:
        st.error(f"Error de Selenium/ChromeDriver: {e}")
    except Exception as e:
        st.error(f"Ocurrió un error durante la ejecución: {e}")
    finally:
        if 'job_dir' in locals() and job_dir.exists() and not KEEP_JOB_DIR:
            shutil.rmtree(job_dir, ignore_errors=True)

st.markdown("</div>", unsafe_allow_html=True)
