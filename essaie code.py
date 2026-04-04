import pandas as pd

# chemin du fichier
fichier = r"C:\Users\USER\Downloads\AffectationSol_-1856678471552617734.csv"

# on declare le chemin du fichier
df = pd.read_csv(fichier, sep=",")

# afficher
print(df.head())
print(df.columns)


# calcul surface par type
usage = df.groupby("TYPE")["SHAPE__Area"].sum()

print("\nSurface par type :")
print(usage)


# calcul du poids (%)
poids = (usage / usage.sum()) * 100

print("\nPoids (%) par type :")
print(poids)


# fonction de score usage du sol
def score_usage(type_sol):
    type_sol = str(type_sol).lower()
    
    if "parc" in type_sol or "espace public" in type_sol:
        return 3
    elif "résident" in type_sol or "mixte" in type_sol:
        return 2
    else:
        return 1

# appliquer le score
df["score_usage"] = df["TYPE"].apply(score_usage)

print("\nScore usage du sol :")
print(df[["TYPE", "score_usage"]].head())


# score final basé sur surface + usage
df["score_final"] = df["score_usage"] * df["SHAPE__Area"]

print("\nScore final :")
print(df[["TYPE", "score_usage", "score_final"]].head())

