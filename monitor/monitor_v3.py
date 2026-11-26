#!/usr/bin/env python3
"""
Monitor de Red v3 para Termux - Nubia Neo 3
Basado en monitor.py v2 + ideas de PingTools:
  1) Escaneo de puertos por dispositivo (qué servicios tiene abiertos)
  2) Nombres de dispositivos por UPnP/SSDP y mDNS + fabricante por MAC (OUI)
  3) Latencia a servicios (Cloudflare, Google, GitHub, Docker Hub, NPM,
     Azure, AWS, ChatGPT, Claude, YouTube, Discord)
Sin root. Hecho para Roger.

Uso en Termux:
    pip install flask          # zeroconf es opcional: pip install zeroconf
    python monitor_v3.py
Luego abre desde otro celular o PC en la misma WiFi:
    http://IP_DEL_CELULAR:5000
Para saber la IP:  termux-wifi-connectioninfo  o  ifconfig wlan0
"""

import hmac
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template, render_template_string, request, send_from_directory

# ---------- CONFIGURACIÓN - edítala si tu red es diferente ----------
PUERTO = 5000
INTERVALO_PING = 10          # segundos entre pings
INTERVALO_ESCANEO = 300      # segundos entre escaneos de dispositivos (5 min)
INTERVALO_VELOCIDAD = 1800   # segundos entre tests de velocidad (30 min)
INTERVALO_LATENCIA = 120     # segundos entre mediciones de latencia a servicios
# IP del router/gateway. Si cambias de router o de red, edita esta constante
# (y SUBNET): el monitor no la vuelve a detectar solo. Telmex Infinitum:
# gateway es .254, no .1. Confirmado 2026-11-09. Si ROUTER_IP y SUBNET se
# dejan vacías, autodetectar_red() intenta detectarlas al arrancar.
ROUTER_IP = "192.168.1.254"
SUBNET = "192.168.1.0/24"
# Token para abrir el panel (se lee del entorno). Se guarda en
# /etc/monitor-red.env (permisos 600) y el servicio lo carga con
# EnvironmentFile. Sin token configurado, el panel no pide nada.
TOKEN = os.environ.get("MONITOR_TOKEN", "")
# -------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)  # para importar oui.py junto al script
from oui import OUI_FABRICANTES

DATA_DIR = os.path.join(BASE_DIR, "data_v3")
os.makedirs(DATA_DIR, exist_ok=True)
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
OUTAGES_FILE = os.path.join(DATA_DIR, "outages.json")

app = Flask(__name__)

# Estado en memoria
state = {
    "router_ip": ROUTER_IP or "192.168.1.1",
    "subnet": SUBNET or "192.168.1.0/24",
    "router_ping_ms": None,
    "google_ping_ms": None,
    "cloudflare_ping_ms": None,
    "internet_ok": None,
    "ultimo_ping": None,
    "ultimo_escaneo": None,
    "ultimo_speedtest": None,
    "download_mbps": None,
    "upload_mbps": None,
    "ping_servidor_speedtest": None,
    "servidor_speedtest": None,
    "caidas_hoy": 0,
    "total_pings": 0,
    "pings_ok": 0,
    "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "latencia_servicios": [],
    "ultima_latencia": None,
    "cpu_pct": None,
    "cpu_temp_c": None,
    "ram_pct": None,
    "ram_usada_gb": None,
    "ram_total_gb": None,
    "ultimo_sistema": None,
}

# ip -> {ip, mac, hostname, nombre, fabricante, puertos, first_seen, last_seen, veces_visto}
history = []   # últimos 60 pings: {hora, router, google, ok}
outages = []   # {inicio, fin, duracion_seg}


def cargar_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def guardar_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"No se pudo guardar {path}: {e}")


devices = cargar_json(DEVICES_FILE, {})
history = cargar_json(HISTORY_FILE, [])
outages = cargar_json(OUTAGES_FILE, [])


def ejecutar(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except Exception as e:
        return f"ERROR: {e}"


def autodetectar_red():
    """Detecta gateway y subred desde 'ip route' y termux-wifi-connectioninfo.
    CORREGIDO: nunca usar la IP del propio celular como router.
    Si no se encuentra gateway, se usa .1 de la misma subred."""
    out = ejecutar(["ip", "route"])
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        state["router_ip"] = m.group(1)
        base = ".".join(m.group(1).split(".")[:3])
        state["subnet"] = f"{base}.0/24"
        return
    # Respaldo: termux-wifi-connectioninfo — solo 'gateway', nunca 'ip'
    out2 = ejecutar(["termux-wifi-connectioninfo"])
    try:
        info = json.loads(out2)
        gw = info.get("gateway")
        mi_ip = info.get("ip")
        if gw:
            state["router_ip"] = gw
            base = ".".join(gw.split(".")[:3])
            state["subnet"] = f"{base}.0/24"
            return
        if mi_ip:
            base = ".".join(mi_ip.split(".")[:3])
            state["subnet"] = f"{base}.0/24"
            state["router_ip"] = f"{base}.1"
    except Exception:
        pass


if not ROUTER_IP or not SUBNET:
    autodetectar_red()
if ROUTER_IP:
    state["router_ip"] = ROUTER_IP
if SUBNET:
    state["subnet"] = SUBNET


def ping(host):
    """Devuelve latencia en ms o None si no responde."""
    out = ejecutar(["ping", "-c", "1", "-W", "2", host], timeout=5)
    m = re.search(r"time=([\d.]+)\s*ms", out)
    return float(m.group(1)) if m else None


# ---------- v3: PUERTOS, NOMBRES Y FABRICANTE ----------

PUERTOS_COMUNES = {
    7: "Echo", 21: "FTP", 22: "SSH", 23: "Telnet", 80: "HTTP",
    135: "RPC", 139: "NetBIOS", 443: "HTTPS", 445: "SMB",
    554: "RTSP", 8000: "HTTP-alt", 8080: "HTTP-alt", 5000: "Monitor",
    8443: "HTTPS-alt", 1900: "UPnP", 5353: "mDNS",
}

# OUI_FABRICANTES vive en oui.py (se separó de este archivo).



def fabricante_por_mac(mac):
    """Fabricante por los primeros 3 bytes de la MAC (OUI)."""
    if not mac or mac == "?":
        return "?"
    oui = mac.upper().replace("-", ":")[:8]
    return OUI_FABRICANTES.get(oui, "Desconocido")


def escanear_puertos(ip, timeout=0.4):
    """TCP connect a los puertos comunes. Sin root. Devuelve lista de puertos abiertos."""
    abiertos = []

    def _probar(p):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((ip, p)) == 0:
                    abiertos.append(p)
        except Exception:
            pass

    try:
        with ThreadPoolExecutor(max_workers=len(PUERTOS_COMUNES)) as ex:
            list(ex.map(_probar, PUERTOS_COMUNES))
    except Exception:
        pass
    return sorted(abiertos)


def _nombre_desde_descripcion(url, ip_esperada="", timeout=1.5):
    """Lee el XML de descripción UPnP y saca friendlyName / fabricante + modelo.
    Solo acepta URLs http cuyo host sea la IP del dispositivo que anunció el SSDP."""
    try:
        partes = urllib.parse.urlparse(url)
        if partes.scheme != "http":
            return ""
        if ip_esperada and (partes.hostname or "") != ip_esperada:
            return ""
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            xml = r.read(8192).decode("utf-8", "ignore")
        m = re.search(r"<friendlyName>([^<]+)</friendlyName>", xml)
        if m:
            return m.group(1).strip()
        fab = re.search(r"<manufacturer>([^<]+)</manufacturer>", xml)
        mod = re.search(r"<modelName>([^<]+)</modelName>", xml)
        partes = [x for x in [(fab.group(1).strip() if fab else ""),
                              (mod.group(1).strip() if mod else "")] if x]
        return " ".join(partes)
    except Exception:
        return ""


def descubrir_ssdp(timeout=2.0):
    """Descubre dispositivos por UPnP/SSDP (multicast UDP, solo stdlib).
    Devuelve {ip: nombre}."""
    res = {}
    msg = ("\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ns=01"',
        "MX: 2",
        "ST: upnp:rootdevice",
        "", ""])).encode()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(msg, ("239.255.255.250", 1900))
            fin = time.time() + timeout
            while time.time() < fin:
                try:
                    data, (ip, _) = s.recvfrom(4096)
                except socket.timeout:
                    break
                except Exception:
                    break
                txt = data.decode("utf-8", "ignore")
                loc = re.search(r"LOCATION:\s*(http://[\d\.]+:\d+(/\S*)?)", txt, re.I)
                srv = re.search(r"SERVER:\s*(.+)", txt, re.I)
                nombre = ""
                if loc:
                    nombre = _nombre_desde_descripcion(loc.group(1), ip)
                if not nombre and srv:
                    nombre = srv.group(1).strip().split(",")[0]
                if ip not in res and nombre:
                    res[ip] = nombre
    except Exception as e:
        print(f"  SSDP falló: {e}")
    return res


def descubrir_mdns(timeout=2.5):
    """Descubre nombres por mDNS/Bonjour. Necesita 'pip install zeroconf'.
    Si no está instalado, devuelve {} sin error."""
    res = {}
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:
        return res

    class Oyente(ServiceListener):
        def add_service(self, zc, tipo, nombre):
            try:
                info = zc.get_service_info(tipo, nombre)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    if ip not in res:
                        res[ip] = nombre.split(".")[0]
            except Exception:
                pass

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    try:
        zc = Zeroconf()
        tipos = ["_http._tcp.local.", "_googlecast._tcp.local.",
                 "_spotify-connect._tcp.local.", "_airplay._tcp.local.",
                 "_hap._tcp.local."]
        for t in tipos:
            ServiceBrowser(zc, t, Oyente())
        time.sleep(timeout)
        zc.close()
    except Exception as e:
        print(f"  mDNS falló: {e}")
    return res


# ---------- v3: LATENCIA A SERVICIOS ----------

SERVICIOS_LATENCIA = [
    ("Cloudflare (DNS)", "ICMP", "1.1.1.1"),
    ("Google (DNS)", "ICMP", "8.8.8.8"),
    ("GitHub", "HTTPS", "github.com"),
    ("Docker Hub", "HTTPS", "docker.com"),
    ("NPM Registry", "HTTPS", "npmjs.org"),
    ("Microsoft Azure", "HTTPS", "azure.microsoft.com"),
    ("AWS", "HTTPS", "aws.amazon.com"),
    ("ChatGPT", "HTTPS", "chatgpt.com"),
    ("Claude", "HTTPS", "claude.ai"),
    ("YouTube", "HTTPS", "youtube.com"),
    ("Discord", "HTTPS", "discord.com"),
]


def medir_https(host, timeout=10):
    """Mide ms de una petición HTTPS. HEAD primero; si el servidor no lo
    acepta (error 4xx/5xx o conexión cerrada), reintenta con GET leyendo
    solo 1 KB. None si falla."""
    for metodo in ("HEAD", "GET"):
        t0 = time.time()
        conn = None
        try:
            conn = http.client.HTTPSConnection(host, timeout=timeout)
            conn.request(metodo, "/", headers={"User-Agent": "Monitor-Red-v3"})
            r = conn.getresponse()
            if r.status >= 400 and metodo == "HEAD":
                continue
            try:
                r.read() if metodo == "HEAD" else r.read(1024)
            except Exception:
                pass
            return round((time.time() - t0) * 1000)
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()
    return None


def _clase_latencia(tipo, ms):
    """Color de la latencia: ICMP se juzga como ping; HTTPS como HTTPS
    (el handshake TLS ya suma 150-300 ms aunque todo esté bien)."""
    if ms is None:
        return "bad"
    if tipo == "ICMP":
        return "ok" if ms < 50 else ("warn" if ms < 150 else "bad")
    return "ok" if ms < 250 else ("warn" if ms < 500 else "bad")


lock_latencia = threading.Lock()


def medir_latencia_servicios():
    """Mide latencia a los 11 servicios de la lista. Guarda en state."""
    if not lock_latencia.acquire(blocking=False):
        print("  Ya hay una medición de latencia en curso, se omite esta")
        return state["latencia_servicios"]
    try:
        out = []
        for nombre, tipo, destino in SERVICIOS_LATENCIA:
            ms = ping(destino) if tipo == "ICMP" else medir_https(destino)
            if ms is not None:
                ms = round(ms)
            out.append({"nombre": nombre, "tipo": tipo, "destino": destino, "ms": ms,
                        "clase": _clase_latencia(tipo, ms)})
        state["latencia_servicios"] = out
        state["ultima_latencia"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return out
    finally:
        lock_latencia.release()


# ---------- DISPOSITIVOS ----------

def _registrar(ip, mac="", vistos=None):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mac = (mac or "").lower() or "?"
    if vistos is not None:
        vistos.add(ip)
    if ip not in devices:
        devices[ip] = {
            "ip": ip, "mac": mac, "hostname": "", "nombre": "",
            "fabricante": fabricante_por_mac(mac), "puertos": [],
            "first_seen": ahora, "last_seen": ahora, "veces_visto": 1,
            "nuevo": True,
        }
        print(f"  Nuevo dispositivo: {ip} ({mac})")
    else:
        if mac and mac != "?":
            devices[ip]["mac"] = mac
            devices[ip]["fabricante"] = fabricante_por_mac(mac)
        devices[ip]["last_seen"] = ahora
        devices[ip]["veces_visto"] = devices[ip].get("veces_visto", 0) + 1
        devices[ip]["nuevo"] = False
        devices[ip].setdefault("nombre", "")
        devices[ip].setdefault("puertos", [])


def _escanear_dispositivos_interno():
    """Escanea la red sin root: nmap -sn (sin -PR, que pide root) + ip neigh.
    v3 suma: nombres por SSDP/mDNS, fabricante por MAC y puertos abiertos por equipo."""
    subnet = state["subnet"]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Escaneando {subnet} ...")
    vistos = set()

    # 1) nmap -sn sin -PR, y leer su salida directamente
    out_nmap = ejecutar(["nmap", "-sn", subnet], timeout=90)
    ip_actual = None
    for line in out_nmap.splitlines():
        m_ip = re.search(r"Nmap scan report for (?:.*\()?(\d+\.\d+\.\d+\.\d+)\)?", line)
        if m_ip:
            ip_actual = m_ip.group(1)
            _registrar(ip_actual, vistos=vistos)
            continue
        m_mac = re.search(r"MAC Address:\s+([0-9A-Fa-f:]{17})", line)
        if m_mac and ip_actual:
            devices[ip_actual]["mac"] = m_mac.group(1).lower()
            devices[ip_actual]["fabricante"] = fabricante_por_mac(m_mac.group(1))

    # 2) ip neigh para completar MACs
    out = ejecutar(["ip", "neigh"])
    for line in out.splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+).*lladdr\s+([0-9a-f:]{17})", line, re.I)
        if m:
            _registrar(m.group(1), m.group(2), vistos=vistos)

    # 3) v3: nombres por UPnP/SSDP y mDNS
    try:
        nombres = descubrir_ssdp()
        nombres.update(descubrir_mdns())
        for ip, nombre in nombres.items():
            if nombre:
                _registrar(ip, vistos=vistos)
                devices[ip]["nombre"] = nombre
    except Exception as e:
        print(f"  Descubrir nombres falló: {e}")

    # 4) v3: fabricante por MAC + puertos abiertos (solo equipos vistos en este escaneo)
    for ip in vistos:
        if ip in devices:
            devices[ip]["fabricante"] = fabricante_por_mac(devices[ip].get("mac", "?"))

    def _puertos_una(ip):
        return ip, escanear_puertos(ip)

    try:
        with ThreadPoolExecutor(max_workers=20) as ex:
            for ip, puertos in ex.map(_puertos_una, sorted(vistos)):
                if ip in devices:
                    devices[ip]["puertos"] = puertos
    except Exception as e:
        print(f"  Escaneo de puertos falló: {e}")

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for ip, d in devices.items():
        d["activo"] = ip in vistos
    state["ultimo_escaneo"] = ahora
    guardar_json(DEVICES_FILE, devices)
    print(f"  Dispositivos activos: {sum(1 for d in devices.values() if d.get('activo'))}")
    return len(devices)


lock_escaneo = threading.Lock()


def escanear_dispositivos():
    """Envoltorio con candado: si ya hay un escaneo corriendo, no arranca otro."""
    if not lock_escaneo.acquire(blocking=False):
        print("Ya hay un escaneo en curso, se omite este")
        return 0
    try:
        return _escanear_dispositivos_interno()
    finally:
        lock_escaneo.release()


lock_velocidad = threading.Lock()


def medir_velocidad():
    """Mide velocidad con speedtest-cli. Devuelve dict o None."""
    if not lock_velocidad.acquire(blocking=False):
        print("  Ya hay un speedtest en curso, se omite este")
        return False
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Midiendo velocidad...")
        out = ejecutar(["speedtest-cli", "--json"], timeout=120)
        try:
            out = out[out.index("{"):]
            d = json.loads(out)
            state["download_mbps"] = round(d["download"] / 1_000_000, 1)
            state["upload_mbps"] = round(d["upload"] / 1_000_000, 1)
            state["ping_servidor_speedtest"] = round(d["ping"], 1)
            state["servidor_speedtest"] = f"{d['server']['name']}, {d['server']['country']}"
            state["ultimo_speedtest"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"  Bajada: {state['download_mbps']} Mbps | Subida: {state['upload_mbps']} Mbps")
            return True
        except Exception as e:
            print(f"  Speedtest falló: {e}")
            return False
    finally:
        lock_velocidad.release()


# ---------- HILOS EN SEGUNDO PLANO ----------

caida_actual = None  # hora de inicio de la caída actual


def hilo_ping():
    global caida_actual
    while True:
        ahora = datetime.now().strftime("%H:%M:%S")
        r = ping(state["router_ip"])
        g = ping("8.8.8.8")
        c = ping("1.1.1.1")
        ok = g is not None or c is not None

        state["router_ping_ms"] = r
        state["google_ping_ms"] = g
        state["cloudflare_ping_ms"] = c
        state["internet_ok"] = ok
        state["ultimo_ping"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["total_pings"] += 1
        if ok:
            state["pings_ok"] += 1

        history.append({"hora": ahora, "router": r, "google": g, "ok": ok})
        if len(history) > 60:
            history.pop(0)
        guardar_json(HISTORY_FILE, history)

        if not ok and caida_actual is None:
            caida_actual = datetime.now()
            print(f"[{ahora}] ⚠️ SIN INTERNET")
        elif ok and caida_actual is not None:
            fin = datetime.now()
            dur = int((fin - caida_actual).total_seconds())
            outages.append({
                "inicio": caida_actual.strftime("%Y-%m-%d %H:%M:%S"),
                "fin": fin.strftime("%Y-%m-%d %H:%M:%S"),
                "duracion_seg": dur,
            })
            if len(outages) > 50:
                outages.pop(0)
            guardar_json(OUTAGES_FILE, outages)
            print(f"[{ahora}] ✅ Internet de vuelta tras {dur}s")
            caida_actual = None

        # Recalcular en cada ciclo (no solo al volver internet): el contador
        # debe reiniciarse al cambiar de día aunque no haya caídas nuevas.
        state["caidas_hoy"] = len([o for o in outages if o["inicio"].startswith(datetime.now().strftime("%Y-%m-%d"))])

        time.sleep(INTERVALO_PING)


def hilo_escaneo():
    while True:
        try:
            escanear_dispositivos()
        except Exception as e:
            print(f"Error escaneando: {e}")
        time.sleep(INTERVALO_ESCANEO)


def hilo_velocidad():
    time.sleep(30)  # esperar a que arranque todo
    while True:
        try:
            medir_velocidad()
        except Exception as e:
            print(f"Error en speedtest: {e}")
        time.sleep(INTERVALO_VELOCIDAD)


def hilo_latencia():
    time.sleep(10)  # esperar a que arranque todo
    while True:
        try:
            medir_latencia_servicios()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Latencia medida: "
                  f"{len(state['latencia_servicios'])} servicios")
        except Exception as e:
            print(f"Error en latencia: {e}")
        time.sleep(INTERVALO_LATENCIA)


# ---------- SISTEMA (CPU / RAM / TEMPERATURA DEL SERVIDOR) ----------

def _leer_proc_stat():
    """Devuelve (total, idle) de /proc/stat, o None si no se puede leer."""
    try:
        with open("/proc/stat") as f:
            partes = f.readline().split()
        nums = [int(x) for x in partes[1:]]
        return sum(nums), nums[3] + nums[4]  # total, idle+iowait
    except Exception:
        return None


def medir_cpu_pct():
    """Uso de CPU en % midiendo /proc/stat con 1 s de diferencia."""
    a = _leer_proc_stat()
    if not a:
        return None
    time.sleep(1)
    b = _leer_proc_stat()
    if not b:
        return None
    total = b[0] - a[0]
    idle = b[1] - a[1]
    if total <= 0:
        return None
    return round((1 - idle / total) * 100, 1)


def medir_ram():
    """Devuelve (usada_gb, total_gb, pct) desde /proc/meminfo, o None."""
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for linea in f:
                k, v = linea.split(":")
                mem[k.strip()] = int(v.split()[0])  # kB
        total = mem["MemTotal"]
        disp = mem.get("MemAvailable", mem.get("MemFree", 0))
        usada = total - disp
        return (round(usada / 1048576, 1), round(total / 1048576, 1),
                round(usada / total * 100, 1))
    except Exception:
        return None


def medir_temperatura():
    """Temperatura del CPU en °C. Prueba zonas térmicas y sensores hwmon.
    Devuelve None si el equipo no expone ningún sensor."""
    cands = []
    try:
        import glob
        for ruta in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            try:
                with open(ruta) as f:
                    v = int(f.read().strip())
                if 0 < v < 200000:  # miligrados razonables
                    cands.append(v / 1000)
            except Exception:
                pass
        for ruta in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
            try:
                with open(ruta) as f:
                    v = int(f.read().strip())
                if 0 < v < 200000:
                    cands.append(v / 1000)
            except Exception:
                pass
    except Exception:
        pass
    if not cands:
        return None
    return round(max(cands), 1)


def medir_sistema():
    """Actualiza el estado con CPU, temperatura y RAM del servidor."""
    cpu = medir_cpu_pct()
    ram = medir_ram()
    temp = medir_temperatura()
    if cpu is not None:
        state["cpu_pct"] = cpu
    if ram is not None:
        state["ram_usada_gb"], state["ram_total_gb"], state["ram_pct"] = ram
    state["cpu_temp_c"] = temp  # None = sin sensor
    state["ultimo_sistema"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sistema: CPU {cpu}% | "
          f"Temp {temp}°C | RAM {state['ram_usada_gb']}/{state['ram_total_gb']} GB")


def hilo_sistema():
    time.sleep(5)  # esperar a que arranque todo
    while True:
        try:
            medir_sistema()
        except Exception as e:
            print(f"Error en sistema: {e}")
        time.sleep(10)


# ---------- PANEL WEB ----------

# Plantilla del panel: templates/index.html (se separó de este archivo).


def clave_ip(d):
    """Ordena por números: 192.168.1.2 va antes que 192.168.1.10."""
    try:
        return tuple(int(x) for x in d["ip"].split("."))
    except Exception:
        return (999, 999, 999, 999)


@app.route("/")
def index():
    pct = round(state["pings_ok"] * 100 / state["total_pings"], 1) if state["total_pings"] else 100.0
    return render_template("index.html", s=state, devices=devices, history=history,
                           outages=outages, pct=pct, servicios=PUERTOS_COMUNES,
                           devices_ordenados=sorted(devices.values(), key=clave_ip))


@app.route("/scan")
def scan_now():
    threading.Thread(target=escanear_dispositivos, daemon=True).start()
    return redirect("/")


@app.route("/speedtest")
def speed_now():
    threading.Thread(target=medir_velocidad, daemon=True).start()
    return redirect("/")


@app.route("/latencia")
def latencia_now():
    threading.Thread(target=medir_latencia_servicios, daemon=True).start()
    return redirect("/")


@app.route("/api/status")
def api_status():
    return jsonify({"state": state, "devices": devices, "outages": outages[-10:]})


def _token_ok(valor):
    # Comparación segura; None o vacío nunca es válido.
    if not valor or not TOKEN:
        return False
    return hmac.compare_digest(valor, TOKEN)


@app.before_request
def pedir_token():
    # Sin token configurado, el panel no pide nada. Con token, hay que abrirlo
    # una vez con ?token=... o con el formulario de /login; la cookie
    # (30 días, SameSite=Lax) hace el resto. Los botones /scan, /speedtest y
    # /latencia quedan cubiertos por este mismo control: sin cookie, un enlace
    # o imagen externa no puede dispararlos.
    if not TOKEN or request.path == "/login":
        return
    if _token_ok(request.cookies.get("mon_token")) or _token_ok(request.args.get("token")):
        return
    return redirect("/login")


@app.after_request
def guardar_cookie(resp):
    if TOKEN and _token_ok(request.args.get("token")):
        resp.set_cookie("mon_token", TOKEN, httponly=True, samesite="Lax",
                        max_age=60 * 60 * 24 * 30)
    return resp


FORM_LOGIN = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar al monitor</title>
<style>
body{font-family:system-ui,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
h1{font-size:22px;margin:0 0 4px}
p{color:#94a3b8;font-size:14px}
.err{color:#f87171}
form{margin:20px 0}
input{font-size:16px;padding:12px;border-radius:10px;border:1px solid #334155;background:#1e293b;color:#e2e8f0;width:100%;max-width:320px;box-sizing:border-box}
button{font-size:16px;padding:12px 20px;border-radius:10px;border:0;background:#2563eb;color:#fff;margin-top:10px;cursor:pointer}
small{color:#94a3b8}
code{background:#1e293b;padding:2px 6px;border-radius:6px}
</style></head><body>
<h1>📡 Monitor de Red v3</h1>
<p>Este panel está protegido. Escribe el token para entrar.</p>
{% if error %}<p class="err">Token incorrecto, inténtalo de nuevo.</p>{% endif %}
<form method="post">
<input type="password" name="token" placeholder="Token" autocomplete="off" autofocus>
<br><button type="submit">Entrar</button>
</form>
<p><small>El token está en el servidor, en /etc/monitor-red.env — lo ves con <code>sudo cat /etc/monitor-red.env</code>. Al entrar se guarda 30 días en este navegador, así que después entras directo.</small></p>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    # Formulario para entrar al panel sin tener que pegar el token en la
    # dirección: con la cookie guardada, la recarga automática del panel
    # (cada 30 s) y los botones siguen funcionando.
    if not TOKEN:
        return redirect("/")
    error = False
    if request.method == "POST":
        if _token_ok(request.form.get("token")):
            resp = redirect("/")
            resp.set_cookie("mon_token", TOKEN, httponly=True, samesite="Lax",
                            max_age=60 * 60 * 24 * 30)
            return resp
        error = True
    return render_template_string(FORM_LOGIN, error=error)


@app.route("/dashboard")
def dashboard():
    """Panel de casa: reloj, clima, vuelos, red y cámaras.
    Sirve dashboard.html si está junto a monitor_v3.py."""
    ruta = os.path.join(BASE_DIR, "dashboard.html")
    if os.path.exists(ruta):
        return send_from_directory(BASE_DIR, "dashboard.html")
    return "Falta dashboard.html junto a monitor_v3.py", 404


if __name__ == "__main__":
    print("=" * 50)
    print("  MONITOR DE RED v3 - Termux")
    print(f"  Router: {state['router_ip']}  Red: {state['subnet']}")
    print(f"  Panel: http://0.0.0.0:{PUERTO}  (abre http://IP_DEL_CELULAR:5000)")
    print("  Nuevo en v3: puertos por equipo + nombres/fabricante + latencia a servicios + CPU/RAM/temperatura del servidor")
    print("=" * 50)
    threading.Thread(target=hilo_ping, daemon=True).start()
    threading.Thread(target=hilo_escaneo, daemon=True).start()
    threading.Thread(target=hilo_velocidad, daemon=True).start()
    threading.Thread(target=hilo_latencia, daemon=True).start()
    threading.Thread(target=hilo_sistema, daemon=True).start()
    app.run(host="0.0.0.0", port=PUERTO, debug=False)
