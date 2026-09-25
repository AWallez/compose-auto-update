"""État persistant : ce que le programme sait de chaque conteneur entre deux passes.

Un seul fichier JSON, lisible par un humain et par le portail. Il ne contient
aucun secret.

Les deux sortes de manuel :
  - manuel PAR CHOIX : décidé dans la configuration (`mode = "manuel"`), il le
    reste quoi qu'il arrive ;
  - manuel TEMPORAIRE : un conteneur automatique qui a échoué. Il porte un
    « blocage » avec sa raison, et redevient automatique dès qu'une mise à jour
    manuelle réussit.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

FORMAT = 1
HISTORIQUE_MAX = 20

# Pour chaque raison de blocage, la marche à suivre affichée sur la carte.
CONSEILS = {
    "telechargement": "Le registre n'a pas répondu, rien n'a été installé. Relance la mise à "
                      "jour depuis la page ; si l'échec persiste, vérifie que l'image existe "
                      "toujours à cette adresse.",
    "installation": "La nouvelle version ne fonctionnait pas : l'ancienne version et ses données "
                    "ont été remises en place. Lis les notes de version avant de réessayer.",
    "majeure": "Une version majeure peut changer la configuration ou les données. Lis les notes "
               "de version, puis lance la mise à jour depuis la page quand tu es prêt.",
    "retour_arriere": "URGENT : le retour à l'ancienne version a lui aussi échoué, le service est "
                      "peut-être arrêté. Les données d'avant la mise à jour sont dans le dossier "
                      "des copies.",
}


def maintenant():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Etat:
    def __init__(self, chemin):
        self.chemin = Path(chemin)
        self.donnees = {"format": FORMAT, "derniere_passe": None, "conteneurs": {}}
        if self.chemin.exists():
            self.donnees = json.loads(self.chemin.read_text(encoding="utf-8"))

    def conteneur(self, nom):
        """Le suivi d'un conteneur, créé vide à la première rencontre."""
        return self.donnees["conteneurs"].setdefault(nom, {
            "mode": None,          # mode effectif lors de la dernière passe
            "mode_choisi": None,   # auto ou manuel choisi depuis la page ; prime sur la configuration
            "decouvert": False,    # absent de la configuration, trouvé sur la machine
            "avertissements": [],  # ce qui mérite correction, par exemple une image via lscr.io
            "version": None,       # version en service
            "empreinte": None,     # empreinte de l'image en service
            "disponible": None,    # {version, empreinte, vue_le} si une nouvelle existe
            "blocage": None,       # {raison, message, erreur, conseil, depuis}
            "notifie": [],         # empreintes déjà signalées par notification
            "historique": [],      # derniers événements
        })

    # ================================================================ blocages
    def blocage(self, nom):
        return self.conteneur(nom)["blocage"]

    def bloquer(self, nom, raison, message, erreur=""):
        self.conteneur(nom)["blocage"] = {
            "raison": raison, "message": message, "erreur": erreur,
            "conseil": CONSEILS[raison], "depuis": maintenant(),
        }
        self.noter(nom, "bloque", message)

    def debloquer(self, nom):
        if self.conteneur(nom)["blocage"]:
            self.conteneur(nom)["blocage"] = None
            self.noter(nom, "debloque", "repasse en automatique")

    # ================================================================== modes
    def mode_choisi(self, nom):
        return self.conteneur(nom).get("mode_choisi")

    def choisir_mode(self, nom, mode):
        """Mode choisi depuis la page. `None` rend la main à la configuration."""
        self.conteneur(nom)["mode_choisi"] = mode
        self.noter(nom, "mode", f"mode choisi : {mode or 'celui de la configuration'}")

    # =========================================================== notifications
    def deja_notifie(self, nom, empreinte):
        return empreinte in self.conteneur(nom)["notifie"]

    def marquer_notifie(self, nom, empreinte):
        notifie = self.conteneur(nom)["notifie"]
        if empreinte not in notifie:
            notifie.append(empreinte)
        del notifie[:-HISTORIQUE_MAX]      # seules les plus récentes servent encore

    # ================================================================ journal
    def noter(self, nom, evenement, detail=""):
        historique = self.conteneur(nom)["historique"]
        historique.append({"date": maintenant(), "evenement": evenement, "detail": detail})
        del historique[:-HISTORIQUE_MAX]

    # ================================================================ écriture
    def sauver(self, *copies):
        """Écrit l'état, et une copie par chemin donné (le portail, par exemple).

        ⚠️ ÉCRITURE ATOMIQUE : on écrit un fichier temporaire à côté, puis on le
        renomme. Un renommage est instantané et tout-ou-rien : le portail ne
        lira jamais un fichier à moitié écrit, et une coupure de courant en
        pleine écriture laisse l'ancien état intact.
        """
        texte = json.dumps(self.donnees, ensure_ascii=False, indent=2)
        for chemin in (self.chemin, *map(Path, copies)):
            chemin.parent.mkdir(parents=True, exist_ok=True)
            descripteur, temporaire = tempfile.mkstemp(dir=chemin.parent, prefix=".etat-")
            with os.fdopen(descripteur, "w", encoding="utf-8") as fichier:
                fichier.write(texte)
            os.chmod(temporaire, 0o644)
            os.replace(temporaire, chemin)
