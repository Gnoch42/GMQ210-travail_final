import cv2
import json
import numpy as np

# Charger un frame de ta vidéo
VIDEO_PATH = "rtsp://camerapi1.local:8554/parc"
cap = cv2.VideoCapture(VIDEO_PATH)
ret, frame = cap.read()
cap.release()

if not ret:
    print("Impossible de lire la vidéo")
    exit()

# Points de calibration
pixel_points = []
gps_points = []

def on_click(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        pixel_points.append([x, y])
        num = len(pixel_points)
        cv2.circle(frame, (x, y), 6, (0, 255, 0), -1)
        cv2.putText(frame, str(num), (x + 10, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow("Calibration", frame)
        print(f"Point {num}: pixel ({x}, {y})")
        print(f"  Entre les coordonnées GPS (lat, lon): ", end="")

cv2.namedWindow("Calibration")
cv2.setMouseCallback("Calibration", on_click)

print("=== Calibration ===")
print("Clique sur 4+ points repères dans l'image.")
print("Après chaque clic, entre les coordonnées GPS dans le terminal.")
print("Appuie sur 'q' quand tu as fini.")
print("")

cv2.imshow("Calibration", frame)

while True:
    key = cv2.waitKey(100) & 0xFF

    if len(pixel_points) > len(gps_points):
        coords = input()
        if coords.strip().lower() == "q":
            break
        try:
            lat, lon = [float(c.strip()) for c in coords.split(",")]
            gps_points.append([lat, lon])
            print(f"  GPS: ({lat}, {lon})")
            print("")
            if len(pixel_points) >= 4:
                print(f"({len(pixel_points)} points) Clique un autre point ou tape 'q' pour terminer.")
        except ValueError:
            print("  Format invalide. Utilise: 45.123456, -73.654321")
            pixel_points.pop()

    if key == ord("q") and len(pixel_points) >= 4:
        break
    elif key == ord("q") and len(pixel_points) < 4:
        print("Il faut au moins 4 points!")

cv2.destroyAllWindows()

# Calculer la matrice d'homographie
src = np.array(pixel_points, dtype=np.float32)
dst = np.array(gps_points, dtype=np.float32)

if len(pixel_points) == 4:
    matrix = cv2.getPerspectiveTransform(src, dst)
else:
    matrix, _ = cv2.findHomography(src, dst)

calibration = {
    "pixel_points": pixel_points,
    "gps_points": gps_points,
    "matrix": matrix.tolist(),
}

with open("calibration.json", "w") as f:
    json.dump(calibration, f, indent=2)

print(f"\nCalibration sauvegardée dans calibration.json ({len(pixel_points)} points)")

# Test: afficher la conversion du centre de l'image
h, w = frame.shape[:2]
test_point = np.array([[[w/2, h/2]]], dtype=np.float64)
result = cv2.perspectiveTransform(test_point, matrix)
print(f"Test: centre de l'image ({w/2}, {h/2}) -> GPS ({result[0][0][0]:.6f}, {result[0][0][1]:.6f})")