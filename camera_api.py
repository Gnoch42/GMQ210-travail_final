from flask import Flask, jsonify, request
import subprocess

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
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
    result = subprocess.run(
        ["sudo", "systemctl", "start", "mediamtx"],
        capture_output=True,
        text=True
    )
    success = result.returncode == 0

    return jsonify({
        "message": "Stream démarré" if success else "Échec du démarrage",
        "success": success
    })

@app.route("/stream/stop", methods=["POST"])
def stream_stop():
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
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON requis"}), 400

    config_path = "/usr/local/etc/mediamtx.yml"

    # 1. Lire le fichier
    with open(config_path, "r") as f:
        lines = f.readlines()

    # 2. Mapping: ce que le client envoie → ce qui est dans le fichier
    mapping = {
        "width": "rpiCameraWidth",
        "height": "rpiCameraHeight",
        "fps": "rpiCameraFPS",
        "bitrate": "rpiCameraBitrate",
    }

    # 3. Parcourir les lignes et remplacer
    for i, line in enumerate(lines):
        stripped = line.strip()
        for param, yml_key in mapping.items():
            if param in data and stripped.startswith(yml_key + ":"):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}{yml_key}: {data[param]}\n"

    # 4. Réécrire le fichier
    with open(config_path, "w") as f:
        f.writelines(lines)

    # 5. Redémarrer MediaMTX pour appliquer
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)