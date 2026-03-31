import cv2
import math
import time
import psycopg2
import requests
from datetime import datetime, timezone
from ultralytics import YOLO


class Tracker:
    def __init__(self, max_distance=50, max_disappeared=30):
        self.next_id = 0
        self.persons = {}
        self.disappeared = {}
        self.max_distance = max_distance
        self.max_disappeared = max_disappeared

    def update(self, detections):
        if len(detections) == 0:
            for person_id in list(self.disappeared.keys()):
                self.disappeared[person_id] += 1
                if self.disappeared[person_id] > self.max_disappeared:
                    del self.persons[person_id]
                    del self.disappeared[person_id]
            return self.persons

        if len(self.persons) == 0:
            for centroid in detections:
                self.persons[self.next_id] = centroid
                self.disappeared[self.next_id] = 0
                self.next_id += 1
            return self.persons

        person_ids = list(self.persons.keys())
        person_centroids = list(self.persons.values())

        distances = {}
        for i, pid in enumerate(person_ids):
            for j, det in enumerate(detections):
                px, py = person_centroids[i]
                dx, dy = det
                dist = math.sqrt((px - dx) ** 2 + (py - dy) ** 2)
                distances[(i, j)] = dist

        used_persons = set()
        used_detections = set()

        for (i, j), dist in sorted(distances.items(), key=lambda x: x[1]):
            if i in used_persons or j in used_detections:
                continue
            if dist > self.max_distance:
                continue
            pid = person_ids[i]
            self.persons[pid] = detections[j]
            self.disappeared[pid] = 0
            used_persons.add(i)
            used_detections.add(j)

        for i, pid in enumerate(person_ids):
            if i not in used_persons:
                self.disappeared[pid] += 1
                if self.disappeared[pid] > self.max_disappeared:
                    del self.persons[pid]
                    del self.disappeared[pid]

        for j, det in enumerate(detections):
            if j not in used_detections:
                self.persons[self.next_id] = det
                self.disappeared[self.next_id] = 0
                self.next_id += 1

        return self.persons


# Contrôle de la caméra via Flask API
PI_API = "http://camerapi1.local:5000"

def camera_control(action):
    """Envoie une commande au Pi. action = start, stop, restart"""
    try:
        r = requests.post(f"{PI_API}/stream/{action}", timeout=5)
        data = r.json()
        print(f"[Caméra] {data.get('message', action)}")
        return data
    except Exception as e:
        print(f"[Caméra] Erreur: {e}")
        return None

def camera_config(preset):
    """Change la config caméra. preset = haute, standard, economie"""
    presets = {
        "haute":     {"width": 1920, "height": 1080, "fps": 15, "bitrate": 4000000},
        "standard":  {"width": 1280, "height": 720,  "fps": 15, "bitrate": 2000000},
        "economie":  {"width": 640,  "height": 480,  "fps": 10, "bitrate": 800000},
    }
    if preset not in presets:
        print(f"[Config] Preset inconnu: {preset}")
        return None
    try:
        r = requests.put(f"{PI_API}/config", json=presets[preset], timeout=10)
        data = r.json()
        print(f"[Config] {data.get('message', preset)}")
        return data
    except Exception as e:
        print(f"[Config] Erreur: {e}")
        return None

def camera_health():
    """Vérifie l'état de la caméra."""
    try:
        r = requests.get(f"{PI_API}/health", timeout=3)
        data = r.json()
        print(f"[Santé] Status: {data.get('status')} | MediaMTX: {data.get('mediamtx_running')}")
        return data
    except Exception as e:
        print(f"[Santé] Erreur: {e}")
        return None


# Configuration
STREAM_URL = "rtsp://camerapi1.local:8554/parc"
DB_CONFIG = {
    "dbname": "parc_frequentation",
    "user": "remybillette",
    "host": "localhost",
    "port": 5432,
}
SNAPSHOT_INTERVAL = 30
CAMERA_ID = "camerapi1"

# Initialisation
model = YOLO("yolov8n.pt")
tracker = Tracker(max_distance=50, max_disappeared=30)
conn = psycopg2.connect(**DB_CONFIG)
conn.autocommit = True
cur = conn.cursor()

cap = cv2.VideoCapture(STREAM_URL)
if not cap.isOpened():
    print("Impossible de se connecter au flux")
    exit()

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

last_snapshot = time.time()
frame_count = 0
status_message = ""
status_time = 0

while True:
    ret, frame = cap.read()
    if not ret:
        print("Perte du flux, reconnexion...")
        cap.release()
        time.sleep(2)
        cap = cv2.VideoCapture(STREAM_URL)
        continue

    results = model(frame, verbose=False, conf=0.3)

    centroids = []
    confidences = []
    boxes = []
    for box in results[0].boxes:
        if int(box.cls[0]) == 0:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            centroids.append((cx, cy))
            confidences.append(float(box.conf[0]))
            boxes.append((x1, y1, x2, y2))

    persons = tracker.update(centroids)

    # Stocker dans PostGIS
    now = datetime.now(timezone.utc)
    for pid, (cx, cy) in persons.items():
        conf = 0.0
        for i, (dx, dy) in enumerate(centroids):
            if abs(cx - dx) < 1 and abs(cy - dy) < 1:
                conf = confidences[i]
                break
        cur.execute(
            """INSERT INTO detections (camera_id, timestamp, person_track_id, pixel_x, pixel_y, confidence)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (CAMERA_ID, now, pid, cx, cy, conf)
        )

    # Snapshot périodique
    current_time = time.time()
    if current_time - last_snapshot >= SNAPSHOT_INTERVAL:
        cur.execute(
            "INSERT INTO occupancy_snapshots (camera_id, timestamp, person_count) VALUES (%s, %s, %s)",
            (CAMERA_ID, now, len(persons))
        )
        last_snapshot = current_time
        status_message = f"Snapshot: {len(persons)} personne(s)"
        status_time = current_time

    # Dessiner les détections
    for pid, (cx, cy) in persons.items():
        for i, (dx, dy) in enumerate(centroids):
            if abs(cx - dx) < 1 and abs(cy - dy) < 1:
                x1, y1, x2, y2 = boxes[i]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, f"ID {pid}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                break

    # Affichage HUD
    h, w = frame.shape[:2]
    cv2.putText(frame, f"Personnes: {len(persons)}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

    # Message de statut temporaire (5 secondes)
    if status_message and current_time - status_time < 5:
        cv2.putText(frame, status_message, (10, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

    # Raccourcis en bas
    cv2.putText(frame, "q:quit  h:health  s:stop  r:restart  1:HD  2:STD  3:ECO",
                (10, h - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

    cv2.imshow("Parc", frame)

    # Gestion des raccourcis clavier
    key = cv2.waitKey(1) & 0xFF

    if key == ord("q"):
        break
    elif key == ord("h"):
        result = camera_health()
        if result:
            status_message = f"Sante: {result.get('status')}"
            status_time = time.time()
    elif key == ord("s"):
        camera_control("stop")
        status_message = "Stream arrete"
        status_time = time.time()
    elif key == ord("r"):
        camera_control("restart")
        status_message = "Stream redemarre"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("1"):
        camera_config("haute")
        status_message = "Config: haute qualite"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("2"):
        camera_config("standard")
        status_message = "Config: standard"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)
    elif key == ord("3"):
        camera_config("economie")
        status_message = "Config: economie"
        status_time = time.time()
        cap.release()
        time.sleep(3)
        cap = cv2.VideoCapture(STREAM_URL)

    frame_count += 1
    if frame_count % 100 == 0:
        print(f"Frame {frame_count} | Personnes: {len(persons)}")

cap.release()
cur.close()
conn.close()
cv2.destroyAllWindows()
print("Pipeline arrêté")