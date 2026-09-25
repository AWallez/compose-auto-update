"""Le déroulé d'une passe, d'une mise à jour et d'un retour arrière.

C'est le seul module qui DÉCIDE. Les autres savent faire une chose chacun :
parler au registre, à Docker, copier des données, vérifier la santé. Celui-ci
choisit quoi faire, dans quel ordre, et quoi faire quand ça rate.

Deux sortes d'images passent par ici :
  - téléchargées d'un registre : on compare leur empreinte à celle du registre ;
  - construites sur place (option `construction`) : aucun registre ne les
    connaît, on surveille donc leurs images de BASE et on reconstruit quand
    l'une d'elles change. Voir construction.py.
Ensuite tout est commun : arrêt, copie, recréation, vérification, retour arrière.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import construction, decouverte, donnees, sante, versions
from .commande import ErreurCommande, executer
from .config import ConfConteneur
from .construction import ErreurConstruction
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

# Ce qui peut faire échouer l'examen d'UN conteneur sans arrêter la passe.
ERREURS_EXAMEN = (ErreurRegistre, ErreurCommande, ErreurConstruction)


def _texte(erreur):
    """Le message d'une erreur, sans les guillemets qu'ajoute KeyError."""
    return erreur.args[0] if isinstance(erreur, KeyError) and erreur.args else str(erreur)


@dataclass
class Nouveaute:
    """Une nouvelle version proposée pour un conteneur."""
    conf: object                  # ConfConteneur
    actuel: object                # docker.Conteneur, tel qu'il tourne
    empreinte: str                # empreinte de la nouvelle image (clé de notification)
    version_actuelle: str | None
    version_nouvelle: str | None
    derniere_erreur: str = ""     # dernier message d'échec de téléchargement
    # --- images construites sur place ---
    changements: list = field(default_factory=list)   # (base, de, vers, empreinte) par base changée
    code: str = ""                # pourquoi on ne peut pas reconstruire à l'identique ; vide sinon


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
    locaux: list = field(default_factory=list)          # (nom, raison) : images maison non suivies
    urgent: bool = False

    def resume(self):
        return (f"{len(self.mis_a_jour)} mise(s) à jour, {len(self.erreurs)} erreur(s), "
                f"{len(self.en_attente)} en attente, "
                f"{len(self.nouveaux) + len(self.locaux)} nouveau(x)")


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
        self._distantes = {}      # empreintes des images de base, demandées une fois par passe

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
            donnees_c = decouverte.donnees_probables(c, tous, self.conf.racines_donnees)
            _, empreintes, etiquettes, _ = self.docker.image(c.image_id)
            if not empreintes:
                cc, raison = self._construction_decouverte(c, etiquettes, donnees_c)
                if cc:
                    liste.append(cc)
                else:
                    ignores[c.nom] = {"etiquette": "local", "raison": (
                        f"image construite sur place, pas mise à jour : {raison}. Déclare-la "
                        f"dans la configuration avec « construction » pour qu'elle le soit")}
                continue
            liste.append(ConfConteneur(c.nom, self.conf.mode_decouverte, donnees_c, decouvert=True))
        self.etat.donnees["ignores"] = ignores
        return liste

    def _construction_decouverte(self, c, etiquettes, donnees_c):
        """Une image construite sur place, trouvée sur la machine : peut-on la suivre seul ?

        Seulement si TOUT est vérifiable, car c'est la garantie de ne jamais
        déployer de code : construite par Compose, dans un dépôt git, avec son
        commit inscrit dans l'image, et un Dockerfile qui le transmet (sans quoi
        l'image reconstruite le perdrait). Sinon : (None, la raison).
        """
        try:
            contexte, chemin = self.docker.plan_de_construction(c, "compose")
            with open(chemin, encoding="utf-8") as fichier:
                texte = fichier.read()
        except (ErreurCommande, OSError, KeyError, ValueError) as erreur:
            return None, f"sa construction est introuvable ({_texte(erreur)})"
        if not construction.bases(texte):
            return None, "son Dockerfile n'a aucune image de base lisible"
        depot = construction.depot_de(contexte)
        if not depot:
            return None, "elle n'est pas construite depuis un dépôt git"
        if not construction.est_un_commit(etiquettes.get(construction.REVISION)):
            return None, (f"l'image ne dit pas de quel commit elle sort "
                          f"(étiquette {construction.REVISION})")
        variable = construction.argument_de_revision(texte)
        if not variable:
            return None, "son Dockerfile n'inscrit pas le commit dans l'image"
        return ConfConteneur(c.nom, self.conf.mode_decouverte, donnees_c, decouvert=True,
                             construction="compose", depot=depot,
                             arguments={variable: f"label:{construction.REVISION}"}), ""

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
        """Cherche une nouvelle version. Renvoie None si le conteneur est à jour."""
        actuel = self.docker.conteneur(cc.nom)
        suivi = self.etat.conteneur(cc.nom)
        suivi["mode"] = self.mode_effectif(cc)
        suivi["decouvert"] = cc.decouvert
        suivi["avertissements"] = []
        if cc.construction:
            return self._examiner_construction(cc, actuel, suivi)

        ref = analyser(actuel.image)
        _, empreintes, etiquettes, environnement = self.docker.image(actuel.image_id)
        suivi["version"] = versions.lire(etiquettes, environnement, cc.version)
        suivi["empreinte"] = empreintes[0] if empreintes else None
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
        """Vrai si la mise à jour est importante, ou si on ne peut pas le savoir."""
        if n.conf.construction:
            return bool(self._bases_importantes(n))
        return self._importante(analyser(n.actuel.image).etiquette, n.version_actuelle,
                                n.version_nouvelle, n.conf.segments_majeurs)

    @staticmethod
    def _importante(etiquette, avant, apres, segments):
        """Une montée de version qui mérite ton accord.

        ⚠️ SI L'ÉTIQUETTE FIGE DÉJÀ UN NUMÉRO (« 2 », « 22-alpine », « v3.41.3 »),
        c'est la limite choisie en écrivant le fichier compose : tout ce qu'elle
        laisse passer est accepté. Sinon on compare les `segments` premiers
        nombres : 1 pour 6.4 → 7.0, 2 pour que nginx 1.30 → 1.32 compte aussi.
        """
        if versions.etiquette_fige_majeure(etiquette):
            return False
        a, b = versions.prefixe(avant, segments), versions.prefixe(apres, segments)
        if a is None or b is None:
            return True          # ⚠️ version illisible : dans le doute, on te demande
        return b > a

    def _plateforme_docker(self):
        if self._plateforme is None:
            self._plateforme = self.docker.plateforme()
        return self._plateforme

    # ================================================ images construites sur place
    def _examiner_construction(self, cc, actuel, suivi):
        """Ses images de base ont-elles changé depuis sa construction ?

        ⚠️ AUCUN REGISTRE NE CONNAÎT CETTE IMAGE. On compare donc chaque image de
        base à celle qui a servi à la construire. Celle-ci est relevée dans le
        cache local de Docker : juste après une reconstruction par l'outil, ou à
        la première passe qui voit l'image (construite par ton outil de
        déploiement, qui vient de télécharger ses bases avec « build --pull »).
        """
        _, _, etiquettes, _ = self.docker.image(actuel.image_id)
        contexte, chemin, texte = self._plan(cc, actuel)
        for base in construction.inconnues(texte):
            suivi["avertissements"].append({"cle": f"base:{base}", "message": (
                f"l'image de base « {base} » est écrite avec une variable : elle n'est pas surveillée")})

        releve = suivi.get("construction") or {}
        if releve.get("image_id") != actuel.image_id:
            releve = {"image_id": actuel.image_id,
                      "dockerfile": construction.empreinte_texte(texte),
                      "bases": self._releve_bases(construction.bases(texte))}
            suivi["construction"] = releve
            journal.info("%s : nouvelle image, images de base relevées", cc.nom)
        bases = releve["bases"]
        finale = next(reversed(bases), None)      # la base de l'image finale, celle qui tourne
        suivi["version"] = self._libelle(finale, bases[finale]["version"]) if finale else None
        suivi["empreinte"] = None

        # Ce qui construit ET lance ce service : son dossier, son Dockerfile, ses fichiers compose
        code = self._code_modifie(cc, texte, releve, etiquettes, [contexte, chemin, *actuel.fichiers])
        blocage = self.etat.blocage(cc.nom)
        if blocage and blocage["raison"] == "code" and not code:
            self.etat.debloquer(cc.nom)       # le code a été redéployé : plus d'obstacle

        changements = []
        for base, connu in bases.items():
            ref = analyser(base)
            if ref not in self._distantes:
                self._distantes[ref] = self.registre.empreinte(ref)
            distante = self._distantes[ref]
            if distante in connu["empreintes"]:
                continue
            etiquettes_n, environnement_n = self.registre.configuration(
                ref, distante, self._plateforme_docker())
            changements.append((base, connu["version"],
                                construction.version_de_base(ref.depot, etiquettes_n, environnement_n),
                                distante))
        if not changements:
            suivi["disponible"] = None
            return None

        # Ce qui s'affiche : la base de l'image finale, puis ce qui a changé d'autre.
        # « nginx 1.30.5 + node 22.23.4 » : même nginx, reconstruit avec un node corrigé.
        nouvelles = {base: vers for base, _, vers, _ in changements}
        libelles = [self._libelle(finale, nouvelles.get(finale, bases[finale]["version"]))]
        libelles += [self._libelle(base, vers) for base, _, vers, _ in changements if base != finale]
        vers = " + ".join(dict.fromkeys(libelles))
        if vers == suivi["version"]:
            # Même numéro, image différente : l'éditeur l'a republiée avec des
            # correctifs (paquets Alpine, par exemple). Sans cette mention, on lirait
            # « nginx 1.30.5 → nginx 1.30.5 » sans comprendre ce qui change.
            vers += " (image corrigée)"
        cle = construction.empreinte_texte("\n".join(sorted(f"{b}@{e}" for b, _, _, e in changements)))
        suivi["disponible"] = {"version": vers, "empreinte": cle, "vue_le": maintenant()}
        return Nouveaute(cc, actuel, cle, suivi["version"], vers,
                         changements=changements, code=code)

    def _plan(self, cc, actuel):
        """(dossier de construction, chemin du Dockerfile, texte du Dockerfile)."""
        try:
            contexte, chemin = self.docker.plan_de_construction(actuel, cc.construction)
            with open(chemin, encoding="utf-8") as fichier:
                return contexte, chemin, fichier.read()
        except (OSError, KeyError, ValueError) as erreur:
            raise ErreurConstruction(f"Dockerfile introuvable ou illisible : {_texte(erreur)}") from None

    def _releve_bases(self, refs):
        """Empreintes et versions des images de base, lues dans le cache local de Docker.

        ⚠️ Une base absente du cache est notée sans empreinte : elle sera vue
        comme changée, et l'image reconstruite par prudence.
        """
        releve = {}
        for base in refs:
            try:
                _, empreintes, etiquettes, environnement = self.docker.image(base)
            except ErreurCommande:
                releve[base] = {"empreintes": [], "version": None}
                continue
            releve[base] = {"empreintes": empreintes, "version": construction.version_de_base(
                analyser(base).depot, etiquettes, environnement)}
        return releve

    def _code_modifie(self, cc, texte, releve, etiquettes, chemins):
        """Pourquoi la reconstruction ne serait pas identique, ou "" si elle le serait.

        ⚠️ C'EST LA GARANTIE QUE L'OUTIL NE DÉPLOIE JAMAIS DE CODE. Il ne change
        que les images de base ; le code, c'est ton outil de déploiement.

        Le dépôt peut être en avance sur l'image : un « deploy.sh web » récupère
        tout le dépôt mais ne reconstruit que le site. Ce n'est un obstacle pour
        l'api que si les commits en plus touchent ce qui la construit ou la lance.
        """
        if construction.empreinte_texte(texte) != releve["dockerfile"]:
            return "le Dockerfile a changé depuis la construction de l'image en service"
        if not cc.depot:
            return ""
        try:
            depot = construction.commit_du_depot(cc.depot)
        except OSError as erreur:
            raise ErreurConstruction(f"dépôt {cc.depot} illisible : {erreur}") from None
        en_service = etiquettes.get(construction.REVISION) or ""
        if depot and depot == en_service:
            return ""
        if not construction.est_un_commit(en_service):
            return (f"l'image en service ne dit pas de quel commit elle sort "
                    f"(étiquette {construction.REVISION} : « {en_service or 'absente'} »)")
        if not depot:
            return f"impossible de lire le commit du dépôt {cc.depot}"

        suivis = self._relatifs(cc.depot, chemins)
        ecart = f"dépôt au commit {depot[:7]}, image au commit {en_service[:7]}"
        try:
            nombre = construction.commits_touchant(cc.depot, en_service, depot, suivis)
        except (ErreurCommande, OSError, ValueError) as erreur:
            # ⚠️ Dans le doute, on ne reconstruit pas : sans git, pas de comparaison.
            journal.warning("%s : comparaison des commits impossible : %s", cc.nom, erreur)
            return f"{ecart}, et la comparaison des deux est impossible (git indisponible ?)"
        if nombre == 0:
            return ""      # les commits en plus ne touchent pas ce service
        return f"{nombre} commit(s) non déployé(s) touchent {', '.join(suivis)} ({ecart})"

    @staticmethod
    def _relatifs(depot, chemins):
        """Les chemins situés dans le dépôt, relatifs à lui, pour git. Les autres sont ignorés."""
        racine = Path(depot)
        relatifs = []
        for chemin in chemins:
            try:
                relatifs.append(Path(chemin).relative_to(racine).as_posix())
            except ValueError:
                continue
        return list(dict.fromkeys(relatifs)) or ["."]

    def _variables(self, n):
        """Les variables de construction. « label:X » reprend l'étiquette X de l'image en service.

        C'est ainsi que GIT_SHA garde le commit d'origine : l'image reconstruite
        porte le même, et rien ne la croit en retard sur le dépôt.
        """
        _, _, etiquettes, _ = self.docker.image(n.actuel.image_id)
        variables = {}
        for cle, valeur in n.conf.arguments.items():
            if not valeur.startswith("label:"):
                variables[cle] = valeur
            elif etiquettes.get(valeur[6:]):
                variables[cle] = etiquettes[valeur[6:]]
        return variables

    def _noter_reconstruction(self, n):
        """Après une reconstruction réussie, les bases fraîches deviennent la référence."""
        suivi = self.etat.conteneur(n.conf.nom)
        releve = suivi["construction"]
        try:
            releve["bases"] = self._releve_bases(list(releve["bases"]))
            releve["image_id"] = self.docker.conteneur(n.conf.nom).image_id
        except ErreurCommande as erreur:
            # Sans gravité : la passe suivante relèvera tout comme pour une image inconnue.
            journal.warning("%s : relevé après reconstruction impossible : %s", n.conf.nom, erreur)
            releve["image_id"] = None
        finale = next(reversed(releve["bases"]), None)
        if finale:
            suivi["version"] = self._libelle(finale, releve["bases"][finale]["version"])

    @staticmethod
    def _libelle(base, version):
        return f"{construction.nom_court(base)} {version or '?'}"

    def _bases_importantes(self, n):
        return [(base, de, vers) for base, de, vers, _ in n.changements
                if self._importante(analyser(base).etiquette, de, vers, n.conf.segments_majeurs)]

    # ======================================================================= passe
    def passe(self):
        """La passe complète : détecter, trier, télécharger, installer, prévenir."""
        bilan = Bilan()
        a_installer = []
        a_traiter = self.conteneurs_a_traiter()
        self._signaler_locaux(bilan)
        for cc in a_traiter:
            if cc.decouvert and not self.etat.deja_notifie(cc.nom, CLE_DECOUVERTE):
                bilan.nouveaux.append((cc.nom, self._texte_decouverte(cc), CLE_DECOUVERTE))
            try:
                n = self.examiner(cc)
            except ERREURS_EXAMEN as erreur:
                journal.error("%s : vérification impossible : %s", cc.nom, erreur)
                bilan.erreurs.append((cc.nom, f"vérification impossible : {erreur}", None))
                continue
            for avertissement in self.etat.conteneur(cc.nom)["avertissements"]:
                if not self.etat.deja_notifie(cc.nom, avertissement["cle"]):
                    bilan.avertissements.append((cc.nom, avertissement["message"], avertissement["cle"]))
            if n is None:
                journal.info("%s : à jour", cc.nom)
                self._plus_rien_n_attend(cc.nom)
                continue
            if self.mode_effectif(cc) == "manuel" or self.etat.blocage(cc.nom):
                journal.info("%s : nouvelle version %s, attend une action manuelle",
                             cc.nom, n.version_nouvelle)
                self._en_attente(n, bilan)
                continue
            if n.code:
                # « À corriger » plutôt que « à faire à la main » : le bouton de la
                # page n'y peut rien, seul un déploiement du code lève l'obstacle.
                journal.info("%s : reconstruction refusée, %s", cc.nom, n.code)
                self.etat.bloquer(cc.nom, "code", n.code)
                if not self.etat.deja_notifie(cc.nom, n.empreinte):
                    bilan.avertissements.append((cc.nom, (
                        f"{n.version_nouvelle} attend, mais {n.code} : déploie ce code "
                        f"avec ton outil habituel"), n.empreinte))
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
            raison, quoi = self._obtention(n)
            message = f"{quoi} impossible après {1 + len(self.conf.attentes_reessai)} essais"
            self.etat.bloquer(n.conf.nom, raison, message, n.derniere_erreur)
            bilan.erreurs.append((n.conf.nom, message, n.empreinte))

        self._terminer(bilan)
        return bilan

    def _signaler_locaux(self, bilan):
        """Chaque image maison laissée de côté est signalée UNE fois, avec sa raison.

        ⚠️ Sans ce message, elle vieillirait en silence : sa seule trace serait
        une étiquette « local » sur la page.
        """
        locaux = {nom: info["raison"] for nom, info in self.etat.donnees["ignores"].items()
                  if info["etiquette"] == "local"}
        # Un conteneur retiré puis revenu est signalé à nouveau
        signales = [nom for nom in self.etat.donnees.get("locaux_signales", []) if nom in locaux]
        self.etat.donnees["locaux_signales"] = signales
        bilan.locaux = [(nom, raison) for nom, raison in locaux.items() if nom not in signales]

    def _plus_rien_n_attend(self, nom):
        """À jour : un blocage n'a plus d'objet, la version attendue est en place.

        C'est le cas après une mise à jour faite par un autre moyen, ou un
        redéploiement par ton outil habituel.
        ⚠️ Sauf un retour arrière échoué : être à jour ne dit pas que le service marche.
        """
        blocage = self.etat.blocage(nom)
        if blocage and blocage["raison"] != "retour_arriere":
            self.etat.debloquer(nom)

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
                    self._obtenir(n)
                except ErreurConstruction as erreur:
                    # ⚠️ Pas de nouvel essai : reconstruire encore donnerait la même image.
                    self._refuser(n, str(erreur), bilan)
                    continue
                except ErreurCommande as erreur:
                    journal.warning("%s : %s %d/%d raté : %s", n.conf.nom, self._obtention(n)[1],
                                    numero, len(attentes), erreur.erreur)
                    n.derniere_erreur = erreur.erreur
                    rates.append(n)
                    continue
                self._installer(n, bilan)
            a_installer = rates
        return a_installer

    def _obtenir(self, n):
        """Télécharge la nouvelle image, ou reconstruit une image construite sur place.

        ⚠️ Dans les deux cas l'ancien conteneur tourne toujours : un échec ici
        n'interrompt pas le service une seconde.
        """
        if not n.conf.construction:
            self.docker.telecharger(n.actuel)
            return
        self.docker.construire(n.actuel, n.conf, self._variables(n))
        if not self.simulation:
            self._verifier_commit(n)

    def _verifier_commit(self, n):
        """L'image reconstruite doit porter le même commit que celle qui tourne.

        ⚠️ Sinon la variable qui le transmet est mal déclarée : l'image est
        peut-être bonne, mais plus rien ne saurait de quel code elle sort, et le
        garde-fou du code ne marcherait plus. On ne l'installe pas, et le nom de
        l'image est rendu à l'ancienne.
        """
        c = n.actuel
        _, _, avant, _ = self.docker.image(c.image_id)
        attendu = avant.get(construction.REVISION)
        if not construction.est_un_commit(attendu):
            return
        _, _, apres, _ = self.docker.image(c.image)
        obtenu = apres.get(construction.REVISION)
        if obtenu == attendu:
            return
        self._rendre_le_nom(c)
        raise ErreurConstruction(
            f"l'image reconstruite ne porte plus le commit {attendu[:7]} (étiquette : "
            f"« {obtenu or 'absente'} ») : vérifie « arguments » dans la configuration")

    def _rendre_le_nom(self, c):
        """Refait pointer le nom de l'image vers l'ancienne, qui tourne toujours.

        ⚠️ Sinon le prochain « docker compose up » installerait sans contrôle
        l'image qui vient d'être refusée.
        """
        try:
            self.docker.etiqueter(c.image_id, c.image)
        except ErreurCommande as erreur:
            journal.warning("%s : ancienne image non réétiquetée : %s", c.nom, erreur.erreur)

    def _refuser(self, n, message, bilan):
        """Une image reconstruite qu'on n'installe pas : le service tourne toujours sur l'ancienne."""
        journal.error("%s : %s", n.conf.nom, message)
        self.etat.bloquer(n.conf.nom, "construction", message)
        bilan.erreurs.append((n.conf.nom, message, n.empreinte))

    @staticmethod
    def _obtention(n):
        """(raison de blocage, mot pour les messages) selon la façon d'obtenir l'image."""
        return ("construction", "reconstruction") if n.conf.construction \
            else ("telechargement", "téléchargement")

    # ======================================================== une mise à jour
    def _installer(self, n, bilan):
        """Contrôle, arrêt, copie des données, recréation, vérification. Retour arrière si ça casse.

        ⚠️ L'ANCIEN CONTENEUR N'EST ARRÊTÉ QU'APRÈS LE TÉLÉCHARGEMENT. Si le
        registre ne répond pas, le service n'a pas été interrompu une seconde.
        """
        nom, c = n.conf.nom, n.actuel
        if n.conf.controle and not self._controler(n, bilan):
            return False
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
            if n.conf.construction:
                self._noter_reconstruction(n)
            else:
                suivi["version"], suivi["empreinte"] = n.version_nouvelle, n.empreinte
            suivi["disponible"] = None
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

    def _controler(self, n, bilan):
        """Lance le contrôle prévu sur la nouvelle image, l'ancienne tournant encore.

        Exemple : valider la configuration de Caddy. Une configuration invalide
        empêcherait la nouvelle version de démarrer, mais AUSSI l'ancienne au
        retour arrière : sans ce contrôle, le proxy entier tomberait.
        En cas d'échec, le nom de l'image est rendu à l'ancienne.
        """
        nom, c = n.conf.nom, n.actuel
        if self.simulation:
            journal.info("simulation, non exécuté : %s", " ".join(n.conf.controle))
            return True
        try:
            executer(n.conf.controle, delai=300)
            return True
        except (ErreurCommande, OSError) as erreur:
            detail = getattr(erreur, "erreur", "") or str(erreur)
        journal.error("%s : contrôle avant installation en échec : %s", nom, detail)
        self._rendre_le_nom(c)
        message = "le contrôle avant installation a échoué, rien n'a été touché"
        self.etat.bloquer(nom, "controle", message, detail)
        bilan.erreurs.append((nom, message, n.empreinte))
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
            # Après le téléchargement ou la reconstruction, la référence compose
            # (« …:latest ») désigne la nouvelle image. On la refait pointer vers
            # l'ancienne, puis on recrée : Compose relance exactement ce qui tournait avant.
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
        ⚠️ SAUF UN CODE DIFFÉRENT : l'outil ne déploie jamais de code, même à
        ta demande. C'est le rôle de ton outil de déploiement.
        """
        cc = self._trouver(nom)
        bilan = Bilan()
        n = self.examiner(cc)
        if n is None:
            journal.info("%s : déjà à jour", nom)
            self.etat.debloquer(nom)
        elif n.code:
            self.etat.bloquer(nom, "code", n.code)
            bilan.erreurs.append((nom, f"reconstruction refusée : {n.code}", n.empreinte))
        else:
            try:
                self._obtenir(n)
            except ErreurConstruction as erreur:
                self._refuser(n, str(erreur), bilan)
            except ErreurCommande as erreur:
                raison, quoi = self._obtention(n)
                self.etat.bloquer(nom, raison, f"{quoi} impossible", erreur.erreur)
                bilan.erreurs.append((nom, f"{quoi} impossible", n.empreinte))
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
        if cc.construction:
            return (f"construit sur place, suivi en {cc.mode} : reconstruit quand ses images "
                    f"de base reçoivent un correctif, toujours avec le code en service "
                    f"(dépôt {cc.depot})")
        protege = ", ".join(cc.donnees) if cc.donnees else "aucune donnée identifiée"
        return f"suivi en {cc.mode} ; copié avant chaque mise à jour : {protege}"

    def _texte_majeure(self, n):
        if n.conf.construction:
            details = self._bases_importantes(n)
            texte = "image de base à valider : " + ", ".join(
                f"{base} {de or '?'} → {vers or '?'}" for base, de, vers in details)
            if any(de is None or vers is None for _, de, vers in details):
                texte += " (version illisible, validation manuelle par prudence)"
            return texte
        segments = n.conf.segments_majeurs
        if versions.prefixe(n.version_actuelle, segments) is None \
                or versions.prefixe(n.version_nouvelle, segments) is None:
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
            self.etat.donnees.setdefault("locaux_signales", []).extend(nom for nom, _ in bilan.locaux)
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
            ("Nouveaux conteneurs :", [f"• {nom} : {message}" for nom, message, _ in bilan.nouveaux]
                                      + [f"• {nom} : {raison}" for nom, raison in bilan.locaux]),
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
            if bilan.nouveaux or bilan.locaux:
                parties.append(f"{len(bilan.nouveaux) + len(bilan.locaux)} nouveau(x) conteneur(s)")
            if bilan.avertissements:
                parties.append(f"{len(bilan.avertissements)} à corriger")
            titre, priorite = "Mises à jour : " + ", ".join(parties), 3
        return self.notificateur.envoyer(titre, "\n".join(lignes), priorite)
