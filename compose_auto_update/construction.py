"""Images construites sur place : leurs images de base, et le code dont elles sortent.

Une image construite sur la machine n'a pas de registre qui annoncerait une
nouvelle version. Ce qui vieillit, ce sont ses images de BASE (nginx, node,
caddy…) : c'est donc elles qu'on surveille, et l'image est reconstruite quand
l'une d'elles reçoit un correctif.

⚠️ RECONSTRUIRE AVEC LE MÊME CODE, JAMAIS AVEC UN AUTRE. Le dépôt présent sur
la machine peut contenir des commits que personne n'a encore déployés. Les
construire serait mettre en production sans le vouloir. On vérifie donc que
ces commits, s'il y en a, ne touchent pas ce qui sert à construire l'image, et
que le Dockerfile n'a pas changé depuis la construction de l'image qui tourne.
"""

import hashlib
import re
import shutil
from pathlib import Path

from .commande import executer

REVISION = "org.opencontainers.image.revision"   # étiquette OCI standard : le commit d'origine


class ErreurConstruction(Exception):
    """Dockerfile ou dépôt illisible, ou image reconstruite qui a perdu son commit."""


_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)


def bases(dockerfile):
    """Les images de base d'un Dockerfile, dans l'ordre, sans doublon.

    ⚠️ « FROM build » peut désigner une étape précédente (« AS build ») et non
    une image : on écarte les noms d'étapes déjà vus. « scratch » n'est pas une
    image non plus.
    ⚠️ Une base écrite avec une variable (« FROM node:${VERSION} ») n'est pas
    devinée : elle est ignorée, et `inconnues()` permet de la signaler.
    """
    etapes, trouvees = set(), []
    for image, alias in _FROM.findall(dockerfile):
        if image.lower() != "scratch" and image not in etapes and "$" not in image \
                and image not in trouvees:
            trouvees.append(image)
        if alias:
            etapes.add(alias)
    return trouvees


def inconnues(dockerfile):
    """Les bases écrites avec une variable, qu'on ne sait pas surveiller."""
    return [image for image, _ in _FROM.findall(dockerfile) if "$" in image]


def empreinte_texte(texte):
    """Empreinte d'un Dockerfile : sert à voir s'il a changé depuis la construction."""
    return "sha256:" + hashlib.sha256(texte.encode("utf-8")).hexdigest()


def nom_court(reference):
    """« nginx:stable-alpine » donne « nginx », « ghcr.io/x/caddy:2 » donne « caddy »."""
    return reference.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]


def version_de_base(depot, etiquettes, environnement):
    """Version d'une image de base.

    Les images officielles la donnent dans une variable nommée d'après le
    logiciel : NGINX_VERSION, NODE_VERSION, CADDY_VERSION. À défaut, l'étiquette
    OCI de version.
    """
    nom = depot.rsplit("/", 1)[-1].upper().replace("-", "_")
    for ligne in environnement:
        cle, _, valeur = ligne.partition("=")
        if cle == f"{nom}_VERSION" and valeur:
            return valeur
    return etiquettes.get("org.opencontainers.image.version") or None


def commit_du_depot(depot):
    """Le commit sur lequel pointe un dépôt git, lu dans `.git` sans avoir besoin de git.

    ⚠️ Certaines machines n'ont pas git (c'est le cas de UGOS) : on lit HEAD,
    puis la référence qu'il désigne, dans son fichier ou dans packed-refs.
    """
    git = Path(depot) / ".git"
    tete = (git / "HEAD").read_text(encoding="utf-8").strip()
    if not tete.startswith("ref: "):
        return tete                       # HEAD détachée : c'est déjà un commit
    reference = tete[5:]
    fichier = git / reference
    if fichier.exists():
        return fichier.read_text(encoding="utf-8").strip()
    tasse = git / "packed-refs"
    if tasse.exists():
        for ligne in tasse.read_text(encoding="utf-8").splitlines():
            if ligne.endswith(" " + reference):
                return ligne.split()[0]
    return None


def est_un_commit(texte):
    """Vrai pour un identifiant de commit git : 7 à 40 caractères hexadécimaux.

    ⚠️ « unknown », la valeur par défaut d'une image construite à la main, n'en
    est pas un : impossible alors de savoir de quel code elle sort.
    """
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", texte or ""))


_ETIQUETTE_REVISION = re.compile(
    r"org\.opencontainers\.image\.revision\s*=\s*[\"']?\$\{?(\w+)")


def argument_de_revision(dockerfile):
    """Nom de l'ARG qui inscrit le commit dans l'image, ou None.

    « LABEL org.opencontainers.image.revision=$GIT_SHA » donne « GIT_SHA » :
    c'est la variable à renseigner pour que l'image reconstruite garde son commit.
    """
    trouve = _ETIQUETTE_REVISION.search(dockerfile)
    return trouve.group(1) if trouve else None


def depot_de(dossier):
    """Le dépôt git qui contient ce dossier, en remontant vers la racine, ou None."""
    for candidat in (Path(dossier), *Path(dossier).parents):
        if (candidat / ".git" / "HEAD").is_file():
            return str(candidat)
    return None


def commits_touchant(depot, depuis, jusqu_a, chemins):
    """Combien de commits, entre `depuis` et `jusqu_a`, touchent ces chemins.

    C'est la question qui compte : un commit qui ne modifie que le site ne
    change rien à ce qui construit l'api. Zéro veut dire que reconstruire
    maintenant donne exactement le code de l'image en service.

    ⚠️ Le git de la machine s'il existe ; sinon celui d'un conteneur jetable
    (image alpine/git, déjà présente), sans réseau et avec le dépôt en lecture
    seule. UGOS n'a pas git.
    """
    if shutil.which("git"):
        commande, racine = ["git"], depot
    else:
        commande = ["docker", "run", "--rm", "--network", "none", "--pull", "never",
                    "--volume", f"{depot}:/depot:ro", "alpine/git"]
        racine = "/depot"
    # safe.directory : le dépôt appartient à un autre utilisateur que root
    sortie = executer([*commande, "-c", f"safe.directory={racine}", "-C", racine,
                       "rev-list", "--count", f"{depuis}..{jusqu_a}", "--", *chemins], delai=120)
    return int(sortie.strip())
