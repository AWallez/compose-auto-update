"""Le déroulé d'une passe, d'une mise à jour et d'un retour arrière.

C'est le seul module qui DÉCIDE. Les autres savent faire une chose chacun :
parler au registre, à Docker, copier des données, vérifier la santé. Celui-ci
choisit quoi faire, dans quel ordre, et quoi faire quand ça rate.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import decouverte, donnees, sante, versions
from .commande import ErreurCommande, executer
from .config import ConfConteneur
from .etat import maintenant
from .image import analyser, sans_etiquette
from .registre import ErreurRegistre

journal = logging.getLogger(__name__)

# Étiquette posée sur l'ancienne image à chaque mise à jour. Elle la protège
# du nettoyage, et permet un retour arrière à la main dans les jours qui suivent.
ETIQUETTE_SECOURS = "avant-maj"

# Clés de notification qui ne sont pas des empreintes d'image.
CLE_DECOUVERTE = "decouverte"
CLE_LSCR = "avertissement:lscr.io"


@dataclass
class Nouveaute:
    """Une nouvelle version proposée par le registre pour un conteneur."""
    conf: object                  # ConfConteneur
    actuel: object                # docker.Conteneur, tel qu'il tourne
    empreinte: str                # empreinte de la nouvelle image
    version_actuelle: str | None
    version_nouvelle: str | None
    derniere_erreur: str = ""     # dernier message d'échec de téléchargement


@dataclass
class Bilan:
    """Ce qu'une passe a fait : sert à la notification et au signal de vie.

    Dans chaque liste, le DERNIER élément est la clé qui empêche de notifier
    deux fois la même chose (une empreinte, ou une clé comme CLE_LSCR).
    """
    mis_a_jour: list = field(default_factory=list)      # (nom, de, vers)
    erreurs: list = field(default_factory=list)         # (nom, message, clé ou None)
    en_attente: list = field(default_factory=list)      # (nom, de, vers, clé)
    nouveaux: list = field(default_factory=list)        # (nom, message, clé)
    avertissements: list = field(default_factory=list)  # (nom, message, clé)
    urgent: bool = False

    def resume(self):
        return (f"{len(self.mis_a_jour)} mise(s) à jour, {len(self.erreurs)} erreur(s), "
                f"{len(self.en_attente)} en attente, {len(self.nouveaux)} nouveau(x)")


class Moteur:
    def __init__(self, conf, docker, registre, etat, notificateur,
                 simulation=False, attendre=time.sleep):
        self.conf = conf
        self.docker = docker
        self.registre = registre
        self.etat = etat
        self.notificateur = notificateur
        self.simulation = simulation
        self.attendre = attendre
        self._plateforme = None

    # ============================================================ qui, et comment
    def conteneurs_a_traiter(self):
        """La configuration, complétée des conteneurs découverts sur la machine.

        Les conteneurs volontairement laissés de côté sont notés dans l'état,
        avec leur raison : une ligne vide sur un tableau de bord ne dit pas
        POURQUOI un conteneur n'a pas d'option de mise à jour.
        """
        liste = list(self.conf.conteneurs)
        tous = self.docker.tous()
        ignores = {c.nom: {"etiquette": "exclu", "raison": "exclu par la configuration"}
                   for c in tous if c.nom in self.conf.exclus}
        for c in decouverte.inconnus(self.conf, tous):
            if not c.projet or not c.service:
                # ⚠️ Sans étiquettes Compose, l'outil ne saurait pas le recréer à
                # l'identique : il le surveille, mais ne le touchera jamais seul.
                liste.append(ConfConteneur(c.nom, "manuel", decouvert=True, recreable=False))
                continue
            _, empreintes, _, _ = self.docker.image(c.image_id)
            if not empreintes:
                ignores[c.nom] = {"etiquette": "local", "raison": (
                    "image construite sur place : aucun registre ne peut la mettre à jour, "
                    "elle se met à jour en la reconstruisant")}
                continue
            liste.append(ConfConteneur(c.nom, self.conf.mode_decouverte,
                                       decouverte.donnees_probables(c, tous, self.conf.racines_donnees),
                                       decouvert=True))
        self.etat.donnees["ignores"] = ignores
        return liste

    def mode_effectif(self, cc):
        """Le choix fait depuis la page prime sur la configuration.

        ⚠️ Sauf pour un conteneur que Compose n'a pas créé : il reste manuel quoi
        qu'on choisisse, puisque l'outil ne sait pas le recréer.
        """
        if not cc.recreable:
            return "manuel"
        return self.etat.mode_choisi(cc.nom) or cc.mode

    def _trouver(self, nom):
        cc = next((c for c in self.conteneurs_a_traiter() if c.nom == nom), None)
        if cc is None:
            raise ValueError(f"{nom} : conteneur inconnu, ni configuré ni en marche")
        return cc

    # =================================================================== détection
    def examiner(self, cc):
        """Compare l'image en service à celle du registre. Renvoie None si à jour."""
        actuel = self.docker.conteneur(cc.nom)
        ref = analyser(actuel.image)
        _, empreintes, etiquettes, environnement = self.docker.image(actuel.image_id)

        suivi = self.etat.conteneur(cc.nom)
        suivi["mode"] = self.mode_effectif(cc)
        suivi["decouvert"] = cc.decouvert
        suivi["version"] = versions.lire(etiquettes, environnement, cc.version)
        suivi["empreinte"] = empreintes[0] if empreintes else None
        suivi["avertissements"] = []
        if ref.registre == "lscr.io":
            # ⚠️ lscr.io n'est qu'une passerelle vers ghcr.io. Le 25/09/2026 elle
            # s'est effondrée à 4 h pile pendant que ghcr.io répondait normalement.
            suivi["avertissements"].append({"cle": CLE_LSCR, "message": (
                f"passe encore par lscr.io : dans son fichier compose, remplace "
                f"« {sans_etiquette(actuel.image)} » par « ghcr.io/{ref.depot} »")})

        distante = self.registre.empreinte(ref)
        # ⚠️ ON COMPARE DES EMPREINTES, PAS DES NUMÉROS DE VERSION. LinuxServer
        # reconstruit ses images sans changer le numéro de l'application : même
        # « 6.4.4 », image différente, avec des correctifs de sécurité dedans.
        if distante in empreintes:
            suivi["disponible"] = None
            return None

        etiquettes_n, environnement_n = self.registre.configuration(
            ref, distante, self._plateforme_docker())
        nouvelle = versions.lire(etiquettes_n, environnement_n, cc.version)
        suivi["disponible"] = {"version": nouvelle, "empreinte": distante, "vue_le": maintenant()}
        return Nouveaute(cc, actuel, distante, suivi["version"], nouvelle)

    def montee_majeure(self, n):
        """Vrai si la nouvelle version change de majeure, ou si on ne peut pas le savoir."""
        if versions.etiquette_fige_majeure(analyser(n.actuel.image).etiquette):
            return False
        avant = versions.majeure(n.version_actuelle)
        apres = versions.majeure(n.version_nouvelle)
        if avant is None or apres is None:
            return True          # ⚠️ version illisible : dans le doute, on te demande
        return apres > avant

    def _plateforme_docker(self):
        if self._plateforme is None:
            self._plateforme = self.docker.plateforme()
        return self._plateforme

    # ======================================================================= passe
    def passe(self):
        """La passe complète : détecter, trier, télécharger, installer, prévenir."""
        bilan = Bilan()
        a_installer = []
        for cc in self.conteneurs_a_traiter():
            if cc.decouvert and not self.etat.deja_notifie(cc.nom, CLE_DECOUVERTE):
                bilan.nouveaux.append((cc.nom, self._texte_decouverte(cc), CLE_DECOUVERTE))
            try:
                n = self.examiner(cc)
            except (ErreurRegistre, ErreurCommande) as erreur:
                journal.error("%s : vérification impossible : %s", cc.nom, erreur)
                bilan.erreurs.append((cc.nom, f"vérification impossible : {erreur}", None))
                continue
            for avertissement in self.etat.conteneur(cc.nom)["avertissements"]:
                if not self.etat.deja_notifie(cc.nom, avertissement["cle"]):
                    bilan.avertissements.append((cc.nom, avertissement["message"], avertissement["cle"]))
            if n is None:
                journal.info("%s : à jour", cc.nom)
                continue
            if self.mode_effectif(cc) == "manuel" or self.etat.blocage(cc.nom):
                journal.info("%s : nouvelle version %s, attend une action manuelle",
                             cc.nom, n.version_nouvelle)
                self._en_attente(n, bilan)
                continue
            if n.actuel.etat != "running":
                # ⚠️ Un conteneur arrêté l'a sans doute été exprès : le recréer le
                # redémarrerait. On le laisse tel quel.
                journal.info("%s : à l'arrêt, laissé tel quel", cc.nom)
                continue
            if self.montee_majeure(n):
                journal.info("%s : montée majeure, attend ton accord", cc.nom)
                self.etat.bloquer(cc.nom, "majeure", self._texte_majeure(n))
                self._en_attente(n, bilan)
                continue
            a_installer.append(n)

        rates = self._telecharger_et_installer(a_installer, bilan)
        for n in rates:
            message = f"téléchargement impossible après {1 + len(self.conf.attentes_reessai)} essais"
            self.etat.bloquer(n.conf.nom, "telechargement", message, n.derniere_erreur)
            bilan.erreurs.append((n.conf.nom, message, n.empreinte))

        self._terminer(bilan)
        return bilan

    def _telecharger_et_installer(self, a_installer, bilan):
        """Télécharge et installe, avec des nouveaux essais espacés pour les ratés.

        ⚠️ UN ÉCHEC NE RETARDE PAS LES AUTRES. On fait le tour complet, puis on
        revient sur les ratés après une attente : un registre en difficulté
        pendant une minute, comme lscr.io à 4 h pile le 25/09/2026, ne bloque
        que ses propres images, et seulement le temps qu'il se remette.
        """
        attentes = [0, *self.conf.attentes_reessai]
        for numero, attente in enumerate(attentes, 1):
            if not a_installer:
                break
            if attente:
                journal.info("%d téléchargement(s) raté(s), nouvel essai dans %d s",
                             len(a_installer), attente)
                self.attendre(attente)
            rates = []
            for n in a_installer:
                try:
                    self.docker.telecharger(n.actuel)
                except ErreurCommande as erreur:
                    journal.warning("%s : téléchargement %d/%d raté : %s",
                                    n.conf.nom, numero, len(attentes), erreur.erreur)
                    n.derniere_erreur = erreur.erreur
                    rates.append(n)
                    continue
                self._installer(n, bilan)
            a_installer = rates
        return a_installer

    # ======================================================== une mise à jour
    def _installer(self, n, bilan):
        """Arrêt, copie des données, recréation, vérification. Retour arrière si ça casse.

        ⚠️ L'ANCIEN CONTENEUR N'EST ARRÊTÉ QU'APRÈS LE TÉLÉCHARGEMENT. Si le
        registre ne répond pas, le service n'a pas été interrompu une seconde.
        """
        nom, c = n.conf.nom, n.actuel
        horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
        copie, nouvelle_lancee = None, False
        try:
            self.docker.arreter(c)
            # ⚠️ Arrêter AVANT de copier : copier une base de données en pleine
            # écriture donnerait une copie incohérente, inutilisable au retour.
            copie = donnees.copier(n.conf.donnees, self.conf.dossier_copies,
                                   nom, horodatage, self.simulation)
            self.docker.etiqueter(c.image_id, f"{sans_etiquette(c.image)}:{ETIQUETTE_SECOURS}")
            nouvelle_lancee = True
            self.docker.recreer(c)
            ok, raison = self._verifier(n.conf)
        except (ErreurCommande, OSError) as erreur:
            ok, raison = False, f"erreur pendant la mise à jour : {erreur}"

        if ok:
            journal.info("%s : mis à jour, %s → %s", nom, n.version_actuelle, n.version_nouvelle)
            self.etat.debloquer(nom)
            suivi = self.etat.conteneur(nom)
            suivi["version"], suivi["empreinte"], suivi["disponible"] = (
                n.version_nouvelle, n.empreinte, None)
            self.etat.noter(nom, "mis_a_jour", f"{n.version_actuelle} → {n.version_nouvelle}")
            bilan.mis_a_jour.append((nom, n.version_actuelle, n.version_nouvelle))
            try:
                donnees.elaguer(self.conf.dossier_copies, nom,
                                self.conf.copies_conservees, self.simulation)
            except OSError as erreur:
                journal.warning("%s : anciennes copies non supprimées : %s", nom, erreur)
            return True

        journal.error("%s : %s ; retour à l'ancienne version", nom, raison)
        # Les données ne sont restaurées que si la nouvelle version a pu les
        # toucher. Avant la recréation, elles sont intactes : rien à remettre.
        self._retour_arriere(n, copie if nouvelle_lancee else None, horodatage, raison, bilan)
        return False

    def _retour_arriere(self, n, copie, horodatage, raison, bilan):
        """Remet l'ancienne image, et les anciennes données si la nouvelle version a tourné."""
        nom, c = n.conf.nom, n.actuel
        try:
            try:
                self.docker.arreter(c)
            except ErreurCommande:
                pass     # ⚠️ normal si Compose avait déjà supprimé le conteneur raté
            if copie:
                donnees.restaurer(copie, horodatage, self.simulation)
            # Après le téléchargement, la référence compose (« …:latest »)
            # désigne la nouvelle image. On la refait pointer vers l'ancienne,
            # puis on recrée : Compose relance exactement ce qui tournait avant.
            self.docker.etiqueter(c.image_id, c.image)
            self.docker.recreer(c)
            ok, raison_retour = self._verifier(n.conf)
        except (ErreurCommande, OSError) as erreur:
            ok, raison_retour = False, str(erreur)

        if ok:
            self.etat.bloquer(nom, "installation", raison)
            bilan.erreurs.append((nom, f"{raison} ; ancienne version remise en place", n.empreinte))
        else:
            journal.critical("%s : RETOUR ARRIÈRE ÉCHOUÉ : %s", nom, raison_retour)
            self.etat.bloquer(nom, "retour_arriere",
                              f"{raison} ; retour arrière échoué : {raison_retour}")
            bilan.erreurs.append((nom, f"{raison} ; RETOUR ARRIÈRE ÉCHOUÉ", n.empreinte))
            bilan.urgent = True

    def _verifier(self, cc):
        if self.simulation:
            return True, ""
        return sante.verifier(self.docker, cc.nom, cc.sante or None,
                              observation=self.conf.observation,
                              delai_http=self.conf.delai_sante, attendre=self.attendre)

    # ================================================== actions demandées par toi
    def appliquer(self, nom):
        """Mise à jour immédiate d'un conteneur, demandée par toi (page ou terminal).

        ⚠️ ELLE PASSE OUTRE LE BLOCAGE ET LA MONTÉE MAJEURE : c'est toi qui la
        demandes, après avoir lu les notes de version. Tout le reste est
        identique : copie, vérification, retour arrière. Et si elle réussit,
        un conteneur automatique bloqué redevient automatique.
        """
        cc = self._trouver(nom)
        bilan = Bilan()
        n = self.examiner(cc)
        if n is None:
            journal.info("%s : déjà à jour", nom)
            self.etat.debloquer(nom)
        else:
            try:
                self.docker.telecharger(n.actuel)
            except ErreurCommande as erreur:
                self.etat.bloquer(nom, "telechargement", "téléchargement impossible", erreur.erreur)
                bilan.erreurs.append((nom, "téléchargement impossible", n.empreinte))
            else:
                self._installer(n, bilan)
        self._terminer(bilan)
        return bilan

    def choisir_mode(self, nom, mode):
        """Bascule un conteneur en automatique ou en manuel (bouton de la page).

        `mode` vaut « auto », « manuel », ou None pour revenir à la configuration.
        """
        if mode not in ("auto", "manuel", None):
            raise ValueError(f"mode {mode!r} inconnu")
        cc = self._trouver(nom)
        self.etat.choisir_mode(nom, mode)
        # ⚠️ Le mode AFFICHÉ se recalcule tout de suite. Sans cette ligne, le
        # choix était bien enregistré mais la page montrait l'ancien mode jusqu'à
        # la passe suivante : on croyait que le clic n'avait rien fait.
        self.etat.conteneur(nom)["mode"] = self.mode_effectif(cc)
        if not self.simulation:
            self.etat.sauver(*self.conf.exports)

    # ================================================================== clôture
    def _en_attente(self, n, bilan):
        """⚠️ UNE SEULE NOTIFICATION PAR VERSION. La clé est l'empreinte : tant
        qu'elle ne change pas, on ne relance pas. Une nouvelle version, c'est une
        nouvelle empreinte, donc une nouvelle notification."""
        if not self.etat.deja_notifie(n.conf.nom, n.empreinte):
            bilan.en_attente.append((n.conf.nom, n.version_actuelle, n.version_nouvelle,
                                     n.empreinte))

    def _texte_decouverte(self, cc):
        if not cc.recreable:
            return "suivi en manuel : il n'a pas été créé par Docker Compose"
        protege = ", ".join(cc.donnees) if cc.donnees else "aucune donnée identifiée"
        return f"suivi en {cc.mode} ; copié avant chaque mise à jour : {protege}"

    @staticmethod
    def _texte_majeure(n):
        if versions.majeure(n.version_actuelle) is None or versions.majeure(n.version_nouvelle) is None:
            return (f"version illisible ({n.version_actuelle or '?'} → "
                    f"{n.version_nouvelle or '?'}), validation manuelle par prudence")
        return f"version majeure : {n.version_actuelle} → {n.version_nouvelle}"

    def _terminer(self, bilan):
        try:
            self.docker.nettoyer_images()
        except ErreurCommande as erreur:
            journal.warning("nettoyage des images impossible : %s", erreur.erreur)
        self.etat.donnees["derniere_passe"] = {"fin": maintenant(), "bilan": bilan.resume()}
        if bilan.mis_a_jour:
            self._apres_mise_a_jour()

        # ⚠️ Une clé n'est marquée « signalée » qu'une fois la notification
        # réellement partie. Si ntfy est injoignable, on réessaiera à la passe suivante.
        if self._notifier(bilan):
            for nom, *_, cle in [*bilan.erreurs, *bilan.en_attente,
                                 *bilan.nouveaux, *bilan.avertissements]:
                if cle:
                    self.etat.marquer_notifie(nom, cle)
        if not self.simulation:
            self.etat.sauver(*self.conf.exports)

    def _apres_mise_a_jour(self):
        """Lance les commandes prévues après une mise à jour réussie.

        Par exemple le script qui relève les versions pour un tableau de bord :
        sans lui, la carte montrait la date d'avant la mise à jour jusqu'au
        relevé suivant. Seulement quand quelque chose a VRAIMENT changé.

        ⚠️ Un échec ici n'annule rien : les mises à jour ont eu lieu. On le note
        au journal et on continue.
        """
        for commande in self.conf.apres_mise_a_jour:
            if self.simulation:
                journal.info("simulation, non exécuté : %s", commande)
                continue
            try:
                executer([commande], delai=300)
                journal.info("après mise à jour : %s exécuté", commande)
            except (ErreurCommande, OSError) as erreur:
                journal.warning("après mise à jour : %s en échec : %s", commande, erreur)

    def _notifier(self, bilan):
        """Rien ne part si rien ne mérite ton attention : le silence est normal."""
        sections = [
            ("Erreurs :", [f"• {nom} : {message}" for nom, message, _ in bilan.erreurs]),
            ("À faire à la main :", [f"• {nom} : {de or '?'} → {vers or '?'}"
                                     for nom, de, vers, _ in bilan.en_attente]),
            ("Nouveaux conteneurs :", [f"• {nom} : {message}" for nom, message, _ in bilan.nouveaux]),
            ("À corriger :", [f"• {nom} : {message}" for nom, message, _ in bilan.avertissements]),
        ]
        lignes = []
        for titre_section, contenu in sections:
            if contenu:
                lignes += ([""] if lignes else []) + [titre_section, *contenu]
        if not lignes:
            return False

        if bilan.urgent:
            titre, priorite = "Mises à jour : retour arrière ÉCHOUÉ", 5
        elif bilan.erreurs:
            titre, priorite = f"Mises à jour : {len(bilan.erreurs)} erreur(s)", 4
        else:
            parties = []
            if bilan.en_attente:
                parties.append(f"{len(bilan.en_attente)} à faire à la main")
            if bilan.nouveaux:
                parties.append(f"{len(bilan.nouveaux)} nouveau(x) conteneur(s)")
            if bilan.avertissements:
                parties.append(f"{len(bilan.avertissements)} à corriger")
            titre, priorite = "Mises à jour : " + ", ".join(parties), 3
        return self.notificateur.envoyer(titre, "\n".join(lignes), priorite)
