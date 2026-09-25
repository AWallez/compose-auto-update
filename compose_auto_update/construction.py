"""Images construites sur place : leurs images de base, et le code dont elles sortent.

Une image construite sur la machine n'a pas de registre qui annoncerait une
nouvelle version. Ce qui vieillit, ce sont ses images de BASE (nginx, node,
caddy…) : c'est donc elles qu'on surveille, et l'image est reconstruite quand
l'une d'elles reçoit un correctif.

⚠️ RECONSTRUIRE AVEC LE MÊME CODE, JAMAIS AVEC UN AUTRE. Le dépôt présent sur
la machine peut contenir des commits que personne n'a encore déployés. Les
construire serait mettre en production sans le vouloir. On vérifie donc que le
dépôt pointe toujours sur le commit inscrit dans l'image qui tourne, et que le
Dockerfile n'a pas changé depuis la construction de cette image.
"""

import hashlib
import re
from pathlib import Path

REVISION = "org.opencontainers.image.revision"   # étiquette OCI standard : le commit d'origine


class ErreurConstruction(Exception):
    """Dockerfile ou dépôt illisible : on ne sait plus comment l'image a été construite."""

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
