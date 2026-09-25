"""Découpage d'une référence d'image : registre, dépôt, étiquette.

« nginx:alpine » est une abréviation. Pour interroger un registre, il faut la
forme complète : hôte registry-1.docker.io, dépôt library/nginx, étiquette alpine.
"""

from dataclasses import dataclass

DOCKER_HUB = "registry-1.docker.io"


@dataclass(frozen=True)
class Reference:
    registre: str     # hôte à interroger, ex. ghcr.io
    depot: str        # ex. linuxserver/radarr
    etiquette: str    # ex. latest

    def __str__(self):
        return f"{self.registre}/{self.depot}:{self.etiquette}"


def _separer(texte):
    """Sépare « nom:étiquette ». Sans étiquette, Docker sous-entend « latest ».

    ⚠️ Un deux-points n'annonce une étiquette que s'il est dans le DERNIER
    segment : dans « registre:5000/image », c'est un numéro de port.
    """
    texte = texte.split("@", 1)[0]
    if ":" in texte.rsplit("/", 1)[-1]:
        nom, etiquette = texte.rsplit(":", 1)
        return nom, etiquette
    return texte, "latest"


def analyser(texte):
    """Transforme une référence abrégée en Reference complète.

    ⚠️ C'est la règle de Docker, pas une invention : le premier segment n'est un
    registre QUE s'il contient un point ou un deux-points, ou vaut « localhost ».
    Sinon c'est un compte Docker Hub, comme dans « louislam/uptime-kuma ».
    """
    nom, etiquette = _separer(texte)
    premier, _, suite = nom.partition("/")
    if suite and ("." in premier or ":" in premier or premier == "localhost"):
        registre, depot = premier, suite
    else:
        registre, depot = DOCKER_HUB, nom
    if registre in ("docker.io", "index.docker.io"):
        registre = DOCKER_HUB
    if registre == DOCKER_HUB and "/" not in depot:
        depot = "library/" + depot        # images officielles : « nginx » = « library/nginx »
    return Reference(registre, depot, etiquette)


def sans_etiquette(texte):
    """« lscr.io/linuxserver/radarr:latest » donne « lscr.io/linuxserver/radarr »."""
    return _separer(texte)[0]
