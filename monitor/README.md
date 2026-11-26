# hp-server

Archivos del servidor casero (HP Pavilion, Ubuntu Server 24.04, `hp-server`).

## Monitor de red v3 (`monitor/`)

Panel Flask que escanea la red de casa (192.168.1.0/24) cada 5 minutos:
dispositivos conectados, MAC, puertos abiertos, latencia a servicios y
prueba de velocidad. Corre como servicio systemd y arranca solo.

- `monitor_v3.py` — el monitor (versión corregida, la que corre en el servidor).
- `monitor.service` — unidad systemd (va en `/etc/systemd/system/monitor.service`).
- Panel: `http://192.168.1.83:5000` en casa, `http://100.100.101.1:5000` por Tailscale.

### Descargar o actualizar en el servidor

```bash
cd ~/monitor
curl -L -o monitor_v3.py https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor_v3.py
sudo systemctl restart monitor
```

(Si el repositorio es privado, la descarga directa necesita un token;
hazlo público o usa `git clone` con un token de acceso.)

### Instalar desde cero en el servidor

```bash
sudo apt install -y python3-flask speedtest-cli nmap python3-zeroconf
mkdir -p ~/monitor && cd ~/monitor
curl -L -o monitor_v3.py https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor_v3.py
sudo cp monitor.service /etc/systemd/system/monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now monitor
systemctl status monitor
```

`python3-zeroconf` es opcional pero recomendado: permite descubrir nombres
de dispositivos por mDNS (Chromecast, teles, Echo, etc.).
