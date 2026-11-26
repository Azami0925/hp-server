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

import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory

# ---------- CONFIGURACIÓN - edítala si tu red es diferente ----------
PUERTO = 5000
INTERVALO_PING = 10          # segundos entre pings
INTERVALO_ESCANEO = 300      # segundos entre escaneos de dispositivos (5 min)
INTERVALO_VELOCIDAD = 1800   # segundos entre tests de velocidad (30 min)
INTERVALO_LATENCIA = 120     # segundos entre mediciones de latencia a servicios
ROUTER_IP = "192.168.1.254"  # Telmex Infinitum: gateway es .254, no .1. Confirmado 2026-11-09.
SUBNET = "192.168.1.0/24"
# -------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

# Primeros 3 bytes de la MAC -> fabricante (OUI). Solo marcas comunes en casa.
OUI_FABRICANTES = {
    "50:C7:BF": "TP-Link", "60:E3:27": "TP-Link", "98:DA:C4": "TP-Link",
    "B0:4E:26": "TP-Link", "14:CC:20": "TP-Link", "84:D8:1B": "TP-Link",
    "F4:F2:6D": "TP-Link", "C0:25:A5": "TP-Link", "00:27:19": "TP-Link",
    "90:F6:52": "TP-Link", "E8:DE:27": "TP-Link", "1C:3B:F3": "TP-Link",
    "30:B5:C2": "TP-Link", "6C:19:8F": "TP-Link", "A8:2B:B5": "TP-Link",
    "50:FA:84": "TP-Link",
    "F0:27:2D": "Amazon", "44:65:0D": "Amazon", "0C:47:C9": "Amazon",
    "68:37:E9": "Amazon", "34:D2:70": "Amazon", "40:B4:CD": "Amazon",
    "74:C2:46": "Amazon", "88:71:E5": "Amazon", "18:74:2E": "Amazon",
    "A0:02:DC": "Amazon", "00:FC:8B": "Amazon", "50:DC:E7": "Amazon",
    "F4:F5:D8": "Google", "94:EB:2C": "Google", "54:60:09": "Google",
    "18:B4:30": "Google", "00:1A:11": "Google", "3C:5A:B4": "Google",
    "20:DF:B9": "Google",
    "24:6F:28": "Espressif", "24:0A:C4": "Espressif", "30:AE:A4": "Espressif",
    "3C:71:BF": "Espressif", "7C:DF:A1": "Espressif", "84:CC:A8": "Espressif",
    "98:F4:AB": "Espressif", "A0:76:4E": "Espressif", "BC:DD:C2": "Espressif",
    "C4:4F:33": "Espressif", "DC:4F:22": "Espressif", "E8:68:E7": "Espressif",
    "84:0D:8E": "Espressif", "40:F5:20": "Espressif", "08:3A:F2": "Espressif",
    "10:52:1C": "Espressif",
    "64:CC:2E": "Xiaomi", "8C:BE:BE": "Xiaomi", "F0:B4:29": "Xiaomi",
    "34:CE:00": "Xiaomi", "28:6C:07": "Xiaomi", "78:11:DC": "Xiaomi",
    "04:CF:8C": "Xiaomi", "10:2A:B3": "Xiaomi", "7C:2F:80": "Xiaomi",
    "0C:1D:AF": "Xiaomi", "14:F6:5A": "Xiaomi",
    "78:52:1A": "Samsung", "5C:51:88": "Samsung", "8C:77:12": "Samsung",
    "84:38:35": "Samsung", "C0:BD:D1": "Samsung", "34:23:87": "Samsung",
    "88:30:8A": "Samsung", "00:12:FB": "Samsung", "D0:22:BE": "Samsung",
    "9C:3A:AF": "Samsung", "60:A1:0A": "Samsung", "50:77:23": "Samsung",
    "88:63:DF": "Apple", "40:30:04": "Apple", "F0:18:98": "Apple",
    "8C:85:90": "Apple", "04:0C:CE": "Apple", "7C:6D:F8": "Apple",
    "6C:72:E7": "Apple", "04:15:52": "Apple", "28:CF:DA": "Apple",
    "9C:35:EB": "Apple", "3C:06:30": "Apple", "5C:96:9D": "Apple",
    "8C:7B:9D": "Apple",
    "00:9A:CD": "Huawei", "04:02:1F": "Huawei", "48:46:C1": "Huawei",
    "60:DE:44": "Huawei", "0C:96:BF": "Huawei", "24:2E:90": "Huawei",
    "58:22:33": "Huawei", "80:B6:86": "Huawei",
    "9C:30:5B": "LG", "98:0D:51": "LG", "74:A7:22": "LG", "20:47:ED": "LG",
    "00:1E:75": "LG", "3C:BD:3E": "LG", "78:5D:C8": "LG",
    "00:E0:4C": "Realtek",
    "D8:31:34": "Roku", "CC:6D:A0": "Roku", "B8:A1:75": "Roku",
    "00:1B:21": "Intel", "00:1F:3B": "Intel", "3C:FD:FE": "Intel",
    "4C:79:BA": "Intel", "7C:7A:91": "Intel", "9C:FC:01": "Intel",
}


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
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            if s.connect_ex((ip, p)) == 0:
                abiertos.append(p)
        except Exception:
            pass
        finally:
            s.close()

    try:
        with ThreadPoolExecutor(max_workers=len(PUERTOS_COMUNES)) as ex:
            list(ex.map(_probar, PUERTOS_COMUNES))
    except Exception:
        pass
    return sorted(abiertos)


def _nombre_desde_descripcion(url, timeout=1.5):
    """Lee el XML de descripción UPnP y saca friendlyName / fabricante + modelo."""
    try:
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
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
                nombre = _nombre_desde_descripcion(loc.group(1))
            if not nombre and srv:
                nombre = srv.group(1).strip().split(",")[0]
            if ip not in res and nombre:
                res[ip] = nombre
        s.close()
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


def medir_https(host, timeout=5):
    """Mide ms de una petición HTTPS (HEAD). None si falla."""
    t0 = time.time()
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conn.request("HEAD", "/")
        r = conn.getresponse()
        r.read()
        return round((time.time() - t0) * 1000)
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def medir_latencia_servicios():
    """Mide latencia a los 11 servicios de la lista. Guarda en state."""
    out = []
    for nombre, tipo, destino in SERVICIOS_LATENCIA:
        ms = ping(destino) if tipo == "ICMP" else medir_https(destino)
        if ms is not None:
            ms = round(ms)
        out.append({"nombre": nombre, "tipo": tipo, "destino": destino, "ms": ms})
    state["latencia_servicios"] = out
    state["ultima_latencia"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


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
    """Escanea la red sin root: nmap -sn (sin -PR, que pide root) + barrido ping + ip neigh.
    v3 suma: nombres por SSDP/mDNS, fabricante por MAC y puertos abiertos por equipo."""
    subnet = state["subnet"]
    base = subnet.split("/")[0].rsplit(".", 1)[0]  # ej: 192.168.1
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

    # 2) Barrido ping en paralelo (.1 a .254). Las cámaras que bloquean ping
    # igual dejan su MAC en la tabla ARP al intentar resolverlas.
    def _ping_una(i):
        ip = f"{base}.{i}"
        return ip if ping(ip) is not None else None

    try:
        with ThreadPoolExecutor(max_workers=30) as ex:
            for ip in ex.map(_ping_una, range(1, 255)):
                if ip:
                    _registrar(ip, vistos=vistos)
    except Exception as e:
        print(f"  Barrido ping falló: {e}")

    # 3) ip neigh para completar MACs
    out = ejecutar(["ip", "neigh"])
    for line in out.splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+).*lladdr\s+([0-9a-f:]{17})", line, re.I)
        if m:
            _registrar(m.group(1), m.group(2), vistos=vistos)

    # 4) v3: nombres por UPnP/SSDP y mDNS
    try:
        nombres = descubrir_ssdp()
        nombres.update(descubrir_mdns())
        for ip, nombre in nombres.items():
            if nombre:
                _registrar(ip, vistos=vistos)
                devices[ip]["nombre"] = nombre
    except Exception as e:
        print(f"  Descubrir nombres falló: {e}")

    # 5) v3: fabricante por MAC + puertos abiertos (solo equipos vistos en este escaneo)
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


def medir_velocidad():
    """Mide velocidad con speedtest-cli. Devuelve dict o None."""
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
            state["caidas_hoy"] = len([o for o in outages if o["inicio"].startswith(datetime.now().strftime("%Y-%m-%d"))])
            print(f"[{ahora}] ✅ Internet de vuelta tras {dur}s")
            caida_actual = None

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

HTML = """
<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Monitor de Red v3</title>
<style>
body{font-family:system-ui,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:24px 0 8px}
.sub{color:#94a3b8;font-size:13px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.card{background:#1e293b;border-radius:12px;padding:14px}
.card b{font-size:22px;display:block;margin-top:4px}
.ok{color:#4ade80}.bad{color:#f87171}.warn{color:#facc15}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden}
th,td{padding:10px;text-align:left;font-size:13px;border-bottom:1px solid #334155}
th{color:#94a3b8}.badge{background:#334155;padding:3px 8px;border-radius:20px;font-size:11px}
.btn{display:inline-block;background:#2563eb;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;margin:4px 4px 16px 0}
.btn2{background:#334155}
small{color:#94a3b8}
</style></head><body>
<h1>📡 Monitor de Red v3</h1>
<div class="sub">Router {{ s.router_ip }} · Red {{ s.subnet }} · Desde {{ s.inicio }} · Último ping {{ s.ultimo_ping or '-' }}</div>

<div class="grid">
  <div class="card">Internet<b class="{{ 'ok' if s.internet_ok else 'bad' }}">{{ '✅ ONLINE' if s.internet_ok else '❌ SIN INTERNET' if s.internet_ok==False else '...' }}</b></div>
  <div class="card">Ping router<b>{{ s.router_ping_ms }} ms</b></div>
  <div class="card">Ping Google<b>{{ s.google_ping_ms }} ms</b></div>
  <div class="card">Ping Cloudflare<b>{{ s.cloudflare_ping_ms }} ms</b></div>
  <div class="card">Bajada<b>{{ s.download_mbps or '-' }} Mbps</b><small>Último test: {{ s.ultimo_speedtest or '-' }}{% if s.servidor_speedtest %} · {{ s.servidor_speedtest }}{% endif %}</small></div>
  <div class="card">Subida<b>{{ s.upload_mbps or '-' }} Mbps</b></div>
  <div class="card">Activos ahora<b>{{ devices.values()|selectattr('activo')|list|length }}</b><small>Último escaneo: {{ s.ultimo_escaneo or '-' }}</small></div>
  <div class="card">Disponibilidad<b>{{ pct }}%</b><small>{{ s.pings_ok }}/{{ s.total_pings }} pings OK · Caídas hoy: {{ s.caidas_hoy }}</small></div>
</div>

<a class="btn" href="/scan">🔍 Escanear ahora</a>
<a class="btn" href="/speedtest">⚡ Medir velocidad ahora</a>
<a class="btn" href="/latencia">📶 Medir latencia ahora</a>
<a class="btn btn2" href="/">↻ Actualizar</a>

<h2>🖥️ Servidor</h2>
<div class="grid">
  <div class="card">CPU<b class="{{ 'ok' if s.cpu_pct is not none and s.cpu_pct < 70 else ('warn' if s.cpu_pct is not none and s.cpu_pct < 90 else 'bad') }}">{{ (s.cpu_pct|string + ' %') if s.cpu_pct is not none else '-' }}</b><small>Última medición: {{ s.ultimo_sistema or '-' }}</small></div>
  <div class="card">Temperatura<b class="{{ 'ok' if s.cpu_temp_c is not none and s.cpu_temp_c < 70 else ('warn' if s.cpu_temp_c is not none and s.cpu_temp_c < 85 else 'bad') }}">{{ (s.cpu_temp_c|string + ' °C') if s.cpu_temp_c is not none else 'sin sensor' }}</b></div>
  <div class="card">RAM<b>{{ (s.ram_usada_gb|string + ' / ' + s.ram_total_gb|string + ' GB') if s.ram_usada_gb is not none else '-' }}</b><small>{{ (s.ram_pct|string + ' % en uso') if s.ram_pct is not none else '' }}</small></div>
</div>

<h2>Latencia a servicios</h2>
<p><small>Última medición: {{ s.ultima_latencia or '-' }} · Se mide sola cada 2 minutos.</small></p>
<table><tr><th>Servicio</th><th>Tipo</th><th>Destino</th><th>Latencia</th></tr>
{% for l in s.latencia_servicios %}
<tr><td>{{ l.nombre }}</td><td>{{ l.tipo }}</td><td>{{ l.destino }}</td>
<td class="{{ 'ok' if l.ms is not none and l.ms < 50 else ('warn' if l.ms is not none and l.ms < 150 else 'bad') }}">{{ (l.ms|string + ' ms') if l.ms is not none else 'sin respuesta' }}</td></tr>
{% endfor %}</table>

<h2>Dispositivos conectados</h2>
<table><tr><th>IP</th><th>Nombre</th><th>Fabricante</th><th>MAC</th><th>Puertos abiertos</th><th>Primera vez</th><th>Última vez</th><th>Veces</th></tr>
{% for d in devices_ordenados %}
<tr><td>{{ d.ip }} {% if d.ip==s.router_ip %}<span class="badge">Router</span>{% endif %} {% if d.nuevo %}<span class="badge">🆕 Nuevo</span>{% endif %}</td>
<td>{{ d.nombre or '-' }}</td><td>{{ d.fabricante or '?' }}</td><td>{{ d.mac }}</td>
<td>{% if d.puertos %}{% for p in d.puertos %}<span class="badge">{{ p }} {{ servicios.get(p, '') }}</span> {% endfor %}{% else %}-{% endif %}</td>
<td>{{ d.first_seen }}</td><td>{{ d.last_seen }}</td><td>{{ d.veces_visto }}</td></tr>
{% endfor %}</table>
<p><small>Si un dispositivo no aparece, puede estar apagado o con WiFi apagado. El escaneo corre solo cada 5 minutos.</small></p>

<h2>Últimas caídas</h2>
{% if outages %}
<table><tr><th>Inicio</th><th>Fin</th><th>Duración</th></tr>
{% for o in outages|reverse %}
<tr><td>{{ o.inicio }}</td><td>{{ o.fin }}</td><td>{{ o.duracion_seg }} s</td></tr>
{% endfor %}</table>
{% else %}<p><small>Sin caídas registradas desde que arrancó el monitor.</small></p>{% endif %}

<h2>Historial reciente de ping (Google)</h2>
<p>{% for h in history|reverse %}<span style="color:{{ '#4ade80' if h.ok else '#f87171' }}">●</span> {{ h.hora }}: {{ h.google or 'sin respuesta' }} ms &nbsp;{% endfor %}</p>
<p><small>El panel se actualiza solo cada 30 segundos. Datos guardados en data_v3/.</small></p>
</body></html>
"""


def clave_ip(d):
    """Ordena por números: 192.168.1.2 va antes que 192.168.1.10."""
    try:
        return tuple(int(x) for x in d["ip"].split("."))
    except Exception:
        return (999, 999, 999, 999)


@app.route("/")
def index():
    pct = round(state["pings_ok"] * 100 / state["total_pings"], 1) if state["total_pings"] else 100.0
    return render_template_string(HTML, s=state, devices=devices, history=history,
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


@app.after_request
def permitir_dashboard(resp):
    # El dashboard vive en el mismo monitor, pero esto también lo deja
    # consultable desde otra página local si hace falta.
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


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
