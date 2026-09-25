"""Client minimal de registre d'images (API « distribution » de l'OCI).

Sert à savoir, SANS RIEN TÉLÉCHARGER, si une nouvelle version existe et
laquelle : on lit l'empreinte de ce que désigne l'étiquette, puis, seulement
si elle a changé, les étiquettes de la nouvelle image.

⚠️ POURQUOI NE PAS SIMPLEMENT FAIRE `docker pull` POUR VOIR ? Parce que ça
télécharge des centaines de mégaoctets pour répondre à une question qui tient
en quelques centaines d'octets. Et Docker Hub compte chaque manifeste lu en GET
dans son quota de téléchargements ; une requête HEAD, elle, n'est pas comptée.
"""

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import versions
from .image import DOCKER_HUB, Reference

journal = logging.getLogger(__name__)

# Les quatre formats de manifeste en circulation. On les accepte tous : un
# registre répond avec celui qu'il a, et répond 404 si on n'en demande aucun
# qu'il connaisse.
TYPES_MANIFESTE = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
TYPES_INDEX = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
REDIRECTIONS = (301, 302, 303, 307, 308)


class ErreurRegistre(Exception):
    """Le registre est injoignable, ou a répondu autre chose que prévu."""


class _PasDeRedirectionAutomatique(urllib.request.HTTPRedirectHandler):
    """⚠️ ON SUIT LES REDIRECTIONS NOUS-MÊMES. lscr.io renvoie vers ghcr.io, et
    ghcr.io renvoie les fichiers vers un stockage signé sur un autre domaine.
    Suivies automatiquement, soit le jeton d'accès se perd en route, soit il part
    vers un domaine qui n'a pas à le voir. À la main, chaque saut reçoit
    exactement l'autorisation qui lui revient."""

    def redirect_request(self, *args, **kwargs):
        return None


class Registre:
    def __init__(self, identifiants=None, delai=20):
        # identifiants : {"ghcr.io": ("utilisateur", "jeton")}, facultatif.
        # Sans eux, les images publiques restent lisibles, mais les limites de
        # débit anonymes sont plus basses.
        self.identifiants = identifiants or {}
        self.delai = delai
        self._jetons = {}
        self._ouvreur = urllib.request.build_opener(_PasDeRedirectionAutomatique)

    # ======================================================== ce qu'on demande
    def empreinte(self, ref):
        """Empreinte (« sha256:… ») de ce que l'étiquette désigne aujourd'hui."""
        url = f"https://{ref.registre}/v2/{ref.depot}/manifests/{ref.etiquette}"
        _, entetes, _ = self._demander(url, "HEAD", ref, TYPES_MANIFESTE)
        empreinte = entetes.get("Docker-Content-Digest")
        if not empreinte:
            raise ErreurRegistre(f"{ref} : le registre n'a pas donné d'empreinte")
        return empreinte

    def configuration(self, ref, empreinte, plateforme):
        """Étiquettes et variables d'environnement d'une image, sans la télécharger.

        Un manifeste « index » regroupe une image par architecture : on choisit
        celle du NAS (par exemple linux/amd64), puis on lit son petit fichier
        de configuration JSON, qui contient les étiquettes.
        """
        manifeste = self._json(ref, f"manifests/{empreinte}", TYPES_MANIFESTE)
        if manifeste.get("mediaType") in TYPES_INDEX or "manifests" in manifeste:
            systeme, architecture = plateforme.split("/", 1)
            choix = [m for m in manifeste.get("manifests", [])
                     if m.get("platform", {}).get("os") == systeme
                     and m.get("platform", {}).get("architecture") == architecture]
            if not choix:
                raise ErreurRegistre(f"{ref} : aucune image pour {plateforme}")
            manifeste = self._json(ref, f"manifests/{choix[0]['digest']}", TYPES_MANIFESTE)
        config = self._json(ref, f"blobs/{manifeste['config']['digest']}", "*/*")
        contenu = config.get("config") or {}
        return contenu.get("Labels") or {}, contenu.get("Env") or []

    def version_par_etiquettes(self, ref, empreinte, essais=10):
        """La version d'une image qui n'en déclare aucune, retrouvée par ses autres noms.

        Un éditeur publie la même image sous plusieurs étiquettes : « 2 », « 2.5 »,
        « 2.5.5 ». Celle qui a la même empreinte et le plus de chiffres donne la
        version. Renvoie None si aucune ne correspond.

        - Docker Hub : UNE requête à son API donne les 100 étiquettes les plus
          récentes avec leur empreinte.
        - Ailleurs (ghcr.io…), la liste ne donne que les noms : on demande
          l'empreinte des `essais` plus hautes versions, une par une, en HEAD.
        """
        if ref.registre == DOCKER_HUB:
            espace, _, depot = ref.depot.partition("/")
            page = self._hub(f"https://hub.docker.com/v2/namespaces/{espace}/repositories/"
                             f"{depot}/tags?page_size=100", ref)
            return versions.la_plus_precise(
                [t["name"] for t in page.get("results") or [] if t.get("digest") == empreinte])

        noms = self._json(ref, "tags/list?n=10000", "application/json").get("tags") or []
        # Les plus hautes d'abord ; à version égale, « 3.4.0 » avant « postgresql-3.4.0 »
        candidats = sorted((n for n in noms if versions.de_etiquette(n)), reverse=True,
                           key=lambda n: (versions.nombres(versions.de_etiquette(n)),
                                          n == versions.de_etiquette(n)))
        for nom in candidats[:essais]:
            if self.empreinte(Reference(ref.registre, ref.depot, nom)) == empreinte:
                return versions.de_etiquette(nom)
        return None

    def _hub(self, url, ref):
        """Une page de l'API de Docker Hub (distincte du registre, et sans jeton)."""
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"Accept": "application/json"}), timeout=self.delai) as reponse:
                return json.loads(reponse.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as erreur:
            raise ErreurRegistre(f"{ref} : API de Docker Hub injoignable ({erreur})") from None

    # ============================================================== mécanique
    def _json(self, ref, chemin, accept):
        url = f"https://{ref.registre}/v2/{ref.depot}/{chemin}"
        _, _, corps = self._demander(url, "GET", ref, accept)
        try:
            return json.loads(corps)
        except ValueError:
            raise ErreurRegistre(f"{ref} : réponse illisible pour {chemin}") from None

    def _demander(self, url, methode, ref, accept, sauts=8):
        """Une requête, avec authentification et redirections gérées à la main.

        Le premier essai part sans jeton. Le registre répond 401 et dit où en
        demander un : on le demande, puis on recommence. Si le jeton gardé en
        mémoire a expiré entre-temps, on en redemande un neuf, une seule fois.
        """
        jeton, essais_auth = None, 0
        for _ in range(sauts):
            requete = urllib.request.Request(url, method=methode, headers={"Accept": accept})
            if jeton:
                requete.add_unredirected_header("Authorization", f"Bearer {jeton}")
            try:
                with self._ouvreur.open(requete, timeout=self.delai) as reponse:
                    return reponse.status, reponse.headers, reponse.read()
            except urllib.error.HTTPError as erreur:
                if erreur.code in REDIRECTIONS and erreur.headers.get("Location"):
                    url = urllib.parse.urljoin(url, erreur.headers["Location"])
                    jeton, essais_auth = None, 0      # nouveau domaine : on repart de zéro
                    continue
                if erreur.code == 401 and essais_auth < 2:
                    defi = erreur.headers.get("WWW-Authenticate", "")
                    jeton = self._jeton(defi, ref, neuf=essais_auth > 0)
                    essais_auth += 1
                    if jeton:
                        continue
                raise ErreurRegistre(f"{ref} : HTTP {erreur.code} sur {url}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as erreur:
                raise ErreurRegistre(f"{ref} : injoignable ({erreur})") from None
        raise ErreurRegistre(f"{ref} : trop de redirections")

    def _jeton(self, defi, ref, neuf=False):
        """Obtient un jeton d'accès à partir du défi « WWW-Authenticate ».

        C'est le registre qui dit lui-même où demander le jeton (realm), pour
        quel service et avec quelle portée : aucune adresse n'est écrite en dur.
        """
        if not defi.lower().startswith("bearer "):
            return None
        parametres = dict(re.findall(r'(\w+)="([^"]*)"', defi))
        royaume = parametres.pop("realm", None)
        if not royaume:
            return None
        cle = (royaume, parametres.get("service"), parametres.get("scope"))
        if cle in self._jetons and not neuf:
            return self._jetons[cle]

        requete = urllib.request.Request(royaume + "?" + urllib.parse.urlencode(parametres))
        hote = urllib.parse.urlparse(royaume).hostname
        compte = self.identifiants.get(ref.registre) or self.identifiants.get(hote)
        if compte and compte[0] and compte[1]:
            paire = base64.b64encode(f"{compte[0]}:{compte[1]}".encode()).decode()
            requete.add_unredirected_header("Authorization", f"Basic {paire}")
        try:
            with urllib.request.urlopen(requete, timeout=self.delai) as reponse:
                donnees = json.loads(reponse.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as erreur:
            raise ErreurRegistre(f"{ref} : jeton refusé ({erreur})") from None
        jeton = donnees.get("token") or donnees.get("access_token")
        self._jetons[cle] = jeton
        return jeton
