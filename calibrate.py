"""
OUTIL DE CALIBRATION INTERACTIVE (Homographie pixel → GPS)
=============================================================

Ce script guide l'utilisateur pour créer une matrice d'homographie
qui transforme les coordonnées pixel de la caméra en coordonnées GPS (WGS84).

Processus :
1. Affiche un frame statique de la vidéo RTSP
2. L'utilisateur clique sur des points repères visibles dans l'image
3. Pour chaque clic, l'utilisateur entre manuellement les coordonnées GPS
4. Le script calcule la meilleure matrice d'homographie possible
5. La matrice est sauvegardée dans calibration.json pour utilisation par main.py

Théorie :
- Une homographie est une transformation projective qui mappe les coordonnées
  d'un plan (l'image caméra) vers un autre plan (le sol géoréférencé)
- Avec 4 points appariés (pixel, GPS), on peut calculer exactement la matrice 3x3
- Avec plus de 4 points, on utilise les moindres carrés pour minimiser l'erreur

Points repères recommandés :
- Au moins 4 points, bien répartis dans l'image (coins, centre, etc.)
- De préférence sur des éléments visibles et fixes (marquages au sol, coins de bâtiments)
- La précision GPS de ces points de référence détermine la précision du résultat final
"""

import cv2
import json
import numpy as np

# ============================================================================
# INITIALISATION
# ============================================================================

# URL du flux RTSP fourni par le serveur MediaMTX
VIDEO_PATH = "rtsp://camerapi1.local:8554/parc"

# Ouvre le flux et lit un seul frame pour l'afficher en image fixe
cap = cv2.VideoCapture(VIDEO_PATH)
ret, frame = cap.read()
cap.release()  # Ferme immédiatement le flux — on n'a besoin que d'une image

if not ret:
    # Si la lecture échoue, arrête le script
    print("Impossible de lire la vidéo")
    exit()

# Listes pour stocker les points de calibration saisis par l'utilisateur
pixel_points = []  # Liste de [x, y] en coordonnées image (pixels)
gps_points = []    # Liste de [latitude, longitude] en WGS84


# ============================================================================
# GESTION DES CLICS SOURIS
# ============================================================================

def on_click(event, x, y, flags, param):
    """
    Callback OpenCV invoqué chaque fois que l'utilisateur interagit avec la souris
    sur la fenêtre d'affichage.

    Paramètres :
        event : type d'événement souris (cv2.EVENT_LBUTTONDOWN, etc.)
        x, y : coordonnées du curseur dans l'image (en pixels)
        flags : état des modifieurs (Shift, Ctrl, Alt) — non utilisé ici
        param : paramètre optionnel passé lors de setMouseCallback — non utilisé ici

    Comportement :
        - Attend un clic gauche (EVENT_LBUTTONDOWN)
        - Enregistre le point pixel cliqué dans pixel_points
        - Affiche le point et son numéro sur l'image
        - Invite l'utilisateur à saisir les coordonnées GPS dans le terminal
    """
    if event == cv2.EVENT_LBUTTONDOWN:
        # Enregistre le point pixel cliqué
        pixel_points.append([x, y])
        num = len(pixel_points)

        # Dessine une petite marque circulaire au point cliqué
        # Paramètres : image, centre, rayon, couleur BGR, épaisseur (-1 = rempli)
        cv2.circle(frame, (x, y), 6, (0, 255, 0), -1)

        # Affiche le numéro du point près du clic (décalé de +10, -10 pour la lisibilité)
        cv2.putText(frame, str(num), (x + 10, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # Met à jour l'affichage de la fenêtre pour montrer le nouveau point
        cv2.imshow("Calibration", frame)

        # Affiche les informations dans le terminal et invite à saisir le GPS
        print(f"Point {num}: pixel ({x}, {y})")
        print(f"  Entre les coordonnées GPS (lat, lon): ", end="")


# Crée une fenêtre OpenCV nommée "Calibration"
cv2.namedWindow("Calibration")

# Registre le callback de souris : appelle on_click() pour chaque événement souris
cv2.setMouseCallback("Calibration", on_click)

# Affiche les instructions pour l'utilisateur dans le terminal
print("=== Calibration ===")
print("Clique sur 4+ points repères dans l'image.")
print("Après chaque clic, entre les coordonnées GPS dans le terminal.")
print("Appuie sur 'q' quand tu as fini.")
print("")

# Affiche le frame initial dans la fenêtre
cv2.imshow("Calibration", frame)

# ============================================================================
# BOUCLE DE SAISIE DES COORDONNÉES GPS
# ============================================================================

while True:
    # Attend un événement clavier (timeout de 100 ms, puis reprend la boucle)
    key = cv2.waitKey(100) & 0xFF

    # Vérifie s'il y a un nouveau point pixel sans coordonnées GPS associées
    # (l'utilisateur vient de cliquer sur un nouveau point)
    if len(pixel_points) > len(gps_points):
        # Attend l'entrée de l'utilisateur : les coordonnées GPS
        coords = input()

        # Permet à l'utilisateur de quitter en tapant "q"
        if coords.strip().lower() == "q":
            break

        try:
            # Parse les coordonnées : attend le format "latitude, longitude"
            # Exemple : "45.123456, -73.654321"
            lat, lon = [float(c.strip()) for c in coords.split(",")]
            # Enregistre le point GPS correspondant au dernier clic
            gps_points.append([lat, lon])

            # Confirmation à l'utilisateur
            print(f"  GPS: ({lat}, {lon})")
            print("")

            # Affiche un message encourageant si au moins 4 points sont collectés
            if len(pixel_points) >= 4:
                print(f"({len(pixel_points)} points) Clique un autre point ou tape 'q' pour terminer.")
        except ValueError:
            # Format incorrect : affiche un exemple et annule le dernier clic
            print("  Format invalide. Utilise: 45.123456, -73.654321")
            pixel_points.pop()  # Supprime le dernier pixel_point orphelin

    # Gestion du clavier : permet de quitter avec 'q'
    if key == ord("q") and len(pixel_points) >= 4:
        # Quitter si au moins 4 points sont collectés (minimum requis pour une homographie)
        break
    elif key == ord("q") and len(pixel_points) < 4:
        # Affiche un message d'erreur si pas assez de points
        print("Il faut au moins 4 points!")

# Ferme toutes les fenêtres OpenCV
cv2.destroyAllWindows()

# ============================================================================
# CALCUL DE LA MATRICE D'HOMOGRAPHIE
# ============================================================================

# Convertit les listes Python en arrays NumPy (format attendu par OpenCV)
# dtype=np.float32 pour la compatibilité avec les fonctions OpenCV
src = np.array(pixel_points, dtype=np.float32)  # Points source (pixels)
dst = np.array(gps_points, dtype=np.float32)    # Points destination (GPS)

if len(pixel_points) == 4:
    # CAS EXACT : exactement 4 points
    # getPerspectiveTransform calcule la matrice homographie exacte
    # (il existe une solution unique avec exactement 4 points appariés)
    matrix = cv2.getPerspectiveTransform(src, dst)
else:
    # CAS SURDÉTERMINÉ : plus de 4 points
    # findHomography utilise les moindres carrés pour trouver la meilleure transformation
    # Cela réduit l'influence des imprécisions dans les coordonnées GPS ou les clics
    # _ ignore la liste des inliers/outliers (non nécessaire ici)
    matrix, _ = cv2.findHomography(src, dst)

# ============================================================================
# SAUVEGARDE DE LA CALIBRATION
# ============================================================================

# Prépare un dictionnaire avec toutes les informations de calibration
calibration = {
    "pixel_points": pixel_points,   # Points cliqués dans l'image
    "gps_points": gps_points,       # Coordonnées GPS correspondantes
    "matrix": matrix.tolist(),      # Matrice d'homographie (3x3) convertie en liste JSON-sérialisable
}

# Sauvegarde le dictionnaire dans un fichier JSON (relu par main.py au démarrage)
with open("calibration.json", "w") as f:
    json.dump(calibration, f, indent=2)

# Affiche un message de confirmation
print(f"\nCalibration sauvegardée dans calibration.json ({len(pixel_points)} points)")

# ============================================================================
# TEST DE VALIDATION
# ============================================================================
# Applique la transformation à un point de test (le centre de l'image)
# pour vérifier que la matrice retourne des coordonnées GPS plausibles

h, w = frame.shape[:2]  # Hauteur et largeur du frame

# Centre de l'image en coordonnées pixels
# cv2.perspectiveTransform attend un array de forme (N, 1, 2)
test_point = np.array([[[w/2, h/2]]], dtype=np.float64)

# Applique la transformation homographique
result = cv2.perspectiveTransform(test_point, matrix)

# Affiche les résultats pour validation visuelle
print(f"Test: centre de l'image ({w/2}, {h/2}) -> GPS ({result[0][0][0]:.6f}, {result[0][0][1]:.6f})")