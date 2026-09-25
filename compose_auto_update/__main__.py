"""Point d'entrée : python3 -m compose_auto_update [options] <commande>

Commandes :
  verifier        ce qui serait mis à jour ; ne télécharge ni ne modifie rien
  passe           la passe complète, celle que lance la minuterie systemd
  appliquer NOM   met un conteneur à jour tout de suite (bouton de la page)
  mode NOM CHOIX  bascule un conteneur : auto, manuel, ou defaut (celui de la configuration)
  etat            affiche ce que le programme sait de chaque conteneur

Options :
  --config CHEMIN   fichier de configuration
  --simulation      déroule tout sans rien modifier : chaque action est écrite au journal
  --bavard          journal détaillé, commandes lancées comprises

Codes de sortie : 0 tout va bien, 1 au moins une erreur, 2 configuration
invalide, 3 une autre passe est déjà en cours.
"""

import argparse
import contextlib
import logging
import sys

from . import config as configuration
from .commande import ErreurCommande
from .docker import Docker
from .etat import Etat
from .moteur import Moteur
from .notifier import Notificateur
from .registre import ErreurRegistre, Registre

CONFIG_PAR_DEFAUT = "/etc/compose-auto-update/config.toml"
VERROU = "/run/lock/compose-auto-update.lock"

journal = logging.getLogger("compose_auto_update")


class PasseEnCours(Exception):
    pass


@contextlib.contextmanager
def verrou(chemin=VERROU):
    """⚠️ UNE SEULE PASSE À LA FOIS. La minuterie de 7 h 10 et le bouton de la
    page pourraient se croiser : deux programmes qui recréent le même conteneur
    en même temps, c'est la garantie d'un état incohérent. Le second refuse de
    démarrer et le dit, plutôt que d'attendre sans fin.

    Le verrou est posé par le noyau et libéré à la fin du processus, même en
    cas d'arrêt brutal : un verrou oublié ne peut pas bloquer la nuit suivante.
    """
    import fcntl                       # propre à Linux : importé ici seulement
    with open(chemin, "w") as fichier:
        try:
            fcntl.flock(fichier, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PasseEnCours() from None
        yield


def verifier(moteur):
    """Lecture seule : on interroge Docker et les registres, rien d'autre."""
    code = 0
    for cc in moteur.conteneurs_a_traiter():
        mode = moteur.mode_effectif(cc)
        try:
            n = moteur.examiner(cc)
        except (ErreurRegistre, ErreurCommande) as erreur:
            print(f"{cc.nom:22} {mode:7} ERREUR : {erreur}")
            code = 1
            continue
        if n is None:
            statut = "à jour"
        else:
            changement = f"{n.version_actuelle or '?'} → {n.version_nouvelle or '?'}"
            if mode == "manuel" or moteur.etat.blocage(cc.nom):
                statut = f"en attente d'une action manuelle ({changement})"
            elif moteur.montee_majeure(n):
                statut = f"bloqué, montée majeure ({changement})"
            else:
                statut = f"serait mis à jour ({changement})"
        print(f"{cc.nom:22} {mode:7} {statut}")
        if cc.decouvert:
            print(f"{'':30}découvert ; données copiées : {', '.join(cc.donnees) or 'aucune'}")
        for avertissement in moteur.etat.conteneur(cc.nom)["avertissements"]:
            print(f"{'':30}⚠️ {avertissement['message']}")
    return code


def afficher_etat(etat):
    passe = etat.donnees.get("derniere_passe") or {}
    print(f"dernière passe : {passe.get('fin', 'jamais')}  {passe.get('bilan', '')}")
    print("(* : mode choisi depuis la page, prioritaire sur la configuration)\n")
    for nom, suivi in sorted(etat.donnees["conteneurs"].items()):
        mode = (suivi.get("mode") or "?") + ("*" if suivi.get("mode_choisi") else "")
        ligne = f"{nom:22} {mode:7} {suivi.get('version') or '?'}"
        if suivi.get("decouvert"):
            ligne += "  (découvert)"
        if suivi.get("disponible"):
            ligne += f"  → {suivi['disponible'].get('version') or '?'} disponible"
        if suivi.get("blocage"):
            ligne += f"\n{'':30}BLOQUÉ ({suivi['blocage']['raison']}) : {suivi['blocage']['message']}"
        print(ligne)
    return 0


def main(argv=None):
    parseur = argparse.ArgumentParser(prog="compose-auto-update",
                                      description="Mises à jour de conteneurs Docker Compose.")
    parseur.add_argument("--config", default=CONFIG_PAR_DEFAUT)
    parseur.add_argument("--simulation", action="store_true")
    parseur.add_argument("--bavard", action="store_true")
    commandes = parseur.add_subparsers(dest="commande", required=True)
    commandes.add_parser("verifier")
    commandes.add_parser("passe")
    commandes.add_parser("appliquer").add_argument("nom")
    choix = commandes.add_parser("mode")
    choix.add_argument("nom")
    choix.add_argument("choix", choices=["auto", "manuel", "defaut"])
    commandes.add_parser("etat")
    args = parseur.parse_args(argv)

    # ⚠️ Pas d'heure dans le format : journald ajoute la sienne, deux dates par
    # ligne ne feraient que brouiller la lecture.
    logging.basicConfig(level=logging.DEBUG if args.bavard else logging.INFO,
                        format="%(levelname)-8s %(message)s", stream=sys.stderr)

    try:
        conf = configuration.charger(args.config)
    except configuration.ErreurConfig as erreur:
        journal.error("%s", erreur)
        return 2

    if args.commande == "etat":
        return afficher_etat(Etat(conf.fichier_etat))

    simulation = args.simulation or args.commande == "verifier"
    notificateur = Notificateur(conf.ntfy_url, conf.ntfy_sujet, conf.ntfy_jeton,
                                conf.lien, conf.kuma_push, simulation)
    moteur = Moteur(conf, Docker(simulation), Registre(conf.identifiants),
                    Etat(conf.fichier_etat), notificateur, simulation)

    if args.commande == "verifier":
        return verifier(moteur)

    try:
        with verrou():
            if args.commande == "mode":
                moteur.choisir_mode(args.nom, None if args.choix == "defaut" else args.choix)
                journal.info("%s : mode %s enregistré", args.nom, args.choix)
                return 0
            bilan = moteur.passe() if args.commande == "passe" else moteur.appliquer(args.nom)
    except PasseEnCours:
        journal.error("une autre passe est en cours, réessaie dans quelques minutes")
        return 3
    except ValueError as erreur:
        journal.error("%s", erreur)          # nom de conteneur ou mode inconnu : erreur de saisie
        return 2
    except Exception as erreur:
        # ⚠️ LE CAS QUI COMPTE LE PLUS : le programme lui-même s'est arrêté en
        # route. C'est exactement la panne silencieuse qu'on veut rendre
        # impossible, d'où le signal « down » envoyé à Uptime Kuma pour la
        # passe nocturne, la seule que Kuma surveille.
        journal.exception("arrêt inattendu")
        if args.commande == "passe":
            notificateur.signal_de_vie(False, f"arrêt inattendu : {erreur}")
        return 1

    journal.info("bilan : %s", bilan.resume())
    if args.commande == "passe":
        notificateur.signal_de_vie(True, bilan.resume())
    return 1 if bilan.erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
