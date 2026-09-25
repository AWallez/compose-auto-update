"""Découverte des conteneurs absents de la configuration.

Un conteneur ajouté plus tard sur la machine est pris en charge dès la passe
suivante, sans rien écrire nulle part : il est suivi dans le mode prévu pour
les nouveaux venus, et une notification dit ce que l'outil a décidé pour lui.
"""

from pathlib import PurePosixPath


def inconnus(conf, tous):
    """Les conteneurs qui tournent mais ne figurent pas dans la configuration.

    ⚠️ Les conteneurs éphémères (« docker run --rm ») sont ignorés : ce sont des
    outils ponctuels, qui auront disparu avant la passe suivante.
    """
    connus = {cc.nom for cc in conf.conteneurs} | set(conf.exclus)
    return [c for c in tous if c.nom not in connus and not c.ephemere]


def donnees_probables(c, tous, racines):
    """Les dossiers de données d'un conteneur découvert, choisis avec trois garde-fous.

    1. Un volume géré par Docker appartient à son application : il est retenu.
    2. Un dossier de la machine n'est retenu que s'il est DANS une racine
       déclarée, à deux niveaux au moins : « /volume1/docker/app/config » oui ;
       « /volume1/docker/app » non, c'est souvent le dossier d'une pile entière ;
       « /volume1/docker » non plus, dockge le monte en entier.
    3. Un dossier monté aussi par un AUTRE conteneur n'est jamais retenu : le
       restaurer ferait revenir l'autre en arrière avec lui. La médiathèque,
       montée par sept conteneurs, est écartée par cette seule règle.
    """
    partages = {}
    for autre in tous:
        for m in autre.montages:
            partages.setdefault(m.source, set()).add(autre.nom)

    retenus = []
    for m in c.montages:
        if not m.ecriture or partages.get(m.source, set()) - {c.nom}:
            continue
        if m.genre == "volume" or _assez_profond(m.source, racines):
            retenus.append(m.source)
    return retenus


def _assez_profond(chemin, racines):
    chemin = PurePosixPath(chemin)
    for racine in map(PurePosixPath, racines):
        if chemin.is_relative_to(racine) and len(chemin.relative_to(racine).parts) >= 2:
            return True
    return False
