# hp-server

Archivos del servidor casero (HP Pavilion, Ubuntu Server 24.04, `hp-server`).

## Monitor de red v3 (`monitor/`)

Panel Flask que escanea la red de casa (192.168.1.0/24) cada 5 minutos:
dispositivos conectados, MAC, puertos abiertos, latencia a servicios y
prueba de velocidad. Corre como servicio systemd y arranca solo.

- `monitor_v3.py` — el monitor (versión corregida, la que corre en el servidor).
- `oui.py` — tabla OUI (MAC -> fabricante).
- `templates/index.html` — plantilla del panel.
- `monitor.service` — unidad systemd (va en `/etc/systemd/system/monitor.service`).
- Panel: `http://192.168.1.83:5000` en casa, `http://100.100.101.1:5000` por Tailscale.

### Seguridad: token para abrir el panel

El panel pide un token. Se guarda en `/etc/monitor-red.env` (permisos 600)
y el servicio lo carga con `EnvironmentFile`:

```bash
echo "MONITOR_TOKEN=$(openssl rand -hex 16)" | sudo tee /etc/monitor-red.env
sudo chmod 600 /etc/monitor-red.env
sudo systemctl restart monitor
```

Guarda el token en un lugar seguro (lo ves con `sudo cat /etc/monitor-red.env`).
Para abrir el panel, entra a `http://192.168.1.83:5000` y escribe el token en
el formulario que aparece; el navegador guarda una cookie por 30 días y
después entra normal. También sigue funcionando entrar una vez con
`http://192.168.1.83:5000/?token=TU_TOKEN`.

Sin el archivo `/etc/monitor-red.env` el servicio no arranca (así no queda
el panel abierto por accidente). Sin token configurado en el código, el panel
no pediría nada: por eso el archivo es obligatorio.

### Firewall (opcional, recomendado)

El panel escucha en toda la red (0.0.0.0) porque se usa desde la LAN y desde
Tailscale. Para que solo esas dos redes lleguen al puerto 5000:

```bash
sudo ufw allow OpenSSH        # ¡primero! Si no, te quedas sin SSH
sudo ufw allow from 192.168.1.0/24 to any port 5000
sudo ufw allow in on tailscale0 to any port 5000
sudo ufw enable
```

### Descargar o actualizar en el servidor

```bash
cd ~/monitor
mkdir -p templates
curl -L -o monitor_v3.py "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor_v3.py?t=$(date +%s)"
curl -L -o oui.py "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/oui.py?t=$(date +%s)"
curl -L -o templates/index.html "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/templates/index.html?t=$(date +%s)"
sudo systemctl restart monitor
```

El `?t=$(date +%s)` evita que GitHub sirva una copia vieja del archivo.

Si cambió `monitor.service`, también:

```bash
cd ~/monitor
curl -L -o monitor.service "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor.service?t=$(date +%s)"
sudo cp monitor.service /etc/systemd/system/monitor.service
sudo systemctl daemon-reload
sudo systemctl restart monitor
```

(Si el repositorio es privado, la descarga directa necesita un token;
hazlo público o usa `git clone` con un token de acceso.)

### Instalar desde cero en el servidor

```bash
sudo apt install -y python3-flask speedtest-cli nmap python3-zeroconf
mkdir -p ~/monitor/templates && cd ~/monitor
curl -L -o monitor_v3.py "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor_v3.py?t=$(date +%s)"
curl -L -o oui.py "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/oui.py?t=$(date +%s)"
curl -L -o templates/index.html "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/templates/index.html?t=$(date +%s)"
curl -L -o monitor.service "https://raw.githubusercontent.com/Azami0925/hp-server/main/monitor/monitor.service?t=$(date +%s)"
sudo cp monitor.service /etc/systemd/system/monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now monitor
systemctl status monitor
```

`python3-zeroconf` es opcional pero recomendado: permite descubrir nombres
de dispositivos por mDNS (Chromecast, teles, Echo, etc.).
