"""
API FLASK DE CONTRÔLE DE LA CAMÉRA (Raspberry Pi)
===================================================

Ce script implémente une API REST qui s'exécute sur le Raspberry Pi et permet
à main.py (qui tourne sur une machine distante) de :

1. Vérifier l'état du service MediaMTX (health check)
2. Contrôler le service RTSP (start, stop, restart)
3. Modifier la configuration vidéo à la volée (résolution, fps, bitrate)

Chaque endpoint utilise systemctl pour interagir avec le service MediaMTX
et modifie directement le fichier de configuration YAML au besoin.

À exécuter sur le Raspberry Pi :
    python camera_api.py
    # ou en tant que service systemd pour une exécution persistante
"""

from flask import Flask, jsonify, request
import subprocess

# Initialise l'application Flask
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    """
    Endpoint de vérification de santé du service MediaMTX.

    Interroge systemctl pour vérifier si le service mediamtx est actif
    et retourne un rapport JSON.

    Paramètres : aucun (GET request)

    Retour (JSON) :
        {
            "mediamtx_running": true/false,
            "status": "healthy" ou "down"
        }

    Code HTTP : 200 OK

    """
    # Exécute : systemctl is-active mediamtx
    # Retourne "active" si le service est en cours d'exécution, sinon autre chose
    result = subprocess.run(
        ["systemctl", "is-active", "mediamtx"],
        capture_output=True,
        text=True
    )
    is_running = result.stdout.strip() == "active"

    return jsonify({
        "mediamtx_running": is_running,
        "status": "healthy" if is_running else "down"
    })


@app.route("/stream/start", methods=["POST"])
def stream_start():
    """
    Démarre le service MediaMTX et le serveur RTSP.

    Paramètres : aucun (POST request vide)

    Retour (JSON) :
        {
            "message": "Stream démarré" ou "Échec du démarrage",
            "success": true/false
        }

    Code HTTP : 200 OK

    Note : Nécessite les droits sudo pour exécuter systemctl start.
    """
    # Exécute : sudo systemctl start mediamtx
    result = subprocess.run(
        ["sudo", "systemctl", "start", "mediamtx"],
        capture_output=True,
        text=True
    )
    # Si returncode == 0, la commande a réussi
    success = result.returncode == 0

    return jsonify({
        "message": "Stream démarré" if success else "Échec du démarrage",
        "success": success
    })


@app.route("/stream/stop", methods=["POST"])
def stream_stop():
    """
    Arrête le service MediaMTX et le serveur RTSP.

    Paramètres : aucun (POST request vide)

    Retour (JSON) :
        {
            "message": "Stream arrêté" ou "Échec de l'arrêt",
            "success": true/false
        }

    Code HTTP : 200 OK
    """
    # Exécute : sudo systemctl stop mediamtx
    result = subprocess.run(
        ["sudo", "systemctl", "stop", "mediamtx"],
        capture_output=True,
        text=True
    )
    success = result.returncode == 0

    return jsonify({
        "message": "Stream arrêté" if success else "Échec de l'arrêt",
        "success": success
    })


@app.route("/stream/restart", methods=["POST"])
def stream_restart():
    """
    Redémarre le service MediaMTX (arrête puis redémarre).

    Utile pour réinitialiser le stream ou appliquer de nouveaux paramètres
    sans perte prolongée de service.

    Paramètres : aucun (POST request vide)

    Retour (JSON) :
        {
            "message": "Stream redémarré" ou "Échec du redémarrage",
            "success": true/false
        }

    Code HTTP : 200 OK
    """
    # Exécute : sudo systemctl restart mediamtx
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "mediamtx"],
        capture_output=True,
        text=True
    )
    success = result.returncode == 0

    return jsonify({
        "message": "Stream redémarré" if success else "Échec du redémarrage",
        "success": success
    })


@app.route("/config", methods=["PUT"])
def update_config():
    """
    Modifie la configuration vidéo de MediaMTX.

    Cette fonction :
    1. Lit le fichier de configuration YAML de MediaMTX
    2. Remplace les valeurs spécifiées par le client
    3. Réécrit le fichier avec les nouvelles valeurs
    4. Redémarre MediaMTX pour appliquer les changements

    Paramètres (JSON body) :
        Au moins un des paramètres suivants (tous optionnels) :
        - "width" : résolution horizontale (ex: 1920)
        - "height" : résolution verticale (ex: 1080)
        - "fps" : nombre de frames par seconde (ex: 15)
        - "bitrate" : débit vidéo en bits/seconde (ex: 4000000)

    Exemple de requête :
        {
            "width": 1280,
            "height": 720,
            "fps": 15,
            "bitrate": 2000000
        }

    Retour (JSON) :
        {
            "message": "Configuration appliquée" ou "Échec du redémarrage",
            "config": {...},  # Les paramètres envoyés par le client
            "success": true/false
        }

    Codes HTTP :
        - 200 OK : configuration appliquée
        - 400 Bad Request : aucun body JSON fourni
    """

    # Récupère le body JSON de la requête
    data = request.get_json()
    if not data:
        # Si aucun JSON n'est fourni, retourne une erreur
        return jsonify({"error": "Body JSON requis"}), 400

    # Chemin absolu du fichier de configuration MediaMTX
    config_path = "/usr/local/etc/mediamtx.yml"

    # ÉTAPE 1 : Lire le fichier de configuration existant
    with open(config_path, "r") as f:
        lines = f.readlines()

    # ÉTAPE 2 : Créer un mapping entre les paramètres du client et les clés YAML
    # À gauche : le nom attendu par le client
    # À droite : la clé dans le fichier YAML du serveur
    mapping = {
        "width": "rpiCameraWidth",
        "height": "rpiCameraHeight",
        "fps": "rpiCameraFPS",
        "bitrate": "rpiCameraBitrate",
    }

    # ÉTAPE 3 : Parcourir chaque ligne du fichier et remplacer les valeurs
    for i, line in enumerate(lines):
        stripped = line.strip()  # Version sans espaces de la ligne

        # Pour chaque paramètre que le client envoie
        for param, yml_key in mapping.items():
            if param in data and stripped.startswith(yml_key + ":"):
                # Récupère l'indentation originale (nombre d'espaces au début)
                # pour préserver la structure YAML
                indent = line[:len(line) - len(line.lstrip())]
                # Remplace la ligne avec la nouvelle valeur (en conservant l'indentation)
                lines[i] = f"{indent}{yml_key}: {data[param]}\n"

    # ÉTAPE 4 : Réécrire le fichier avec les lignes modifiées
    with open(config_path, "w") as f:
        f.writelines(lines)

    # ÉTAPE 5 : Redémarrer MediaMTX pour appliquer les changements
    # Sans redémarrage, les anciens paramètres resteraient en vigueur
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "mediamtx"],
        capture_output=True,
        text=True
    )
    success = result.returncode == 0

    return jsonify({
        "message": "Configuration appliquée" if success else "Échec du redémarrage",
        "config": data,
        "success": success
    })


# Lance le serveur Flask
if __name__ == "__main__":
    # Écoute sur tous les interfaces réseau (0.0.0.0) au port 5000
    # Permet à main.py d'accéder à l'API depuis un autre ordinateur sur le réseau
    app.run(host="0.0.0.0", port=5000)