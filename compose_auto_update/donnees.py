"""Copie instantanée des données avant une mise à jour, et restauration.

⚠️ POURQUOI COPIER LES DONNÉES, ET PAS SEULEMENT GARDER L'ANCIENNE IMAGE.
Radarr, Sonarr, Vaultwarden ou Jellyfin convertissent leur base au premier
démarrage d'une nouvelle version. Remettre l'ancienne image devant une base
convertie ne marche pas : l'ancienne version ne sait plus la lire. Un vrai
retour arrière remet les deux.

⚠️ POURQUOI C'EST INSTANTANÉ. Sur btrfs, `cp --reflink=always` ne recopie
aucun octet : la copie partage les blocs de l'original, et seuls les blocs
modifiés ensuite prennent de la place. Et `always`, pas `auto` : si le système
de fichiers ne sait pas le faire (dossier des copies sur un autre disque, par
exemple), on préfère une erreur franche à une vraie copie de plusieurs
gigaoctets qui remplirait le disque en silence.

⚠️ SEULS LES DOSSIERS LISTÉS DANS LA CONFIGURATION SONT COPIÉS. Aucune règle
automatique du genre « tout ce que le conteneur écrit » : dockge monte tout
/volume1/docker, et sept conteneurs montent la médiathèque entière.
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .commande import executer

journal = logging.getLogger(__name__)


@dataclass
class Copie:
    dossier: Path
    paires: list = field(default_factory=list)     # [(original, copie)]


def copier(chemins, racine, nom, horodatage, simulation=False):
    """Copie chaque chemin dans racine/<nom>/<horodatage>/."""
    dossier = Path(racine) / nom / horodatage
    copie = Copie(dossier)
    for numero, chemin in enumerate(chemins):
        original = Path(chemin)
        if not original.exists():
            raise FileNotFoundError(f"donnée à copier introuvable : {original}")
        # un numéro devant le nom : deux dossiers « config » ne s'écrasent pas
        destination = dossier / f"{numero}-{original.name}"
        copie.paires.append((original, destination))
        if simulation:
            journal.info("simulation, non copié : %s", original)
            continue
        dossier.mkdir(parents=True, exist_ok=True)
        executer(["cp", "-a", "--reflink=always", str(original), str(destination)])
    return copie


def restaurer(copie, horodatage, simulation=False):
    """Remet les données copiées à leur place.

    ⚠️ RIEN N'EST SUPPRIMÉ. Les données abîmées sont renommées en
    « <dossier>.echec-<horodatage> », juste à côté : elles restent là pour
    comprendre ce qui s'est passé, et c'est toi qui décideras de les effacer.
    """
    for original, sauvegarde in copie.paires:
        if simulation:
            journal.info("simulation, non restauré : %s", original)
            continue
        if original.exists():
            os.rename(original, original.with_name(f"{original.name}.echec-{horodatage}"))
        executer(["cp", "-a", "--reflink=always", str(sauvegarde), str(original)])


def elaguer(racine, nom, garder, simulation=False):
    """Ne garde que les `garder` copies les plus récentes d'un conteneur."""
    dossier = Path(racine) / nom
    if not dossier.is_dir():
        return
    # l'horodatage AAAAMMJJ-HHMMSS se trie dans l'ordre chronologique
    copies = sorted(p for p in dossier.iterdir() if p.is_dir())
    trop_vieilles = copies[:-garder] if garder > 0 else copies
    for ancienne in trop_vieilles:
        if simulation:
            journal.info("simulation, non supprimé : %s", ancienne)
            continue
        shutil.rmtree(ancienne)
