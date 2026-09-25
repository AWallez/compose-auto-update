"""Lecture des numéros de version et détection des montées majeures.

⚠️ « latest » NE DIT RIEN DE LA VERSION. Le numéro vit dans les étiquettes de
l'image, et chaque éditeur le range à sa façon :
  - la norme OCI    : org.opencontainers.image.version = « v3.4.1 »
  - LinuxServer     : build_version = « Linuxserver.io version:- 2.6.5.5623-ls161 Build-date:- … »
  - nginx           : une variable d'environnement, NGINX_VERSION=1.31.6
  - rien du tout    : uptime-kuma, ntfy… La version ne se retrouve alors que
                      dans le NOM d'une autre étiquette de la même image (« 2.5.5 »),
                      voir registre.version_par_etiquettes().
"""

import re

_LINUXSERVER = re.compile(r"version:-\s*(\S+)")

# Images officielles dont la variable ne suit pas « <LOGICIEL>_VERSION »
_ALIAS = {"POSTGRES": "PG"}


def par_nom(depot, environnement):
    """La variable de version qui porte le nom du logiciel de l'image elle-même.

    nginx → NGINX_VERSION, caddy → CADDY_VERSION, postgres → PG_VERSION.
    ⚠️ Le nom doit correspondre : NODE_VERSION dans l'image d'uptime-kuma est la
    version de Node.js, qui sert à la faire tourner, pas la sienne.
    """
    nom = depot.rsplit("/", 1)[-1].upper().replace("-", "_")
    nom = _ALIAS.get(nom, nom)
    for ligne in environnement:
        cle, _, valeur = ligne.partition("=")
        if cle == f"{nom}_VERSION" and valeur:
            return valeur
    return None


def lire(etiquettes, environnement, regle="", depot=""):
    """Numéro de version d'une image, ou None s'il est introuvable.

    `regle` vient de la configuration, pour les éditeurs qui rangent leur
    version ailleurs : « label:nom.de.l.etiquette » ou « env:NOM_DE_VARIABLE ».

    ⚠️ AUCUNE DEVINETTE SUR LES VARIABLES *_VERSION. Uptime Kuma expose
    NODE_VERSION : c'est la version de Node.js, pas celle de l'application.
    La prendre pour une version d'application ferait croire à des montées
    majeures qui n'existent pas. Seule exception, celle qui porte le nom du
    logiciel de l'image (`depot`), voir par_nom().
    """
    if regle:
        genre, _, nom = regle.partition(":")
        if genre == "label":
            return etiquettes.get(nom) or None
        if genre == "env":
            for ligne in environnement:
                cle, _, valeur = ligne.partition("=")
                if cle == nom:
                    return valeur or None
            return None
        raise ValueError(f"règle de version inconnue : {regle!r}")

    if etiquettes.get("org.opencontainers.image.version"):
        return etiquettes["org.opencontainers.image.version"]
    trouve = _LINUXSERVER.search(etiquettes.get("build_version", ""))
    if trouve:
        return trouve.group(1)
    return par_nom(depot, environnement) if depot else None


# Une version dans une étiquette : au début, ou après un tiret (« postgresql-3.4.0 »),
# et suivie de la fin ou d'un tiret (« 17.11-alpine »). « nightly3 » n'en contient pas.
_DANS_ETIQUETTE = re.compile(r"(?:^|[-_])(v?\d+(?:\.\d+)*)(?=$|[-_])")


def de_etiquette(etiquette):
    """La version contenue dans une étiquette d'image, ou None.

    « 2.5.5 », « v2.28.0 », « 17.11-alpine » → « 17.11 », « latest » → None.
    """
    trouve = _DANS_ETIQUETTE.search(etiquette)
    return trouve.group(1) if trouve else None


def nombres(version):
    """« v2.28.0 » donne (2, 28, 0) : pour trier et comparer des versions."""
    return tuple(int(n) for n in version.lstrip("v").split("."))


def la_plus_precise(etiquettes):
    """Parmi des étiquettes d'une MÊME image, la version la plus précise.

    L'image d'uptime-kuma s'appelle à la fois « 2 », « 2.5.5 » et « next » :
    c'est « 2.5.5 » qui dit quelle version tourne.
    """
    trouvees = [v for v in map(de_etiquette, etiquettes) if v]
    return max(trouvees, key=lambda v: len(nombres(v)), default=None)


def majeure(version):
    """Premier nombre d'un numéro de version : « v1.6.0-ls354 » donne 1."""
    if not version:
        return None
    trouve = re.match(r"\D*?(\d+)", version)
    return int(trouve.group(1)) if trouve else None


def prefixe(version, segments=1):
    """Les `segments` premiers nombres d'une version, pour comparer ce qui compte.

    « 1.30.5 » donne (1,) avec 1 segment et (1, 30) avec 2 : c'est ce second
    découpage qui fait d'un passage de nginx 1.30 à 1.32 une mise à jour
    importante, alors que 1.30.5 → 1.30.6 ne l'est pas.
    None si la version est illisible ou trop courte : on ne peut pas conclure.
    """
    if not version:
        return None
    trouve = re.match(r"\D*?(\d+(?:\.\d+)*)", version)
    if not trouve:
        return None
    nombres = [int(n) for n in trouve.group(1).split(".")]
    return tuple(nombres[:segments]) if len(nombres) >= segments else None


def etiquette_fige_majeure(etiquette):
    """Vrai si l'étiquette elle-même rend toute montée majeure impossible.

    « 2 », « 17-alpine », « v3.41.3 » : l'éditeur ne publiera jamais une
    version 3 ou 18 sous ce nom. Inutile de lire les versions, le changement de
    majeure ne peut pas arriver par là.
    """
    return re.match(r"v?\d", etiquette) is not None
