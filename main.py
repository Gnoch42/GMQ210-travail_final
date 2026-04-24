"""
PIPELINE PRINCIPAL DE SUIVI DE PERSONNES EN TEMPS RÉEL
========================================================

Ce script constitue le cœur du système de détection et suivi de personnes dans un parc.
Il assure les fonctionnalités suivantes :

1. CAPTURE VIDÉO : Reçoit le flux RTSP d'une caméra Raspberry Pi déployée sur le terrain
2. DÉTECTION : Utilise YOLOv8 pour identifier les personnes dans chaque frame
3. SUIVI : Associe les détections sur plusieurs frames pour créer des trajectoires d'individus
4. GÉOLOCALISATION : Convertit les coordonnées pixel en coordonnées GPS via une homographie
5. PERSISTANCE : Enregistre les détections dans une base de données PostGIS avec géométrie
6. VISUALISATION : Affiche les résultats en temps réel avec HUD informatif
7. CONTRÔLE CAMÉRA : Permet de piloter la caméra et la configuration du stream via API

Les données sont enregistrées dans la table "detections" avec :
- Horodatage UTC exact
- Identifiant unique de tracking pour suivre une personne entre les frames
- Position en pixels et en coordonnées GPS (WGS84 et projection 32198)
- Confiance de détection YOLO

Des snapshots d'occupation globale sont créés périodiquement pour tracker
l'évolution du nombre de personnes dans la zone.
"""

import cv2
import math
import time
import json
import psycopg2
import requests
import numpy as np
from datetime import datetime, timezone
from ultralytics import YOLO


class Tracker:
    """
    Gestionnaire de suivi par centroides (centroid-tracking) pour associer les détections
    sur plusieurs frames et maintenir des identifiants persistants pour chaque personne.

    Algorithme :
    - Chaque personne reçoit un ID unique dès sa première détection
    - À chaque nouveau frame, on calcule les distances entre les centroides des
      personnes détectées au frame précédent et ceux du frame courant
    - On apparie les détections de façon optimale (greedy matching par distance croissante)
    - Une personne qui n'est pas détectée pendant trop longtemps est supprimée
      (permet de gérer les personnes qui sortent du champ ou sont occultées)
    """

    def __init__(self, max_distance=50, max_disappeared=30):
        """
        Initialise le tracker.

        Paramètres :
            max_distance : distance euclidienne maximale (en pixels) pour considérer qu'une
                           nouvelle détection appartient à la même personne. Au-delà, on crée
                           un nouvel ID. Défaut : 50 pixels.
            max_disappeared : nombre de frames consécutifs sans détection avant de supprimer
                              l'ID d'une personne. Défaut : 30 frames (~1 seconde à 30 FPS).
        """
        # Prochain ID à attribuer à une nouvelle personne
        self.next_id = 0
        # Dictionnaire : person_id → (cx, cy) avec les centroides actuels
        self.persons = {}
        # Dictionnaire : person_id → nombre de frames où la personne n'a pas été détectée
        self.disappeared = {}
        # Seuil de distance pour l'appariement
        self.max_distance = max_distance
        # Seuil de disparition avant suppression de l'ID
        self.max_disappeared = max_disappeared

    def update(self, detections):
        """
        Met à jour l'état du tracker avec les détections du frame courant.

        Paramètres :
            detections : liste de tuples (cx, cy) représentant les centroides détectés
                         dans le frame courant par YOLO.

        Retour :
            Dictionnaire {person_id : (cx, cy)} avec toutes les personnes actuellement
            suivies (i.e., qui ont été détectées très récemment).

        Cas traités :
            - Aucune détection : incrémente les compteurs de disparition
            - Pas de personne précédente : crée un nouvel ID pour chaque détection
            - Matching normal : apparie les détections avec les personnes existantes
        """

        # CAS 1 : Aucune détection dans le frame courant
        if len(detections) == 0:
            # Incrémente le compteur de disparition pour chaque personne suivie
            for person_id in list(self.disappeared.keys()):
                self.disappeared[person_id] += 1
                # Si une personne a disparu trop longtemps, on la supprime
                if self.disappeared[person_id] > self.max_disappeared:
                    del self.persons[person_id]
                    del self.disappeared[person_id]
            return self.persons

        # CAS 2 : Aucune personne en cours de suivi (premier frame ou tous ont disparu)
        if len(self.persons) == 0:
            # Assigne un ID unique à chaque détection
            for centroid in detections:
                self.persons[self.next_id] = centroid
                self.disappeared[self.next_id] = 0
                self.next_id += 1
            return self.persons

        # CAS 3 : Appariement normal (certaines personnes existent, certaines détections arrivent)

        # Récupère les IDs et les positions des personnes actuellement suivies
        person_ids = list(self.persons.keys())
        person_centroids = list(self.persons.values())

        # ÉTAPE 1 : Calcule les distances euclidiennes entre tous les pairs
        # (personne précédente, détection courante)
        distances = {}
        for i, pid in enumerate(person_ids):
            for j, det in enumerate(detections):
                px, py = person_centroids[i]
                dx, dy = det
                # Distance euclidienne : sqrt((dx - px)^2 + (dy - py)^2)
                dist = math.sqrt((px - dx) ** 2 + (py - dy) ** 2)
                distances[(i, j)] = dist

        # ÉTAPE 2 : Appariement glouton (greedy matching)
        # On traite les paires dans l'ordre des distances croissantes pour maximiser
        # l'exactitude du matching
        used_persons = set()
        used_detections = set()

        # Trie les paires par distance (du plus proche au plus loin)
        for (i, j), dist in sorted(distances.items(), key=lambda x: x[1]):
            # Ignore si cette personne ou cette détection est déjà appariée
            if i in used_persons or j in used_detections:
                continue
            # Ignore les paires trop éloignées (distance > max_distance)
            if dist > self.max_distance:
                continue

            # Apparie : met à jour la position de la personne avec la nouvelle détection
            pid = person_ids[i]
            self.persons[pid] = detections[j]
            # Réinitialise le compteur de disparition (on vient de la détecter)
            self.disappeared[pid] = 0
            # Marque cette paire comme traitée
            used_persons.add(i)
            used_detections.add(j)

        # ÉTAPE 3 : Gère les personnes qui n'ont pas été appariées
        for i, pid in enumerate(person_ids):
            if i not in used_persons:
                # Incrémente le compteur de disparition (personne non détectée cette frame)
                self.disappeared[pid] += 1
                # Supprime si elle a disparu trop longtemps
                if self.disappeared[pid] > self.max_disappeared:
                    del self.persons[pid]
                    del self.disappeared[pid]

        # ÉTAPE 4 : Gère les détections qui n'ont pas été appariées
        for j, det in enumerate(detections):
            if j not in used_detections:
                # Nouvelle personne : crée un nouvel ID unique
                self.persons[self.next_id] = det
                self.disappeared[self.next_id] = 0
                self.next_id += 1

        return self.persons


# ============================================================================
# CONTRÔLE DE LA CAMÉRA VIA API FLASK
# ============================================================================
# L'API s'exécute sur le Raspberry Pi et permet de :
# - Vérifier l'état du service de streaming (health check)
# - Démarrer/arrêter/redémarrer le serveur RTSP
# - Modifier la configuration vidéo à la volée (résolution, fps, bitrate)

PI_API = "http://camerapi1.local:5000"

def camera_control(action):
    """
    Envoie une commande de contrôle simple au service MediaMTX sur le Raspberry Pi.

    Paramètres :
        action : chaîne parmi ["start", "stop", "restart"] pour contrôler le stream RTSP.

    Retour :
        Dictionnaire JSON de la réponse, ou None si l'API ne répond pas.

    Exceptions :
        Capte toute erreur réseau et affiche un message d'erreur.
    """
    try:
        # Envoie une requête POST avec un timeout de 5 secondes
        r = requests.post(f"{PI_API}/stream/{action}", timeout=5)
        data = r.json()
        # Affiche le message retourné par l'API
        print(f"[Caméra] {data.get('message', action)}")
        return data
    except Exception as e:
        # En cas d'erreur (timeout, connexion refusée, etc.), affiche et retourne None
        print(f"[Caméra] Erreur: {e}")
        return None

def camera_config(preset):
    """
    Change la configuration vidéo (résolution, fps, bitrate) en sélectionnant un preset.

    Paramètres :
        preset : chaîne parmi ["haute", "standard", "economie"] pour choisir le profil.

    Retour :
        Dictionnaire JSON de la réponse, ou None si le preset est invalide ou l'API ne répond pas.

    Presets disponibles :
        - haute :    1920x1080 @ 15 fps @ 4 Mbps (haute qualité, consommation élevée)
        - standard : 1280x720 @ 15 fps @ 2 Mbps (équilibre qualité/consommation)
        - economie : 640x480 @ 10 fps @ 800 kbps (faible consommation, qualité réduite)
    """
    presets = {
        "high":      {"width": 1920, "height": 1080, "fps": 15, "bitrate": 4000000},
        "standard":  {"width": 1280, "height": 720,  "fps": 15, "bitrate": 2000000},
        "economy":   {"width": 640,  "height": 480,  "fps": 10, "bitrate": 800000},
    }
    if preset not in presets:
        print(f"[Config] Unknown preset: {preset}")
        return None
    try:
        # Envoie une requête PUT avec le dictionnaire de configuration du preset
        r = requests.put(f"{PI_API}/config", json=presets[preset], timeout=10)
        data = r.json()
        print(f"[Config] {data.get('message', preset)}")
        return data
    except Exception as e:
        print(f"[Config] Erreur: {e}")
        return None

def camera_health():
    """
    Vérifie l'état du service MediaMTX et retourne un rapport de santé.

    Retour :
        Dictionnaire JSON avec les clés "status" et "mediamtx_running", ou None si l'API ne répond pas.
        - status : "healthy" ou "down"
        - mediamtx_running : booléen indiquant si MediaMTX est actif
    """
    try:
        r = requests.get(f"{PI_API}/health", timeout=3)
        data = r.json()
        print(f"[Santé] Status: {data.get('status')} | MediaMTX: {data.get('mediamtx_running')}")
        return data
    except Exception as e:
        print(f"[Santé] Erreur: {e}")
        return None


# ============================================================================
# CONFIGURATION ET INITIALISATION
# ============================================================================

# Configuration de la base de données PostGIS
DB_CONFIG = {
    "dbname": "parc_frequentation",
    "user": "remybillette",
    "host": "localhost",
    "port": 5432,
}

# Intervalle entre deux snapshots d'occupation (en secondes)
SNAPSHOT_INTERVAL = 30

# Identifiant unique de la caméra (permet de distinguer les flux si plusieurs caméras)
CAMERA_ID = "camerapi1"

# Chemin vers le fichier de calibration (homographie pixel → GPS)
CALIBRATION_PATH = "calibration.json"

# Instancie le modèle YOLOv8 nano (assez léger pour un Raspberry Pi avec GPU)
model = YOLO("yolov8n.pt")

# Crée le tracker avec les seuils par défaut
tracker = Tracker(max_distance=50, max_disappeared=30)

# Établit la connexion PostgreSQL
conn = psycopg2.connect(**DB_CONFIG)
conn.autocommit = True  # Les insertions sont automatiquement confirmées
cur = conn.cursor()

# Charger la matrice d'homographie (pour la conversion pixel → GPS)
homography_matrix = None
try:
    with open(CALIBRATION_PATH, "r") as f:
        calib = json.load(f)
    # Extrait la matrice 3x3 du fichier JSON et la convertit en array NumPy
    homography_matrix = np.array(calib["matrix"], dtype=np.float64)
    print("Calibration chargée!")
except FileNotFoundError:
    # Si le fichier n'existe pas, on fonctionne en mode "pixels uniquement"
    print("Pas de calibration — coordonnées GPS non disponibles")

# URL du flux RTSP fourni par le serveur MediaMTX sur le Raspberry Pi
STREAM_URL = "rtsp://camerapi1.local:8554/parc"

# Ouvre le flux RTSP
cap = cv2.VideoCapture(STREAM_URL)
if not cap.isOpened():
    # Si la connexion échoue, arrête le script
    print("Impossible de se connecter au flux")
    exit()

# Affiche un message de bienvenue et les raccourcis clavier
print("Connecté au flux! Pipeline actif.")
print("")
print("=== Raccourcis clavier ===")
print("  q     → Quitter")
print("  h     → Health check")
print("  s     → Stop stream")
print("  r     → Restart stream")
print("  1     → Preset haute qualité")
print("  2     → Preset standard")
print("  3     → Preset économie")
print("==========================")

# Variables de gestion du timing et de l'affichage
last_snapshot = time.time()  # Horodatage du dernier snapshot enregistré
frame_count = 0              # Compteur total de frames traitées
status_message = ""          # Message d'état temporaire affiché sur l'écran
status_time = 0              # Horodatage du message d'état (pour l'expiration)

# ============================================================================
# BOUCLE PRINCIPALE DE TRAITEMENT
# ============================================================================

while True:
    # Lit le prochain frame du flux RTSP
    ret, frame = cap.read()
    if not ret:
        # Perte de connexion : reconnexion après une pause
        print("Perte du flux, reconnexion...")
        cap.release()
        time.sleep(2)
        cap = cv2.VideoCapture(STREAM_URL)
        continue

    # Redimensionne le frame à la résolution cible (1920x1080)
    frame = cv2.resize(frame, (1920, 1080))

    # Exécute YOLO pour détecter les personnes
    # conf=0.3 : garde uniquement les détections avec une confiance > 30%
    results = model(frame, verbose=False, conf=0.3)

    # Extrait les informations des boîtes englobantes (pour les personnes seulement)
    centroids = []    # Liste des (cx, cy) des centroïdes détectés
    confidences = []  # Liste des scores de confiance YOLO correspondants
    boxes = []        # Liste des (x1, y1, x2, y2) pour le dessin

    for box in results[0].boxes:
        # YOLO class 0 = personne (voir la taxonomie COCO)
        if int(box.cls[0]) == 0:
            # Récupère les coordonnées de la boîte englobante
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            # Calcule le centroïde (point central de la boîte)
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            centroids.append((cx, cy))
            confidences.append(float(box.conf[0]))
            boxes.append((x1, y1, x2, y2))

    # Met à jour le tracker avec les nouveaux centroides
    persons = tracker.update(centroids)

    # ========================================================================
    # ENREGISTREMENT DANS LA BASE POSTGIS
    # ========================================================================
    now = datetime.now(timezone.utc)  # Horodatage UTC du moment de traitement

    for pid, (cx, cy) in persons.items():
        # Recherche la confiance YOLO associée à ce centroïde
        conf = 0.0
        for i, (dx, dy) in enumerate(centroids):
            # Compare les positions (avec une petite tolérance pour les erreurs d'arrondi)
            if abs(cx - dx) < 1 and abs(cy - dy) < 1:
                conf = confidences[i]
                break

        # Convertit les coordonnées pixel en coordonnées GPS via homographie
        lat, lon = None, None
        if homography_matrix is not None:
            # Formate le point pour cv2.perspectiveTransform (doit être un array 3D)
            point = np.array([[[cx, cy]]], dtype=np.float64)
            result = cv2.perspectiveTransform(point, homography_matrix)
            # Récupère les coordonnées GPS (latitude, longitude)
            lat = float(result[0][0][0])
            lon = float(result[0][0][1])

        # Enregistre dans la base de données
        if lat is not None and lon is not None:
            # Cas avec GPS : enregistre les coordonnées en WGS84 (SRID 4326)
            # et aussi en projection UTM 32198 (système local du Québec)
            cur.execute(
                """INSERT INTO detections (camera_id, timestamp, person_track_id, pixel_x, pixel_y, location, location_32198, confidence)
                   VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 32198), %s)""",
                (CAMERA_ID, now, pid, cx, cy, lon, lat, lon, lat, conf)
            )
        else:
            # Cas sans GPS (pas de calibration) : enregistre les pixels seulement
            cur.execute(
                """INSERT INTO detections (camera_id, timestamp, person_track_id, pixel_x, pixel_y, confidence)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (CAMERA_ID, now, pid, cx, cy, conf)
            )

    # ========================================================================
    # SNAPSHOT PÉRIODIQUE D'OCCUPATION
    # ========================================================================
    # Tous les 30 secondes, on enregistre un résumé du nombre de personnes
    current_time = time.time()
    if current_time - last_snapshot >= SNAPSHOT_INTERVAL:
        cur.execute(
            "INSERT INTO occupancy_snapshots (camera_id, timestamp, person_count) VALUES (%s, %s, %s)",
            (CAMERA_ID, now, len(persons))
        )
        last_snapshot = current_time
        # Affiche un message temporaire sur l'écran
        status_message = f"Snapshot: {len(persons)} personne(s)"
        status_time = current_time

    # ========================================================================
    # DESSIN DES DÉTECTIONS SUR LE FRAME
    # ========================================================================
    # Pour chaque personne suivie, dessine la boîte englobante et l'ID
    for pid, (cx, cy) in persons.items():
        # Recherche la boîte englobante correspondant à ce centroïde
        for i, (dx, dy) in enumerate(centroids):
            if abs(cx - dx) < 1 and abs(cy - dy) < 1:
                x1, y1, x2, y2 = boxes[i]
                # Dessine un rectangle vert autour de la personne
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                # Affiche l'ID du track au-dessus de la boîte
                cv2.putText(frame, f"ID {pid}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                break

    # ========================================================================
    # AFFICHAGE DU HUD (HEAD-UP DISPLAY)
    # ========================================================================
    h, w = frame.shape[:2]  # Hauteur et largeur du frame

    # Affiche le nombre total de personnes détectées
    cv2.putText(frame, f"Personnes: {len(persons)}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

    # Affiche le message d'état temporaire (s'il n'a pas expiré dans les 5 dernières secondes)
    if status_message and current_time - status_time < 5:
        cv2.putText(frame, status_message, (10, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

    # Affiche les raccourcis clavier disponibles
    cv2.putText(frame, "q:quit  h:health  s:stop  r:restart  1:HD  2:STD  3:ECO",
                (10, h - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

    # Affiche le frame dans une fenêtre OpenCV
    cv2.imshow("Parc", frame)

    # ========================================================================
    # GESTION DES RACCOURCIS CLAVIER
    # ========================================================================
    # Attend 1 ms pour une touche (permet une réactivité fluide sans bloquer)
    key = cv2.waitKey(1) & 0xFF

    if key == ord("q"):
        # Quitter le programme
        break
    elif key == ord("h"):
        # Health check : demande l'état du service MediaMTX
        result = camera_health()
        if result:
            status_message = f"Sante: {result.get('status')}"
            status_time = time.time()
    elif key == ord("s"):
        # Arrêter le stream
        camera_control("stop")
        status_message = "Stream arrete"
        status_time = time.time()
    elif key == ord("r"):
        # Redémarrer le stream et relancer la capture
        camera_control("restart")
        status_message = "Stream redemarre"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("1"):
        # Passer en haute qualité (1920x1080) et relancer la capture
        camera_config("high")
        status_message = "Config: high quality"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("2"):
        # Passer en standard (1280x720) et relancer la capture
        camera_config("standard")
        status_message = "Config: standard"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("3"):
        # Passer en économie (640x480) et relancer la capture
        camera_config("economy")
        status_message = "Config: economy"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)

    # Incrémente le compteur et affiche un rapport tous les 100 frames
    frame_count += 1
    if frame_count % 100 == 0:
        print(f"Frame {frame_count} | Personnes: {len(persons)}")

# ============================================================================
# FERMETURE ET NETTOYAGE
# ============================================================================
cap.release()            # Libère la ressource vidéo
cur.close()              # Ferme le curseur PostgreSQL
conn.close()             # Ferme la connexion à la base de données
cv2.destroyAllWindows()  # Ferme toutes les fenêtres OpenCV
print("Pipeline arrêté")