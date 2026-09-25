"""Lecture de la configuration (TOML) et contrôle de sa cohérence.

⚠️ ON REFUSE DE DÉMARRER SUR UNE CONFIGURATION DOUTEUSE. Un nom de conteneur
en double, un mode inconnu, un chemin de données relatif : mieux vaut une
erreur claire au démarrage qu'une passe qui ignore un conteneur sans rien dire.
"""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

journal = logging.getLogger(__name__)

MODES = ("auto", "manuel")


class ErreurConfig(Exception):
    pass


@dataclass
class ConfConteneur:
    nom: str
    mode: str                                     # auto ou manuel
    donnees: list = field(default_factory=list)   # dossiers copiés avant chaque mise à jour
    sante: str = ""                               # adresse HTTP testée après la mise à jour
    version: str = ""                             # où lire la version, si pas au standard
    decouvert: bool = False                       # trouvé sur la machine, absent de la configuration
    recreable: bool = True                        # faux s'il n'a pas été créé par Docker Compose


@dataclass
class Conf:
    fichier_etat: str
    exports: list
    dossier_copies: str
    copies_conservees: int
    observation: int
    delai_sante: int
    attentes_reessai: list
    ntfy_url: str
    ntfy_sujet: str
    ntfy_jeton: str
    lien: str
    kuma_push: str
    identifiants: dict
    conteneurs: list
    apres_mise_a_jour: list = field(default_factory=list)   # commandes lancées après une mise à jour
    mode_decouverte: str = "auto"                  # mode des conteneurs découverts
    racines_donnees: list = field(default_factory=list)
    exclus: list = field(default_factory=list)     # jamais suivis, ni découverts


def _absolu(chemin, ou):
    # ⚠️ PurePosixPath : le programme tourne sous Linux, même si on le teste ailleurs
    if not PurePosixPath(chemin).is_absolute():
        raise ErreurConfig(f"{ou} : le chemin doit être absolu : {chemin!r}")
    return chemin


def charger(chemin):
    try:
        with open(chemin, "rb") as fichier:
            brut = tomllib.load(fichier)
    except FileNotFoundError:
        raise ErreurConfig(f"configuration introuvable : {chemin}") from None
    except tomllib.TOMLDecodeError as erreur:
        raise ErreurConfig(f"configuration illisible : {erreur}") from None

    # ⚠️ Le fichier contient des jetons : personne d'autre que root ne doit le lire.
    if os.name == "posix" and os.stat(chemin).st_mode & 0o077:
        journal.warning("%s est lisible par d'autres que son propriétaire : chmod 600", chemin)

    general = brut.get("general", {})
    notifications = brut.get("notifications", {})
    decouverte = brut.get("decouverte", {})
    mode_decouverte = decouverte.get("mode", "auto")
    if mode_decouverte not in MODES:
        raise ErreurConfig(f"[decouverte] mode {mode_decouverte!r} inconnu, attendu « auto » ou « manuel »")
    if "dossier_copies" not in general:
        raise ErreurConfig("[general] dossier_copies est obligatoire : il doit être sur le même "
                           "volume btrfs que les données, sinon la copie instantanée est impossible")

    conteneurs, vus = [], set()
    for bloc in brut.get("conteneur", []):
        nom = bloc.get("nom", "")
        if not nom:
            raise ErreurConfig("un bloc [[conteneur]] n'a pas de nom")
        if nom in vus:
            raise ErreurConfig(f"{nom} est déclaré deux fois")
        vus.add(nom)
        mode = bloc.get("mode", "")
        if mode not in MODES:
            raise ErreurConfig(f"{nom} : mode {mode!r} inconnu, attendu « auto » ou « manuel »")
        donnees = [_absolu(d, nom) for d in bloc.get("donnees", [])]
        conteneurs.append(ConfConteneur(nom, mode, donnees, bloc.get("sante", ""),
                                        bloc.get("version", "")))

    return Conf(
        fichier_etat=_absolu(general.get("fichier_etat",
                                         "/var/lib/compose-auto-update/etat.json"), "fichier_etat"),
        exports=[_absolu(e, "exports") for e in general.get("exports", [])],
        dossier_copies=_absolu(general["dossier_copies"], "dossier_copies"),
        copies_conservees=int(general.get("copies_conservees", 3)),
        observation=int(general.get("observation", 60)),
        delai_sante=int(general.get("delai_sante", 120)),
        attentes_reessai=[int(a) for a in general.get("attentes_reessai", [300, 1500])],
        ntfy_url=notifications.get("ntfy_url", ""),
        ntfy_sujet=notifications.get("ntfy_sujet", ""),
        ntfy_jeton=notifications.get("ntfy_jeton", ""),
        lien=notifications.get("lien", ""),
        kuma_push=notifications.get("kuma_push", ""),
        identifiants={hote: (v.get("utilisateur", ""), v.get("jeton", ""))
                      for hote, v in brut.get("registres", {}).items()},
        conteneurs=conteneurs,
        apres_mise_a_jour=[_absolu(c, "apres_mise_a_jour")
                           for c in general.get("apres_mise_a_jour", [])],
        mode_decouverte=mode_decouverte,
        racines_donnees=[_absolu(r, "racines_donnees") for r in decouverte.get("racines_donnees", [])],
        exclus=list(decouverte.get("exclus", [])),
    )
