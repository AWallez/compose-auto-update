"""Vérification qu'un conteneur fonctionne après sa mise à jour.

⚠️ « LE CONTENEUR TOURNE » NE SUFFIT PAS. Une application qui plante au
démarrage est relancée par Docker en boucle, et apparaît « running » une bonne
partie du temps. On observe donc sur la durée : toujours en marche, compteur de
redémarrages immobile, sonde de santé Docker au vert si l'image en a une, puis
réponse HTTP si la configuration donne une adresse.
"""

import logging
import time
import urllib.error
import urllib.request

journal = logging.getLogger(__name__)


def verifier(docker, nom, adresse=None, observation=60, delai_http=120,
             pas=5, attendre=time.sleep, horloge=time.monotonic):
    """Renvoie (True, "") si le conteneur est sain, sinon (False, "la raison")."""
    redemarrages = docker.conteneur(nom).redemarrages
    fin = horloge() + observation
    while True:
        c = docker.conteneur(nom)
        if c.etat != "running":
            return False, f"le conteneur est « {c.etat} » au lieu de tourner"
        if c.redemarrages > redemarrages:
            return False, f"le conteneur a redémarré {c.redemarrages - redemarrages} fois"
        if c.sante == "unhealthy":
            return False, "la sonde de santé Docker le déclare en mauvaise santé"
        if horloge() >= fin and c.sante in (None, "healthy"):
            break
        if horloge() >= fin + delai_http:
            return False, "la sonde de santé Docker n'est jamais passée au vert"
        attendre(pas)

    if adresse:
        return _repond(adresse, delai_http, pas, attendre, horloge)
    return True, ""


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # la redirection remonte alors en HTTPError : une réponse


def _repond(adresse, delai, pas, attendre, horloge):
    """Attend que l'application réponde en HTTP.

    ⚠️ N'IMPORTE QUEL CODE HTTP EST UNE BONNE RÉPONSE. Un 401 ou un 403 veut
    dire que l'application tourne et refuse l'accès sans mot de passe : c'est
    exactement ce qu'on veut savoir. Seule l'absence de réponse est un échec.

    ⚠️ LES REDIRECTIONS NE SONT PAS SUIVIES. Une redirection EST une réponse.
    La suivre testerait une autre adresse : un proxy comme Caddy renvoie vers
    https, sur un port et un nom de domaine que la vérification locale ne peut
    pas joindre, et un service sain serait déclaré mort.
    """
    ouvreur = urllib.request.build_opener(_SansRedirection)
    fin = horloge() + delai
    derniere = ""
    while horloge() < fin:
        try:
            with ouvreur.open(adresse, timeout=10):
                return True, ""
        except urllib.error.HTTPError as reponse:
            reponse.close()          # l'erreur porte la connexion : on la referme
            return True, ""
        except (urllib.error.URLError, TimeoutError, OSError) as erreur:
            derniere = str(erreur)
        attendre(pas)
    return False, f"aucune réponse sur {adresse} en {delai} s ({derniere})"
