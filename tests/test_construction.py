"""Tests des images construites sur place : surveillance des bases, reconstruction, garde-fous.

Le décor imite le portfolio : un dépôt git (de vrais fichiers, dans un dossier
temporaire) dont le dossier « frontend » construit l'image « web », à partir de
node (étape de construction) et de nginx (image finale), avec le commit
d'origine dans l'étiquette OCI « revision ».
"""

import dataclasses
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from compose_auto_update import config, construction, sante, versions
from compose_auto_update.commande import ErreurCommande
from compose_auto_update.config import Conf, ConfConteneur
from compose_auto_update.docker import Conteneur, Docker
from compose_auto_update.etat import Etat
from compose_auto_update.image import analyser
from compose_auto_update.moteur import Moteur

from .test_moteur import FauxDocker, FauxNotificateur, monter

REVISION = construction.REVISION
COMMIT = "1dd7e92" + "a" * 33
AUTRE_COMMIT = "b32e021" + "b" * 33
GIT = "compose_auto_update.construction.commits_touchant"

DOCKERFILE_WEB = """\
FROM node:22-alpine AS build
WORKDIR /app
RUN npm ci && npm run build
FROM nginx:stable-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
"""
NGINX, NODE = "nginx:stable-alpine", "node:22-alpine"


def depot(commit=COMMIT, dockerfile=DOCKERFILE_WEB):
    """Un dépôt git minimal : .git/HEAD, la branche, frontend/Dockerfile, infra/docker-compose.yml."""
    racine = Path(tempfile.mkdtemp()) / "portfolio"
    (racine / ".git" / "refs" / "heads").mkdir(parents=True)
    (racine / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (racine / ".git" / "refs" / "heads" / "main").write_text(commit + "\n", encoding="utf-8")
    (racine / "frontend").mkdir()
    (racine / "frontend" / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (racine / "infra").mkdir()
    (racine / "infra" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return racine


class FauxDockerConstruction(FauxDocker):
    """Un Docker qui sait construire : les étiquettes d'image suivent les constructions."""

    def __init__(self):
        super().__init__()
        self.plans = {}                       # conteneur → dossier de construction
        self.tags = {}                        # nom d'image → identifiant
        self.echecs_construction = 0
        self.perd_le_commit = False           # la variable du commit n'arrive pas au Dockerfile
        self.bases_du_registre = {}           # ce que « build --pull » rapportera dans le cache

    def mettre_en_cache(self, base, empreinte, version):
        nom = construction.nom_court(base).upper()
        self.images[base] = (f"sha256:base-{empreinte}", [empreinte], {}, [f"{nom}_VERSION={version}"])

    def image(self, reference):
        if reference not in self.images:
            raise ErreurCommande(["docker", "image", "inspect", reference], 1, "No such image")
        return self.images[reference]

    def plan_de_construction(self, c, construction_):
        if c.nom not in self.plans:
            raise KeyError(f"le service {c.service} n'a pas de section « build »")
        return str(self.plans[c.nom]), str(self.plans[c.nom] / "Dockerfile")

    def construire(self, c, cc, variables):
        self.actions.append(f"construire {c.nom} {variables}")
        if self.echecs_construction:
            self.echecs_construction -= 1
            raise ErreurCommande(["docker", "compose", "build"], 1, "npm ERR! network timeout")
        for base, (empreinte, version) in self.bases_du_registre.items():
            self.mettre_en_cache(base, empreinte, version)
        nouvelle = f"sha256:reconstruite-{len(self.actions)}"
        commit = "unknown" if self.perd_le_commit else variables.get("GIT_SHA", "unknown")
        self.images[nouvelle] = (nouvelle, [], {REVISION: commit}, [])
        self.images[c.image] = self.images[nouvelle]
        self.tags[c.image] = nouvelle

    def etiqueter(self, image_id, reference):
        super().etiqueter(image_id, reference)
        self.tags[reference] = image_id
        self.images[reference] = self.images.get(image_id)

    def recreer(self, c):
        super().recreer(c)
        if c.image in self.tags:
            self.conteneurs[c.nom] = dataclasses.replace(self.conteneurs[c.nom],
                                                         image_id=self.tags[c.image])

    def lancer(self, nom, image, image_id, commit, contexte, fichiers=()):
        self.conteneurs[nom] = Conteneur(
            nom=nom, image=image, image_id=image_id, etat="running", redemarrages=0, sante=None,
            projet="infra", service=nom, dossier="/srv/infra", fichiers=list(fichiers))
        self.images[image_id] = (image_id, [], {REVISION: commit}, [])
        self.images[image] = self.images[image_id]
        self.plans[nom] = contexte

    def deployer(self, commit, **bases):
        """Ce que fait l'outil de déploiement : nouveau code, bases fraîches, nouvelle image."""
        for base, (empreinte, version) in bases.items():
            self.mettre_en_cache(base, empreinte, version)
        nouvelle = f"sha256:deployee-{commit[:7]}"
        self.images[nouvelle] = (nouvelle, [], {REVISION: commit}, [])
        self.conteneurs["web"] = dataclasses.replace(self.conteneurs["web"], image_id=nouvelle)


class RegistreDesBases:
    """Le registre : pour chaque base, l'empreinte et la version publiées aujourd'hui."""

    def __init__(self, bases):
        self.bases = bases

    def _publiee(self, ref):
        return next(v for base, v in self.bases.items() if analyser(base) == ref)

    def empreinte(self, ref):
        return self._publiee(ref)[0]

    def configuration(self, ref, empreinte, plateforme):
        nom = ref.depot.rsplit("/", 1)[-1].upper()
        return {}, [f"{nom}_VERSION={self._publiee(ref)[1]}"]


def monter_web(nginx=("sha256:nginx-a", "1.30.5"), node=("sha256:node-a", "22.23.3"),
               commit_depot=COMMIT, revision=COMMIT, controle=(), configure=True,
               arguments=None):
    """Un « web » construit au commit `revision`, et un dépôt au commit `commit_depot`.

    Le cache local contient les bases de la construction (nginx-a 1.30.5,
    node-a 22.23.3) ; le registre publie `nginx` et `node`. Avec
    `configure=False`, le conteneur n'est pas dans la configuration : il est découvert.
    """
    racine = depot(commit_depot)
    conteneurs = []
    if configure:
        conteneurs.append(ConfConteneur(
            "web", "auto", construction="compose", depot=str(racine), segments_majeurs=2,
            arguments={"GIT_SHA": f"label:{REVISION}"} if arguments is None else arguments,
            controle=list(controle)))
    dossier = Path(tempfile.mkdtemp())
    conf = Conf(fichier_etat=str(dossier / "etat.json"), exports=[],
                dossier_copies=str(dossier / "copies"), copies_conservees=3,
                observation=0, delai_sante=0, attentes_reessai=[300, 1500],
                ntfy_url="", ntfy_sujet="", ntfy_jeton="", lien="", kuma_push="",
                identifiants={}, conteneurs=conteneurs)
    docker = FauxDockerConstruction()
    docker.lancer("web", "portfolio-infra-web", "sha256:id-web", revision, racine / "frontend",
                  fichiers=[str(racine / "infra" / "docker-compose.yml")])
    docker.mettre_en_cache(NODE, "sha256:node-a", "22.23.3")
    docker.mettre_en_cache(NGINX, "sha256:nginx-a", "1.30.5")
    registre = RegistreDesBases({NODE: node, NGINX: nginx})
    docker.bases_du_registre = registre.bases
    moteur = Moteur(conf, docker, registre, Etat(conf.fichier_etat), FauxNotificateur(),
                    attendre=lambda secondes: None)
    moteur.racine = racine
    return moteur, docker


def constructions(docker):
    return [a for a in docker.actions if a.startswith("construire")]


class Reconstruction(unittest.TestCase):
    def test_bases_inchangees_rien_ne_se_passe(self):
        moteur, docker = monter_web()
        bilan = moteur.passe()
        self.assertFalse(constructions(docker))
        self.assertEqual(moteur.etat.conteneur("web")["version"], "nginx 1.30.5")
        self.assertFalse(bilan.erreurs)
        self.assertEqual(moteur.notificateur.envois, [])

    def test_correctif_nginx_reconstruit_avec_le_meme_commit(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"))
        bilan = moteur.passe()
        self.assertEqual(constructions(docker), [f"construire web {{'GIT_SHA': '{COMMIT}'}}"])
        self.assertIn("etiqueter sha256:id-web portfolio-infra-web:avant-maj", docker.actions)
        self.assertEqual(bilan.mis_a_jour, [("web", "nginx 1.30.5", "nginx 1.30.6")])
        self.assertEqual(moteur.etat.conteneur("web")["version"], "nginx 1.30.6")
        self.assertEqual(moteur.notificateur.envois, [])     # une réussite ne notifie pas
        moteur.passe()                                        # le lendemain : rien à refaire
        self.assertEqual(len(constructions(docker)), 1)

    def test_meme_version_image_corrigee(self):
        # nginx republié avec des paquets Alpine corrigés : même numéro, autre image
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.5"))
        bilan = moteur.passe()
        self.assertEqual(bilan.mis_a_jour, [("web", "nginx 1.30.5", "nginx 1.30.5 (image corrigée)")])
        self.assertEqual(moteur.etat.conteneur("web")["version"], "nginx 1.30.5")

    def test_seule_la_base_de_construction_change(self):
        # node:22-alpine fige la majeure : 22.23 → 22.24 n'est pas « importante »,
        # même avec segments_majeurs = 2, prévu pour les branches de nginx
        moteur, docker = monter_web(node=("sha256:node-b", "22.24.0"))
        bilan = moteur.passe()
        self.assertEqual(bilan.mis_a_jour, [("web", "nginx 1.30.5", "nginx 1.30.5 + node 22.24.0")])

    def test_nouvelle_branche_nginx_attend_ton_accord(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.32.0"))
        moteur.passe()
        self.assertFalse(constructions(docker))
        blocage = moteur.etat.blocage("web")
        self.assertEqual(blocage["raison"], "majeure")
        self.assertIn("nginx:stable-alpine 1.30.5 → 1.32.0", blocage["message"])
        bilan = moteur.appliquer("web")                       # ton accord, depuis la page
        self.assertEqual(len(bilan.mis_a_jour), 1)
        self.assertIsNone(moteur.etat.blocage("web"))

    def test_base_absente_du_cache_prudence(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"))
        del docker.images[NGINX]
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("web")["raison"], "majeure")
        self.assertIn("illisible", moteur.etat.blocage("web")["message"])

    def test_reconstruction_ratee_trois_fois_service_intact(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"))
        docker.echecs_construction = 3
        bilan = moteur.passe()
        self.assertNotIn("arreter web", docker.actions)
        blocage = moteur.etat.blocage("web")
        self.assertEqual(blocage["raison"], "construction")
        self.assertIn("npm ERR!", blocage["erreur"])
        self.assertIn("reconstruction impossible", bilan.erreurs[0][1])

    def test_nouvelle_image_cassee_retour_arriere(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"))
        docker.etats_apres_recreation = ["exited", "running"]
        bilan = moteur.passe()
        self.assertEqual(docker.conteneurs["web"].image_id, "sha256:id-web")   # l'ancienne tourne
        self.assertEqual(moteur.etat.blocage("web")["raison"], "installation")
        self.assertFalse(bilan.urgent)
        moteur.passe()                   # le lendemain : toujours en attente, pas de 2e notification
        self.assertEqual(len(constructions(docker)), 1)
        self.assertEqual(len(moteur.notificateur.envois), 1)


class CommitDeLImageReconstruite(unittest.TestCase):
    def test_commit_perdu_rien_n_est_installe(self):
        # la variable du commit n'atteint pas le Dockerfile : l'image dirait « unknown »
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"))
        docker.perd_le_commit = True
        bilan = moteur.passe()
        self.assertNotIn("arreter web", docker.actions)
        self.assertEqual(docker.tags["portfolio-infra-web"], "sha256:id-web")   # nom rendu
        self.assertEqual(len(constructions(docker)), 1)       # pas de nouvel essai : inutile
        self.assertEqual(moteur.etat.blocage("web")["raison"], "construction")
        self.assertIn("ne porte plus le commit 1dd7e92", bilan.erreurs[0][1])

    def test_sans_argument_le_commit_se_perd_aussi(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), arguments={})
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("web")["raison"], "construction")


class GardeFouDuCode(unittest.TestCase):
    def test_commits_en_plus_qui_touchent_le_service_bloquent(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), commit_depot=AUTRE_COMMIT)
        with mock.patch(GIT, return_value=2) as git:
            bilan = moteur.passe()
        depot_, depuis, jusqu_a, chemins = git.call_args[0]
        self.assertEqual((depuis, jusqu_a), (COMMIT, AUTRE_COMMIT))
        self.assertEqual(chemins, ["frontend", "frontend/Dockerfile", "infra/docker-compose.yml"])
        self.assertFalse(constructions(docker))
        blocage = moteur.etat.blocage("web")
        self.assertEqual(blocage["raison"], "code")
        self.assertIn("2 commit(s) non déployé(s)", blocage["message"])
        self.assertIn("b32e021", blocage["message"])
        self.assertIn("déploie", bilan.avertissements[0][1])
        # ⚠️ même ta demande depuis la page ne déploie pas de code
        with mock.patch(GIT, return_value=2):
            bilan = moteur.appliquer("web")
        self.assertFalse(constructions(docker))
        self.assertIn("reconstruction refusée", bilan.erreurs[0][1])

    def test_commits_en_plus_ailleurs_ne_bloquent_pas(self):
        # « deploy.sh api » a récupéré un commit qui ne touche que l'api : le site
        # se reconstruit, et garde SON commit d'origine
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), commit_depot=AUTRE_COMMIT)
        with mock.patch(GIT, return_value=0):
            bilan = moteur.passe()
        self.assertEqual(constructions(docker), [f"construire web {{'GIT_SHA': '{COMMIT}'}}"])
        self.assertEqual(len(bilan.mis_a_jour), 1)

    def test_sans_git_dans_le_doute_on_ne_reconstruit_pas(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), commit_depot=AUTRE_COMMIT)
        with mock.patch(GIT, side_effect=ErreurCommande(["git"], 128, "bad revision")):
            moteur.passe()
        self.assertFalse(constructions(docker))
        self.assertIn("comparaison des deux est impossible", moteur.etat.blocage("web")["message"])

    def test_le_deploiement_leve_le_blocage_tout_seul(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), commit_depot=AUTRE_COMMIT)
        with mock.patch(GIT, return_value=2):
            moteur.passe()
        docker.deployer(AUTRE_COMMIT, **{NGINX: ("sha256:nginx-b", "1.30.6"),
                                         NODE: ("sha256:node-a", "22.23.3")})
        moteur.passe()
        self.assertIsNone(moteur.etat.blocage("web"))
        self.assertEqual(moteur.etat.conteneur("web")["version"], "nginx 1.30.6")
        self.assertFalse(constructions(docker))

    def test_image_sans_commit_jamais_reconstruite(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), revision="unknown")
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("web")["raison"], "code")
        self.assertIn("unknown", moteur.etat.blocage("web")["message"])

    def test_dockerfile_modifie_depuis_la_construction(self):
        moteur, docker = monter_web()
        moteur.passe()                                       # relevé de référence
        dockerfile = moteur.racine / "frontend" / "Dockerfile"
        dockerfile.write_text(DOCKERFILE_WEB + "RUN echo nouveau\n", encoding="utf-8")
        docker.bases_du_registre[NGINX] = ("sha256:nginx-b", "1.30.6")
        moteur._distantes.clear()                            # une nouvelle passe, un nouveau jour
        moteur.passe()
        self.assertFalse(constructions(docker))
        self.assertIn("Dockerfile", moteur.etat.blocage("web")["message"])


class ImagesMaisonDecouvertes(unittest.TestCase):
    """Un conteneur construit sur place, absent de la configuration."""

    def test_tout_est_verifiable_suivi_automatiquement(self):
        moteur, docker = monter_web(configure=False)
        cc = next(c for c in moteur.conteneurs_a_traiter() if c.nom == "web")
        self.assertEqual((cc.mode, cc.construction, cc.depot), ("auto", "compose", str(moteur.racine)))
        self.assertEqual(cc.arguments, {"GIT_SHA": f"label:{REVISION}"})
        bilan = moteur.passe()
        self.assertIn("construit sur place", bilan.nouveaux[0][1])
        # le lendemain, nginx reçoit un correctif : reconstruit seul, même commit
        docker.bases_du_registre[NGINX] = ("sha256:nginx-b", "1.30.6")
        moteur._distantes.clear()
        bilan = moteur.passe()
        self.assertEqual(constructions(docker), [f"construire web {{'GIT_SHA': '{COMMIT}'}}"])
        self.assertEqual(len(bilan.mis_a_jour), 1)

    def verifier_ignore(self, moteur, fragment):
        self.assertNotIn("web", [c.nom for c in moteur.conteneurs_a_traiter()])
        self.assertIn(fragment, moteur.etat.donnees["ignores"]["web"]["raison"])
        moteur.passe()
        self.assertIn(fragment, moteur.notificateur.envois[0][1])
        moteur.passe()
        self.assertEqual(len(moteur.notificateur.envois), 1)     # signalé une seule fois

    def test_image_sans_commit_signalee(self):
        moteur, _ = monter_web(configure=False, revision="unknown")
        self.verifier_ignore(moteur, "ne dit pas de quel commit")

    def test_dockerfile_qui_n_inscrit_pas_le_commit_signale(self):
        moteur, _ = monter_web(configure=False)
        (moteur.racine / "frontend" / "Dockerfile").write_text(
            "FROM nginx:stable-alpine\n", encoding="utf-8")
        self.verifier_ignore(moteur, "n'inscrit pas le commit")

    def test_hors_depot_git_signale(self):
        moteur, _ = monter_web(configure=False)
        (moteur.racine / ".git" / "HEAD").unlink()
        self.verifier_ignore(moteur, "dépôt git")


class CommandesDeConstruction(unittest.TestCase):
    def conteneur(self, image="portfolio-infra-web"):
        return Conteneur(nom="portfolio-web", image=image, image_id="sha256:ancien",
                         etat="running", redemarrages=0, sante=None, projet="portfolio-infra",
                         service="web", dossier="/srv/infra", fichiers=["/srv/infra/compose.yml"])

    def test_compose_recoit_le_commit_de_deux_facons(self):
        cc = ConfConteneur("portfolio-web", "auto", construction="compose")
        with mock.patch("compose_auto_update.docker.executer") as executer:
            Docker().construire(self.conteneur(), cc, {"GIT_SHA": COMMIT})
        arguments, delai, env = executer.call_args[0]
        self.assertEqual(arguments[-4:], ["build", "--pull", f"--build-arg=GIT_SHA={COMMIT}", "web"])
        self.assertEqual(env, {"GIT_SHA": COMMIT})

    def test_construction_directe(self):
        cc = ConfConteneur("portfolio-caddy", "auto", construction="/srv/caddy",
                           reseau_construction="host")
        with mock.patch("compose_auto_update.docker.executer") as executer:
            Docker().construire(self.conteneur("caddy-ratelimit:local"), cc, {})
        self.assertEqual(executer.call_args[0][0], ["docker", "build", "--pull", "--tag",
                                                    "caddy-ratelimit:local", "--network", "host",
                                                    "/srv/caddy"])


class ControleAvantInstallation(unittest.TestCase):
    def test_controle_rate_rien_n_est_touche(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), controle=["valider"])
        with mock.patch("compose_auto_update.moteur.executer",
                        side_effect=ErreurCommande(["valider"], 1, "Caddyfile:12 : directive inconnue")):
            bilan = moteur.passe()
        self.assertNotIn("arreter web", docker.actions)
        self.assertEqual(docker.tags["portfolio-infra-web"], "sha256:id-web")   # nom rendu à l'ancienne
        blocage = moteur.etat.blocage("web")
        self.assertEqual(blocage["raison"], "controle")
        self.assertIn("Caddyfile:12", blocage["erreur"])
        self.assertEqual(len(bilan.erreurs), 1)

    def test_controle_reussi_installation_normale(self):
        moteur, docker = monter_web(nginx=("sha256:nginx-b", "1.30.6"), controle=["valider"])
        with mock.patch("compose_auto_update.moteur.executer", return_value="Valid configuration"):
            bilan = moteur.passe()
        self.assertEqual(len(bilan.mis_a_jour), 1)


class BlocagePerime(unittest.TestCase):
    def test_a_jour_le_blocage_se_leve(self):
        moteur, _, _ = monter(deja_a_jour=True)
        moteur.etat.bloquer("radarr", "majeure", "version majeure : 6.4.4 → 7.0.0")
        moteur.passe()
        self.assertIsNone(moteur.etat.blocage("radarr"))

    def test_retour_arriere_echoue_reste_signale(self):
        moteur, _, _ = monter(deja_a_jour=True)
        moteur.etat.bloquer("radarr", "retour_arriere", "retour arrière échoué")
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "retour_arriere")


class LectureDuDockerfile(unittest.TestCase):
    def test_bases_sans_etapes_ni_scratch(self):
        texte = ("FROM --platform=$BUILDPLATFORM golang:1.26 AS outils\n"
                 "FROM outils AS build\n"
                 "from scratch\n"
                 "FROM node:${NODE_VERSION}-alpine\n"
                 "FROM caddy:2-alpine\n"
                 "FROM caddy:2-alpine\n")
        self.assertEqual(construction.bases(texte), ["golang:1.26", "caddy:2-alpine"])
        self.assertEqual(construction.inconnues(texte), ["node:${NODE_VERSION}-alpine"])

    def test_argument_qui_inscrit_le_commit(self):
        self.assertEqual(construction.argument_de_revision(DOCKERFILE_WEB), "GIT_SHA")
        self.assertEqual(construction.argument_de_revision(
            'LABEL a=b \\\n    org.opencontainers.image.revision="${REV}"\n'), "REV")
        self.assertIsNone(construction.argument_de_revision("FROM nginx\n"))

    def test_est_un_commit(self):
        self.assertTrue(construction.est_un_commit("1dd7e92"))
        self.assertTrue(construction.est_un_commit(COMMIT))
        for texte in ("unknown", "", None, "1dd7e9", "1DD7E92"):
            self.assertFalse(construction.est_un_commit(texte), texte)

    def test_version_de_base(self):
        self.assertEqual(construction.version_de_base(
            "library/nginx", {}, ["PATH=/usr/bin", "NGINX_VERSION=1.30.5"]), "1.30.5")
        self.assertEqual(construction.version_de_base(
            "x/outil", {"org.opencontainers.image.version": "3.1"}, []), "3.1")
        self.assertIsNone(construction.version_de_base("x/outil", {}, []))

    def test_nom_court(self):
        self.assertEqual(construction.nom_court("nginx:stable-alpine"), "nginx")
        self.assertEqual(construction.nom_court("ghcr.io/x/caddy:2@sha256:abc"), "caddy")

    def test_prefixe(self):
        self.assertEqual(versions.prefixe("1.30.5", 2), (1, 30))
        self.assertEqual(versions.prefixe("v2.10.2", 1), (2,))
        self.assertIsNone(versions.prefixe("7", 2))
        self.assertIsNone(versions.prefixe(None))


class Depot(unittest.TestCase):
    def depot(self, head, fichiers=()):
        git = Path(tempfile.mkdtemp()) / ".git"
        git.mkdir()
        (git / "HEAD").write_text(head, encoding="utf-8")
        for chemin, contenu in fichiers:
            (git / chemin).parent.mkdir(parents=True, exist_ok=True)
            (git / chemin).write_text(contenu, encoding="utf-8")
        return str(git.parent)

    def test_branche(self):
        depot_ = self.depot("ref: refs/heads/main\n", [("refs/heads/main", COMMIT + "\n")])
        self.assertEqual(construction.commit_du_depot(depot_), COMMIT)

    def test_references_tassees(self):
        # après un « git gc », la branche n'a plus de fichier à elle
        depot_ = self.depot("ref: refs/heads/main\n", [("packed-refs", (
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{AUTRE_COMMIT} refs/heads/autre\n{COMMIT} refs/heads/main\n"))])
        self.assertEqual(construction.commit_du_depot(depot_), COMMIT)

    def test_tete_detachee(self):
        self.assertEqual(construction.commit_du_depot(self.depot(COMMIT + "\n")), COMMIT)

    def test_depot_trouve_en_remontant(self):
        racine = depot()
        self.assertEqual(construction.depot_de(racine / "frontend"), str(racine))
        self.assertIsNone(construction.depot_de(tempfile.mkdtemp()))

    def test_git_de_la_machine_ou_d_un_conteneur(self):
        with mock.patch("compose_auto_update.construction.executer", return_value="3\n") as executer, \
                mock.patch("compose_auto_update.construction.shutil.which", return_value=None):
            n = construction.commits_touchant("/srv/depot", "aaa", "bbb", ["frontend"])
        arguments = executer.call_args[0][0]
        self.assertEqual(n, 3)
        self.assertIn("/srv/depot:/depot:ro", arguments)           # lecture seule
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertEqual(arguments[-5:], ["rev-list", "--count", "aaa..bbb", "--", "frontend"])


class ConfigurationDeConstruction(unittest.TestCase):
    BASE = '[general]\ndossier_copies = "/srv/copies"\n[[conteneur]]\nnom = "web"\nmode = "auto"\n'

    def charger(self, texte):
        chemin = Path(tempfile.mkdtemp()) / "config.toml"
        chemin.write_text(self.BASE + texte, encoding="utf-8")
        return config.charger(str(chemin))

    def test_valide(self):
        cc = self.charger('construction = "compose"\ndepot = "/srv/portfolio"\nsegments_majeurs = 2\n'
                          'arguments = { GIT_SHA = "label:org.opencontainers.image.revision" }\n'
                          'controle = ["docker", "run", "--rm", "caddy", "validate"]\n').conteneurs[0]
        self.assertEqual((cc.construction, cc.segments_majeurs), ("compose", 2))
        self.assertEqual(cc.arguments["GIT_SHA"], "label:org.opencontainers.image.revision")
        self.assertEqual(cc.controle[-1], "validate")

    def test_depot_sans_construction_refuse(self):
        with self.assertRaisesRegex(config.ErreurConfig, "construction"):
            self.charger('depot = "/srv/portfolio"\n')

    def test_controle_en_ligne_de_shell_refuse(self):
        with self.assertRaisesRegex(config.ErreurConfig, "liste"):
            self.charger('controle = "caddy validate && echo ok"\n')

    def test_dossier_de_construction_relatif_refuse(self):
        with self.assertRaisesRegex(config.ErreurConfig, "absolu"):
            self.charger('construction = "infra/caddy"\n')


class SanteSansRedirection(unittest.TestCase):
    """Caddy répond 308 vers https : c'est une réponse, le service est vivant."""

    def test_une_redirection_est_une_reponse(self):
        class Redirige(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(308)
                self.send_header("Location", "https://injoignable.invalid:8443/")
                self.end_headers()

            def log_message(self, *args):
                pass

        serveur = http.server.HTTPServer(("127.0.0.1", 0), Redirige)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        try:
            adresse = f"http://127.0.0.1:{serveur.server_address[1]}/"
            ok, raison = sante._repond(adresse, 5, 1, lambda s: None, iter(range(100)).__next__)
        finally:
            serveur.shutdown()
            serveur.server_close()
        self.assertTrue(ok, raison)


if __name__ == "__main__":
    unittest.main()
