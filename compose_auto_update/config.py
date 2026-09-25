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
    # Combien de nombres de la version définissent une mise à jour « importante ».
    # 1 : 6.4 → 7.0 l'est. 2 : 1.30 → 1.32 l'est aussi (branches de nginx).
    segments_majeurs: int = 1
    # --- images construites sur place (vide = image téléchargée d'un registre) ---
    construction: str = ""                        # « compose », ou dossier de construction
    reseau_construction: str = ""                 # réseau du build direct, ex. « host »
    arguments: dict = field(default_factory=dict) # variables de construction, ex. GIT_SHA
    depot: str = ""                               # dépôt git : ne reconstruire qu'avec le même commit
    # Commande lancée sur la nouvelle image AVANT d'arrêter l'ancienne, par exemple
    # la validation d'une configuration. Si elle échoue, rien n'est touché.
    controle: list = field(default_factory=list)


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
        construction = bloc.get("construction", "")
        if construction and construction != "compose":
            _absolu(construction, f"{nom} : construction")
        segments = int(bloc.get("segments_majeurs", 1))
        if segments < 1:
            raise ErreurConfig(f"{nom} : segments_majeurs doit valoir au moins 1")
        arguments = dict(bloc.get("arguments", {}))
        for cle, valeur in arguments.items():
            if not isinstance(valeur, str):
                raise ErreurConfig(f"{nom} : l'argument {cle} doit être une chaîne")
        depot = bloc.get("depot", "")
        if depot:
            _absolu(depot, f"{nom} : depot")
            if not construction:
                raise ErreurConfig(f"{nom} : « depot » n'a de sens qu'avec « construction »")
        # ⚠️ Une liste d'arguments, jamais une ligne de shell : voir commande.py
        controle = bloc.get("controle", [])
        if not isinstance(controle, list) or not all(isinstance(a, str) and a for a in controle):
            raise ErreurConfig(f"{nom} : « controle » doit être une liste d'arguments, "
                               f"par exemple [\"docker\", \"run\", …]")
        conteneurs.append(ConfConteneur(
            nom, mode, donnees, bloc.get("sante", ""), bloc.get("version", ""),
            segments_majeurs=segments, construction=construction,
            reseau_construction=bloc.get("reseau_construction", ""),
            arguments=arguments, depot=depot, controle=controle))

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
