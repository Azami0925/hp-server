#!/usr/bin/env python3
"""
Cámara Telegram v1 — hp-server (Roger)
=======================================
Vigila 3 cámaras Tapo (1x C310 por cable + 2x C217 por Wi-Fi) y, cuando la
IA detecta una persona o un animal (sobre todo gatos), manda la foto por
Telegram. No graba vídeo.

Cómo funciona, paso a paso:
  1) Cada 5 minutos toma UNA foto de cada cámara por RTSP (ffmpeg).
  2) Compara la foto con la anterior: si casi nada cambió, no hace nada.
     Esto filtra el movimiento de hojas y ramas sin gastar CPU.
  3) Si hubo movimiento, pasa la foto por MobileNet-SSD (OpenCV DNN).
     Solo avisa por estas clases: persona, gato, perro, pájaro, caballo,
     oveja y vaca. Los árboles NO son una clase del modelo, así que nunca
     provocan un aviso: ese es el truco.
  4) Guarda la foto en la carpeta de buffer (máximo 30 fotos: al llegar
     a la 31 borra la más vieja) y la manda por Telegram con una leyenda
     (cámara, hora y qué se detectó).

Para no saturar el servidor (A9 de 2 núcleos): la detección solo corre
cuando hay movimiento, una sola red neuronal compartida entre las 3 cámaras
y un descanso (cooldown) entre avisos de la misma cámara.

Uso:
    cp config.json.example config.json   # y edita tus datos
    python3 camara-telegram.py
Pruebas sin cámaras:
    python3 camara-telegram.py --probar-foto /ruta/foto.jpg
    python3 camara-telegram.py --probar-telegram
    python3 camara-telegram.py --una-vez

Hecho para Roger. v1.1 — 27 nov 2026 (correcciones de Claude: bucle con una
sola pausa al final, sin token en el log, placeholder RTSP tratado como
vacío, sin sys.exit dentro del hilo de la cámara).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime

import numpy as np
import requests

# ---------- CONFIGURACIÓN POR DEFECTO (se puede cambiar en config.json) ----------
INTERVALO_SEGUNDOS = 300       # segundos entre fotos de cada cámara (5 minutos)
COOLDOWN_SEGUNDOS = 60        # segundos mínimos entre dos avisos de la misma cámara
CONFIANZA_MINIMA = 0.5        # confianza mínima del detector (0 a 1)
UMBRAL_MOVIMIENTO = 0.02      # fracción de píxeles que deben cambiar para hablar de movimiento
MAX_FOTOS = 30                # fotos máximas en el buffer (al llegar a 31 se borra la más vieja)
# -------------------------------------------------------------------------------

# Clases del modelo MobileNet-SSD (PASCAL VOC, 21 clases con fondo).
# ¡OJO! Los índices son los del modelo, no se pueden cambiar:
#   0 fondo, 1 avión, 2 bicicleta, 3 pájaro, 4 barco, 5 botella, 6 autobús,
#   7 coche, 8 gato, 9 silla, 10 vaca, 11 mesa, 12 perro, 13 caballo,
#   14 moto, 15 persona, 16 maceta, 17 oveja, 18 sofá, 19 tren, 20 TV.
# Solo avisamos por estas; "maceta" (16) queda fuera a propósito, y los
# árboles no existen como clase: por eso nunca alertan.
CLASES_OBJETIVO = {
    15: "persona",
    8: "gato",
    12: "perro",
    3: "pájaro",
    13: "caballo",
    17: "oveja",
    10: "vaca",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Estado en memoria
detener = threading.Event()
bloqueo_red = threading.Lock()   # OpenCV DNN no es seguro entre hilos: un solo forward a la vez
ultimo_aviso = {}                # clave de cámara -> timestamp del último aviso enviado


def log(mensaje):
    """Imprime con hora local (el journal de systemd lo guarda)."""
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] {mensaje}", flush=True)


# ---------- CONFIGURACIÓN ----------

def cargar_config(ruta=CONFIG_PATH):
    """Lee config.json y rellena los valores que falten con los de defecto.

    El token de Telegram y el chat id se pueden poner en variables de
    entorno (TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID) y mandan sobre el
    archivo. Lo mismo para el usuario y la contraseña RTSP
    (RTSP_USUARIO y RTSP_CONTRASENA). Así las claves no quedan en el
    archivo si no quieres.
    """
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)

    cfg.setdefault("intervalo_segundos", INTERVALO_SEGUNDOS)
    cfg.setdefault("cooldown_segundos", COOLDOWN_SEGUNDOS)
    cfg.setdefault("confianza_minima", CONFIANZA_MINIMA)
    cfg.setdefault("umbral_movimiento", UMBRAL_MOVIMIENTO)
    cfg.setdefault("max_fotos", MAX_FOTOS)
    cfg.setdefault("buffer", "fotos")
    cfg.setdefault("modelo_prototxt", "modelo/MobileNetSSD_deploy.prototxt")
    cfg.setdefault("modelo_caffemodel", "modelo/MobileNetSSD_deploy.caffemodel")
    cfg.setdefault("telegram", {})
    cfg["telegram"].setdefault("token", "")
    cfg["telegram"].setdefault("chat_id", "")

    # Variables de entorno mandan sobre el archivo
    cfg["telegram"]["token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg["telegram"]["token"]
    cfg["telegram"]["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"]["chat_id"]

    # Si el usuario/contraseña RTSP quedaron como placeholder ("TU_..."),
    # se tratan como vacíos para que manden las variables de entorno
    # RTSP_USUARIO / RTSP_CONTRASENA (si no, el texto "TU_..." se usaría
    # como usuario real y nunca se leerían las variables).
    for cam in cfg.get("camaras", []):
        for clave in ("usuario", "contrasena"):
            if str(cam.get(clave, "")).startswith("TU_"):
                cam[clave] = ""

    return cfg


def construir_url_rtsp(usuario, contrasena, ip, stream="stream1"):
    """Arma la URL RTSP de una Tapo.

    Formato Tapo: rtsp://usuario:contraseña@IP:554/stream1
    (stream1 = imagen principal, stream2 = imagen secundaria, más ligera).
    El usuario y la contraseña son los de la *cuenta de cámara* que se crea
    en la app Tapo, no los de la cuenta TP-Link. La contraseña se codifica
    porque puede llevar caracteres como @ : / # que romperían la URL.
    """
    usuario = urllib.parse.quote(str(usuario), safe="")
    contrasena = urllib.parse.quote(str(contrasena), safe="")
    return f"rtsp://{usuario}:{contrasena}@{ip}:554/{stream}"


# ---------- FOTOS Y BUFFER ----------

def nombre_archivo_foto(clave_camara, etiquetas, cuando=None):
    """Nombre de archivo para una foto guardada: fecha_hora_camara_etiquetas.jpg"""
    cuando = cuando or datetime.now()
    # Con milisegundos: dos fotos en el mismo segundo no se pisan
    base = cuando.strftime("%Y-%m-%d_%H-%M-%S") + f"-{cuando.microsecond // 1000:03d}"
    etiquetas = "-".join(etiquetas) if etiquetas else "deteccion"
    # Solo letras, números, guiones y guion bajo (nada de espacios ni acentos)
    seguro = "".join(c if (c.isascii() and (c.isalnum() or c in "-_")) else "_" for c in etiquetas)
    return f"{base}_{clave_camara}_{seguro}.jpg"


def podar_buffer(carpeta, max_fotos):
    """Mantiene como mucho max_fotos fotos en la carpeta (FIFO).

    Borra las más viejas por fecha de modificación hasta quedar en el
    límite. Devuelve (fotos_que_quedan, fotos_borradas).
    """
    try:
        fotos = [os.path.join(carpeta, f) for f in os.listdir(carpeta)
                 if f.lower().endswith(".jpg")]
    except FileNotFoundError:
        return 0, 0
    fotos.sort(key=lambda p: os.path.getmtime(p))   # más vieja primero
    borradas = 0
    while len(fotos) > max_fotos:
        vieja = fotos.pop(0)
        try:
            os.remove(vieja)
            borradas += 1
        except OSError as e:
            log(f"  No pude borrar {vieja}: {e}")
            break
    return len(fotos), borradas


def guardar_foto(frame_bytes_jpg, carpeta, clave_camara, etiquetas):
    """Guarda una foto JPEG en el buffer y poda lo que sobre. Devuelve la ruta."""
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, nombre_archivo_foto(clave_camara, etiquetas))
    # Si el nombre ya existe (dos fotos en el mismo milisegundo), añade _2, _3...
    base, ext = os.path.splitext(ruta)
    n = 2
    while os.path.exists(ruta):
        ruta = f"{base}_{n}{ext}"
        n += 1
    with open(ruta, "wb") as f:
        f.write(frame_bytes_jpg)
    quedan, borradas = podar_buffer(carpeta, MAX_FOTOS_BUFFER)
    if borradas:
        log(f"  Buffer: borrada(s) {borradas} foto(s) vieja(s), quedan {quedan}")
    return ruta


# El límite de fotos se lee de la config en main(); este global lo usa
# guardar_foto y se fija al arrancar (por defecto MAX_FOTOS).
MAX_FOTOS_BUFFER = MAX_FOTOS


# ---------- MOVIMIENTO (numpy, sin cv2: se puede probar en cualquier lado) ----------

def hay_movimiento(frame_anterior, frame_actual, umbral_pixeles=25,
                   fraccion_minima=UMBRAL_MOVIMIENTO):
    """¿Cambió algo de verdad entre dos fotos?

    Achica las fotos (1 de cada 4 píxeles), las pasa a gris y cuenta
    cuántos píxeles cambiaron más de `umbral_pixeles`. Si la fracción
    que cambió supera `fraccion_minima`, hay movimiento. Unas hojas
    moviéndose cambian pocos píxeles; una persona cruzando, muchos.
    """
    if frame_anterior is None or frame_actual is None:
        return True   # primera foto: no hay con qué comparar, se revisa
    if frame_anterior.shape != frame_actual.shape:
        return True
    a = frame_anterior[::4, ::4].astype(np.int16)
    b = frame_actual[::4, ::4].astype(np.int16)
    gris_a = a.mean(axis=2)
    gris_b = b.mean(axis=2)
    diferencia = np.abs(gris_a - gris_b)
    cambiados = (diferencia > umbral_pixeles).sum()
    fraccion = cambiados / diferencia.size
    return bool(fraccion >= fraccion_minima)


# ---------- DETECTOR (OpenCV DNN + MobileNet-SSD) ----------

def cargar_modelo(ruta_prototxt, ruta_caffemodel):
    """Carga la red MobileNet-SSD. cv2 se importa aquí (no al inicio)
    para que las funciones puras del archivo se puedan probar sin OpenCV."""
    try:
        import cv2
    except ImportError:
        log("ERROR: falta OpenCV. Instálalo con: sudo apt install -y python3-opencv")
        sys.exit(2)
    if not os.path.isfile(ruta_prototxt) or not os.path.isfile(ruta_caffemodel):
        log("ERROR: no encontré los archivos del modelo:")
        log(f"  {ruta_prototxt}")
        log(f"  {ruta_caffemodel}")
        log("Descárgalos como dice el README (carpeta modelo/) y vuelve a arrancar.")
        sys.exit(2)
    net = cv2.dnn.readNetFromCaffe(ruta_prototxt, ruta_caffemodel)
    log("Modelo MobileNet-SSD cargado.")
    return net


def filtrar_detecciones(detecciones, confianza_minima=CONFIANZA_MINIMA):
    """Filtra detecciones crudas del modelo y deja solo lo que nos importa.

    Entrada: lista de dicts {clase_idx, confianza}.
    Salida: lista de dicts {clase_idx, etiqueta, confianza} solo con las
    clases objetivo (persona, gato, perro, pájaro, caballo, oveja, vaca)
    y confianza >= confianza_minima, ordenadas de mayor a menor confianza.
    Todo lo demás (coches, sillas, macetas, TVs...) se tira, y los
    árboles ni siquiera existen como clase: nunca alertan.
    """
    filtradas = []
    for d in detecciones:
        idx = d["clase_idx"]
        conf = d["confianza"]
        if idx in CLASES_OBJETIVO and conf >= confianza_minima:
            filtradas.append({
                "clase_idx": idx,
                "etiqueta": CLASES_OBJETIVO[idx],
                "confianza": round(float(conf), 2),
            })
    filtradas.sort(key=lambda d: d["confianza"], reverse=True)
    return filtradas


def detectar(net, frame, confianza_minima=CONFIANZA_MINIMA):
    """Pasa un frame por la red y devuelve las detecciones filtradas."""
    import cv2
    blob = cv2.dnn.blobFromImage(frame, 0.007843, (300, 300), 127.5)
    with bloqueo_red:   # un solo forward a la vez entre las 3 cámaras
        net.setInput(blob)
        salida = net.forward()
    crudas = []
    for i in range(salida.shape[2]):
        deteccion = salida[0, 0, i]
        crudas.append({
            "clase_idx": int(deteccion[1]),
            "confianza": float(deteccion[2]),
        })
    return filtrar_detecciones(crudas, confianza_minima)


def texto_detecciones(filtradas):
    """'persona (92%), gato (78%)' para la leyenda de Telegram."""
    return ", ".join(f"{d['etiqueta']} ({int(d['confianza'] * 100)}%)"
                     for d in filtradas)


# ---------- TELEGRAM ----------

def construir_caption(nombre_camara, filtradas, cuando=None):
    """Leyenda de la foto: cámara, hora local y qué se detectó."""
    cuando = cuando or datetime.now()
    hora = cuando.strftime("%d %b %Y, %H:%M")
    return (f"📷 {nombre_camara}\n"
            f"🕐 {hora}\n"
            f"Detectado: {texto_detecciones(filtradas)}")


def enviar_foto_telegram(token, chat_id, ruta_foto, caption, timeout=30):
    """Manda una foto por Telegram (sendPhoto). Devuelve True si llegó."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(ruta_foto, "rb") as foto:
            r = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": (os.path.basename(ruta_foto), foto, "image/jpeg")},
                timeout=timeout,
            )
    except requests.RequestException as e:
        # Solo el tipo de error: el texto completo puede incluir la URL
        # con el token del bot, y eso quedaría en el log (journal).
        log(f"  Telegram: no se pudo enviar ({type(e).__name__})")
        return False
    if r.status_code != 200:
        log(f"  Telegram: error {r.status_code}: {r.text[:200]}")
        return False
    return True


def enviar_mensaje_telegram(token, chat_id, texto, timeout=15):
    """Manda un mensaje de texto (para --probar-telegram)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": texto},
                          timeout=timeout)
    except requests.RequestException as e:
        # Solo el tipo de error: el texto completo puede incluir la URL
        # con el token del bot, y eso quedaría en el log (journal).
        log(f"  Telegram: no se pudo enviar ({type(e).__name__})")
        return False
    return r.status_code == 200


# ---------- TOMAR UNA FOTO DE LA CÁMARA ----------

def tomar_foto_ffmpeg(url_rtsp, timeout=20):
    """Toma UNA foto del stream RTSP con ffmpeg y la devuelve como array
    numpy (BGR, como OpenCV) o None si falló.

    Se abre una conexión nueva por foto: gasta un poco más que un stream
    continuo, pero no deja procesos colgados ni streams abiertos si una
    cámara se cae, y la CPU solo trabaja unos segundos por revisión.
    """
    import cv2
    comando = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", url_rtsp,
        "-frames:v", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    try:
        proceso = subprocess.run(comando, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("  ffmpeg: se acabó el tiempo esperando la foto")
        return None
    except FileNotFoundError:
        log("ERROR: falta ffmpeg. Instálalo con: sudo apt install -y ffmpeg")
        return None
    if not proceso.stdout:
        return None
    datos = np.frombuffer(proceso.stdout, dtype=np.uint8)
    frame = cv2.imdecode(datos, cv2.IMREAD_COLOR)
    return frame


def frame_a_jpg(frame):
    """Codifica un frame numpy a bytes JPEG (calidad 85)."""
    import cv2
    ok, datos = cv2.imencode(".jpg", frame,
                             [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    return datos.tobytes()


# ---------- VIGILAR UNA CÁMARA (un hilo por cámara) ----------

def vigilar_camara(camara, cfg, net):
    """Bucle de una cámara: foto -> ¿movimiento? -> ¿persona/animal? -> aviso.

    Una sola pausa al final del ciclo (detener.wait(intervalo)): así, si hay
    movimiento sin detección o el aviso está en cooldown, no se toman fotos
    por RTSP una tras otra (con viento, eso saturaría la CPU).
    """
    nombre = camara["nombre"]
    clave = camara.get("clave", nombre)
    url = construir_url_rtsp(
        camara.get("usuario") or os.environ.get("RTSP_USUARIO", ""),
        camara.get("contrasena") or os.environ.get("RTSP_CONTRASENA", ""),
        camara["ip"],
        camara.get("stream", "stream1"),
    )
    intervalo = cfg["intervalo_segundos"]
    cooldown = cfg["cooldown_segundos"]
    confianza = cfg["confianza_minima"]
    umbral_mov = cfg["umbral_movimiento"]
    carpeta_buffer = cfg["buffer_abs"]
    frame_anterior = None
    fallos_seguidos = 0

    log(f"[{nombre}] vigilando {camara['ip']} (revisión cada {intervalo} s)")
    while not detener.is_set():
        frame = tomar_foto_ffmpeg(url)
        if frame is None:
            fallos_seguidos += 1
            if fallos_seguidos == 1 or fallos_seguidos % 12 == 0:
                log(f"[{nombre}] no pude tomar foto (fallo {fallos_seguidos})")
            detener.wait(10)
            continue
        fallos_seguidos = 0

        if hay_movimiento(frame_anterior, frame,
                          fraccion_minima=umbral_mov):
            filtradas = detectar(net, frame, confianza)
            ahora = time.time()
            if filtradas and ahora - ultimo_aviso.get(clave, 0) >= cooldown:
                ultimo_aviso[clave] = ahora
                etiquetas = [d["etiqueta"] for d in filtradas]
                jpg = frame_a_jpg(frame)
                if jpg is not None:
                    ruta = guardar_foto(jpg, carpeta_buffer, clave, etiquetas)
                    caption = construir_caption(nombre, filtradas)
                    log(f"[{nombre}] {texto_detecciones(filtradas)} -> {os.path.basename(ruta)}")
                    if enviar_foto_telegram(cfg["telegram"]["token"],
                                            cfg["telegram"]["chat_id"], ruta, caption):
                        log(f"[{nombre}] foto enviada por Telegram")
                    else:
                        log(f"[{nombre}] la foto quedó en {ruta} (no se pudo enviar)")
        frame_anterior = frame
        detener.wait(intervalo)


# ---------- ARRANQUE ----------

def revisar_entorno(cfg):
    """Comprueba lo necesario antes de arrancar y sale con un mensaje claro."""
    if not shutil.which("ffmpeg"):
        log("ERROR: falta ffmpeg. Instálalo con: sudo apt install -y ffmpeg")
        sys.exit(2)
    token = cfg["telegram"]["token"]
    chat_id = cfg["telegram"]["chat_id"]
    if not token or token.startswith("TU_") or not chat_id or str(chat_id).startswith("TU_"):
        log("ERROR: falta el token de Telegram o el chat id.")
        log("Copia config.json.example a config.json y pon tus datos,")
        log("o define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID. Ver README.")
        sys.exit(2)
    if not cfg.get("camaras"):
        log("ERROR: no hay cámaras en config.json (lista 'camaras' vacía).")
        sys.exit(2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Modos de prueba (no necesitan cámaras ni modelo, salvo --probar-foto)
    if "--probar-telegram" in argv:
        if not os.path.isfile(CONFIG_PATH):
            log("ERROR: primero crea config.json (ver README).")
            return 2
        cfg = cargar_config()
        ok = enviar_mensaje_telegram(
            cfg["telegram"]["token"], cfg["telegram"]["chat_id"],
            "📷 Prueba de Cámara Telegram: el bot funciona.")
        log("Telegram OK: mensaje enviado." if ok else "Telegram FALLÓ: revisa token y chat id.")
        return 0 if ok else 1

    if "--probar-foto" in argv:
        idx = argv.index("--probar-foto")
        ruta_foto = argv[idx + 1] if idx + 1 < len(argv) else None
        if not ruta_foto or not os.path.isfile(ruta_foto):
            log("Uso: python3 camara-telegram.py --probar-foto /ruta/foto.jpg")
            return 2
        if not os.path.isfile(CONFIG_PATH):
            log("ERROR: primero crea config.json (ver README).")
            return 2
        cfg = cargar_config()
        import cv2
        frame = cv2.imread(ruta_foto)
        if frame is None:
            log(f"ERROR: no pude leer {ruta_foto}")
            return 2
        net = cargar_modelo(
            os.path.join(BASE_DIR, cfg["modelo_prototxt"]),
            os.path.join(BASE_DIR, cfg["modelo_caffemodel"]))
        filtradas = detectar(net, frame, cfg["confianza_minima"])
        if filtradas:
            log(f"Detectado: {texto_detecciones(filtradas)}")
        else:
            log("Sin detecciones (ni persona ni animal).")
        return 0

    if not os.path.isfile(CONFIG_PATH):
        log("ERROR: no existe config.json.")
        log("Copia config.json.example a config.json y pon tus datos. Ver README.")
        return 2
    cfg = cargar_config()
    revisar_entorno(cfg)

    global MAX_FOTOS_BUFFER
    MAX_FOTOS_BUFFER = int(cfg["max_fotos"])
    cfg["buffer_abs"] = cfg["buffer"] if os.path.isabs(cfg["buffer"]) \
        else os.path.join(BASE_DIR, cfg["buffer"])
    os.makedirs(cfg["buffer_abs"], exist_ok=True)
    quedan, _ = podar_buffer(cfg["buffer_abs"], MAX_FOTOS_BUFFER)
    log(f"Buffer de fotos: {quedan}/{MAX_FOTOS_BUFFER} en {cfg['buffer_abs']}")

    net = cargar_modelo(
        os.path.join(BASE_DIR, cfg["modelo_prototxt"]),
        os.path.join(BASE_DIR, cfg["modelo_caffemodel"]))

    def al_recibir_senal(signum, _frame):
        log("Apagando...")
        detener.set()

    signal.signal(signal.SIGINT, al_recibir_senal)
    signal.signal(signal.SIGTERM, al_recibir_senal)

    hilos = []
    for camara in cfg["camaras"]:
        hilo = threading.Thread(target=vigilar_camara, args=(camara, cfg, net),
                                daemon=True)
        hilo.start()
        hilos.append(hilo)

    log(f"Cámara Telegram corriendo: {len(hilos)} cámara(s). Ctrl+C para parar.")
    una_vez = "--una-vez" in argv
    try:
        while not detener.is_set():
            if una_vez:
                detener.wait(30)   # da tiempo a una revisión por cámara
                detener.set()
            detener.wait(1)
    except KeyboardInterrupt:
        detener.set()
    log("Adiós.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
