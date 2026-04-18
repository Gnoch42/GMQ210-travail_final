# Système d'analyse de fréquentation de parc

Ce système utilise une caméra installée dans un parc pour détecter les personnes, suivre leurs déplacements, et convertir leurs positions en coordonnées GPS. Les données sont stockées dans une base de données spatiale (PostGIS) pour analyse dans QGIS.

## Matériel nécessaire

- Raspberry Pi 4 (4 Go RAM) avec Raspberry Pi OS Lite
- Module caméra IMX219 (connecté au port CSI du Pi)
- Carte microSD 32 Go+
- Alimentation USB-C pour le Pi
- Mac (pour le traitement et l'analyse)
- Les deux appareils doivent être sur le même réseau WiFi

## Architecture

```
Parc                                      Bureau
┌─────────────────────┐                   ┌──────────────────────────────┐
│  Raspberry Pi 4     │                   │  Mac                         │
│                     │     WiFi          │                              │
│  IMX219 → MediaMTX ─┼──── RTSP ───────→│  OpenCV → YOLOv8 → Tracker  │
│           (stream)  │                   │           → Homographie      │
│  Flask API          │                   │           → PostGIS          │
│  (contrôle caméra)  │                   │                              │
└─────────────────────┘                   │  QGIS (visualisation)        │
                                          └──────────────────────────────┘
```

La caméra capture les images sur le Pi, MediaMTX les stream en RTSP via WiFi, et tout le traitement (détection, tracking, conversion GPS, stockage) se fait sur le Mac.

---

## 1. Configuration du Raspberry Pi

### 1.1 Installer Raspberry Pi OS Lite

Utiliser Raspberry Pi Imager sur le Mac pour flasher Raspberry Pi OS Lite (64-bit) sur la carte SD. Dans les paramètres avancés, configurer le WiFi et activer SSH.

### 1.2 Se connecter au Pi

```bash
ssh pi@<IP_DU_PI>
```

Pour trouver l'IP du Pi, regarder dans l'interface de ton routeur ou utiliser `ping camerapi1.local`.

### 1.3 Installer les outils caméra

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y rpicam-apps-lite
```

Vérifier que la caméra est détectée :

```bash
rpicam-hello --list-cameras -v
```

### 1.4 Installer MediaMTX (serveur de streaming RTSP)

```bash
cd ~
wget https://github.com/bluenviron/mediamtx/releases/download/v1.11.3/mediamtx_v1.11.3_linux_arm64v8.tar.gz
tar -xzf mediamtx_v1.11.3_linux_arm64v8.tar.gz
sudo cp mediamtx /usr/local/bin/
sudo cp mediamtx.yml /usr/local/etc/mediamtx.yml
```

Modifier la configuration pour activer la caméra :

```bash
sudo nano /usr/local/etc/mediamtx.yml
```

Aller tout en bas du fichier et remplacer la section `paths:` par :

```yaml
paths:
  parc:
    source: rpiCamera
  all_others:
```

Attention à l'indentation : 2 espaces devant `parc:`, 4 espaces devant `source:`.

Donner les droits de modification à l'utilisateur pi :

```bash
sudo chown pi:pi /usr/local/etc/mediamtx.yml
```

### 1.5 Créer le service systemd pour MediaMTX

```bash
sudo nano /etc/systemd/system/mediamtx.service
```

Contenu :

```
[Unit]
Description=MediaMTX RTSP Server
After=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx /usr/local/etc/mediamtx.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Activer et démarrer :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

Vérifier que ça tourne :

```bash
sudo systemctl status mediamtx
```

### 1.6 Tester le flux RTSP

Depuis le Mac, ouvrir VLC → Fichier → Ouvrir un flux réseau :

```
rtsp://<IP_DU_PI>:8554/parc
```

Ou avec ffplay (installer ffmpeg via `brew install ffmpeg`) :

```bash
ffplay rtsp://<IP_DU_PI>:8554/parc
```

### 1.7 Installer l'API Flask de contrôle

Sur le Pi, créer le projet :

```bash
mkdir -p ~/parc-camera
cd ~/parc-camera
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask psutil requests
```

Créer le fichier API :

```bash
nano ~/parc-camera/camera_api.py
```

Contenu :

```python
from flask import Flask, jsonify, request
import subprocess

app = Flask(__name__)

CONFIG_PATH = "/usr/local/etc/mediamtx.yml"

@app.route("/health", methods=["GET"])
def health():
    result = subprocess.run(
        ["systemctl", "is-active", "mediamtx"],
        capture_output=True, text=True
    )
    is_running = result.stdout.strip() == "active"
    return jsonify({
        "mediamtx_running": is_running,
        "status": "healthy" if is_running else "down"
    })

@app.route("/stream/<action>", methods=["POST"])
def stream_control(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Action invalide"}), 400
    result = subprocess.run(
        ["sudo", "systemctl", action, "mediamtx"],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        "message": f"Stream {action}" if success else f"Échec {action}",
        "success": success
    })

@app.route("/config", methods=["PUT"])
def update_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON requis"}), 400

    mapping = {
        "width": "rpiCameraWidth",
        "height": "rpiCameraHeight",
        "fps": "rpiCameraFPS",
        "bitrate": "rpiCameraBitrate",
    }

    with open(CONFIG_PATH, "r") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        for param, yml_key in mapping.items():
            if param in data and stripped.startswith(yml_key + ":"):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}{yml_key}: {data[param]}\n"

    with open(CONFIG_PATH, "w") as f:
        f.writelines(lines)

    result = subprocess.run(
        ["sudo", "systemctl", "restart", "mediamtx"],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        "message": "Configuration appliquée" if success else "Échec du redémarrage",
        "config": data,
        "success": success
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

### 1.8 Configurer sudo sans mot de passe pour systemctl

```bash
sudo visudo -f /etc/sudoers.d/mediamtx
```

Ajouter cette ligne :

```
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl start mediamtx, /usr/bin/systemctl stop mediamtx, /usr/bin/systemctl restart mediamtx
```

### 1.9 Créer le service systemd pour l'API Flask

```bash
sudo nano /etc/systemd/system/camera-api.service
```

Contenu :

```
[Unit]
Description=Camera Control Flask API
After=network-online.target mediamtx.service

[Service]
User=pi
WorkingDirectory=/home/pi/parc-camera
ExecStart=/home/pi/parc-camera/venv/bin/python camera_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Activer et démarrer :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camera-api
```

### 1.10 Vérifier que tout fonctionne sur le Pi

```bash
# Vérifier les services
sudo systemctl status mediamtx
sudo systemctl status camera-api

# Tester l'API depuis le Mac
curl http://<IP_DU_PI>:5000/health
```

---

## 2. Configuration du Mac

### 2.1 Installer les prérequis

```bash
# PostgreSQL et PostGIS
brew install postgresql@17 postgis
brew services start postgresql@17

# Ajouter au PATH (si pas déjà fait)
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# ffmpeg (pour enregistrer des vidéos)
brew install ffmpeg
```

### 2.2 Créer la base de données

```bash
createdb parc_frequentation
psql -d parc_frequentation -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

Créer les tables :

```bash
psql -d parc_frequentation -c "
CREATE TABLE detections (
    id SERIAL PRIMARY KEY,
    camera_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    person_track_id INTEGER,
    pixel_x REAL,
    pixel_y REAL,
    location GEOMETRY(Point, 4326),
    location_32198 GEOMETRY(Point, 32198),
    confidence REAL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_detections_timestamp ON detections(timestamp);
CREATE INDEX idx_detections_location ON detections USING GIST(location);
CREATE INDEX idx_detections_location_32198 ON detections USING GIST(location_32198);

CREATE TABLE occupancy_snapshots (
    id SERIAL PRIMARY KEY,
    camera_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    person_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_snapshots_timestamp ON occupancy_snapshots(timestamp);
"
```

### 2.3 Installer les dépendances Python

```bash
cd ~/parc-analyse
python3 -m venv venv
source venv/bin/activate
pip install opencv-python ultralytics psycopg2-binary requests numpy
```

### 2.4 Placer les fichiers

Le dossier du projet doit contenir :

```
projet/
├── main.py              # Pipeline principal
├── calibrate.py         # Script de calibration
└── calibration.json     # Généré par calibrate.py
```

---

## 3. Calibration (homographie)

La calibration permet de convertir les positions en pixels (sur l'image de la caméra) en coordonnées GPS réelles. Elle doit être faite une fois, quand la caméra est installée à sa position finale.

### 3.1 Enregistrer une vidéo depuis le terrain

```bash
ffmpeg -i rtsp://<IP_DU_PI>:8554/parc -c copy -t 60 ~/Desktop/capture_parc.mp4
```

### 3.2 Identifier les points de référence

Sur le terrain, identifier au moins 4 points fixes et bien visibles dans le champ de la caméra : coins de bancs, intersections de sentiers, bases de lampadaires, etc. Relever leurs coordonnées GPS avec Google Maps (appui long → copier les coordonnées) ou Google Earth Pro.

### 3.3 Lancer la calibration

```bash
python calibrate.py
```

Modifier le chemin de la vidéo dans `calibrate.py` si nécessaire (variable `VIDEO_PATH`).

Le script affiche une image de la vidéo. Pour chaque point de référence :

1. Cliquer sur le point dans l'image
2. Dans le terminal, entrer les coordonnées GPS au format : `45.379385, -71.929164`
3. Répéter pour au moins 4 points (plus il y en a, mieux c'est)
4. Taper `q` dans le terminal pour terminer

Le script génère un fichier `calibration.json` contenant la matrice de transformation.

Important : la résolution de la vidéo de calibration doit être la même que celle utilisée dans `main.py`. Si le flux RTSP est en 1920x1080, calibrer avec une vidéo en 1920x1080.

---

## 4. Utilisation

### 4.1 Lancer le pipeline de détection

```bash
python main.py
```

Par défaut, `main.py` se connecte au flux RTSP de la caméra. Pour analyser une vidéo enregistrée, modifier la variable `STREAM_URL` dans `main.py` :

```python
# Flux en direct
STREAM_URL = "rtsp://camerapi1.local:8554/parc"

# Vidéo enregistrée
STREAM_URL = "/chemin/vers/video.mp4"
```

### 4.2 Raccourcis clavier (dans la fenêtre vidéo)

| Touche | Action |
|--------|--------|
| `q` | Quitter |
| `h` | Vérifier l'état de la caméra (health check) |
| `s` | Arrêter le stream |
| `r` | Redémarrer le stream |
| `1` | Preset haute qualité (1920x1080, 15fps) |
| `2` | Preset standard (1280x720, 15fps) |
| `3` | Preset économie (640x480, 10fps) |

Les raccourcis de contrôle caméra (h, s, r, 1, 2, 3) ne fonctionnent qu'avec le flux en direct, pas avec une vidéo enregistrée.

### 4.3 Contrôler la caméra via le terminal

```bash
# Vérifier l'état
curl http://<IP_DU_PI>:5000/health

# Arrêter / démarrer / redémarrer le stream
curl -X POST http://<IP_DU_PI>:5000/stream/stop
curl -X POST http://<IP_DU_PI>:5000/stream/start
curl -X POST http://<IP_DU_PI>:5000/stream/restart

# Changer la résolution
curl -X PUT http://<IP_DU_PI>:5000/config \
  -H "Content-Type: application/json" \
  -d '{"width": 1920, "height": 1080, "fps": 15, "bitrate": 4000000}'
```

### 4.4 Enregistrer le flux vidéo

```bash
# Enregistrer 1 heure
ffmpeg -i rtsp://<IP_DU_PI>:8554/parc -c copy -t 3600 ~/Desktop/capture.mp4

# Ctrl+C pour arrêter avant
```

---

## 5. Visualisation dans QGIS

### 5.1 Se connecter à PostGIS

Dans QGIS : Couche → Ajouter une couche → PostGIS

Paramètres de connexion :

| Champ | Valeur |
|-------|--------|
| Hôte | localhost |
| Port | 5432 |
| Base de données | parc_frequentation |
| Utilisateur | *(ton nom d'utilisateur Mac)* |
| Mot de passe | *(laisser vide)* |

### 5.2 Ajouter les couches

Sélectionner la table `detections` avec la colonne géométrique `location_32198` (en EPSG:32198, NAD83 / Québec Lambert) pour que les points concordent avec les autres couches québécoises.

### 5.3 Requêtes utiles

```sql
-- Nombre total de détections
SELECT COUNT(*) FROM detections;

-- Derniers snapshots d'occupation
SELECT * FROM occupancy_snapshots ORDER BY timestamp DESC LIMIT 10;

-- Détections avec coordonnées GPS
SELECT pixel_x, pixel_y, ST_AsText(location), confidence
FROM detections WHERE location IS NOT NULL
ORDER BY timestamp DESC LIMIT 10;

-- Vider la base pour recommencer
DELETE FROM detections;
DELETE FROM occupancy_snapshots;
```

---

## Dépannage

| Problème | Solution |
|----------|----------|
| Caméra non détectée sur le Pi | Vérifier le câble ruban, essayer `rpicam-hello --list-cameras -v` |
| Flux RTSP ne fonctionne pas | `sudo systemctl status mediamtx` pour voir les erreurs |
| API Flask ne répond pas | `sudo systemctl status camera-api` pour voir les erreurs |
| YOLOv8 détecte mal | Ajuster le seuil de confiance (0.3 par défaut), vérifier l'éclairage |
| Détections multiples d'une même personne | Augmenter `max_distance` dans le Tracker |
| Points GPS décalés dans QGIS | Vérifier que la couche utilise `location_32198`, vérifier la calibration |
| PostgreSQL ne démarre pas | `brew services restart postgresql@17` |
| Erreur "No space left" sur le Pi | `TMPDIR=/home/pi/tmp pip install --no-cache-dir <paquet>` |
| Image coupée dans calibrate.py | Ajuster `DISPLAY_WIDTH` ou utiliser `cv2.WINDOW_NORMAL` |
