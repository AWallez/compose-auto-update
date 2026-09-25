"""Notifications ntfy et signal de vie vers Uptime Kuma.

⚠️ UNE NOTIFICATION QUI ÉCHOUE NE FAIT JAMAIS ÉCHOUER LA PASSE. Les mises à
jour ont eu lieu, qu'on arrive à le dire ou non. L'échec est écrit au journal,
et le signal de vie sert de filet : si la passe ne s'est pas signalée, c'est
Uptime Kuma qui prévient.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

journal = logging.getLogger(__name__)


class Notificateur:
    def __init__(self, ntfy_url="", sujet="", jeton="", lien="", kuma_push="",
                 simulation=False, delai=15):
        self.ntfy_url = ntfy_url
        self.sujet = sujet
        self.jeton = jeton
        self.lien = lien
        # ⚠️ On ne garde que l'adresse de base de la sonde : si on la colle
        # avec ses paramètres d'exemple (?status=up&msg=OK), on les remplace.
        self.kuma_push = kuma_push.split("?", 1)[0]
        self.simulation = simulation
        self.delai = delai

    def envoyer(self, titre, message, priorite=3):
        """Envoie une notification. Renvoie True si ntfy l'a acceptée.

        ⚠️ ON PASSE PAR L'API JSON DE NTFY, PAS PAR LES EN-TÊTES HTTP. Les
        en-têtes n'acceptent que l'ASCII : un titre accentué y serait refusé ou
        déformé. Le JSON transporte l'UTF-8 sans difficulté.
        """
        if not self.ntfy_url:
            return False
        corps = {"topic": self.sujet, "title": titre, "message": message, "priority": priorite}
        if self.lien:
            corps["click"] = self.lien       # toucher la notification ouvre le portail
        if self.simulation:
            journal.info("simulation, notification non envoyée : %s\n%s", titre, message)
            return False
        requete = urllib.request.Request(self.ntfy_url, data=json.dumps(corps).encode(),
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
        if self.jeton:
            requete.add_header("Authorization", f"Bearer {self.jeton}")
        try:
            with urllib.request.urlopen(requete, timeout=self.delai) as reponse:
                return 200 <= reponse.status < 300
        except (urllib.error.URLError, TimeoutError, OSError) as erreur:
            journal.error("notification ntfy impossible : %s", erreur)
            return False

    def signal_de_vie(self, ok, resume):
        """Prévient Uptime Kuma que la passe a tourné.

        « up » veut dire : le programme a fonctionné, même si des conteneurs
        ont échoué (ces échecs-là ont leur propre notification). « down » est
        réservé au cas où le programme lui-même s'est arrêté en cours de route.
        """
        if not self.kuma_push:
            return
        if self.simulation:
            journal.info("simulation, signal de vie non envoyé : %s", resume)
            return
        parametres = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": resume[:200]})
        try:
            with urllib.request.urlopen(f"{self.kuma_push}?{parametres}", timeout=self.delai):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as erreur:
            journal.error("signal de vie vers Uptime Kuma impossible : %s", erreur)
