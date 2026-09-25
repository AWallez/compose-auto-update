"""Tout ce qui parle au démon Docker et à Docker Compose.

Chaque conteneur porte les étiquettes posées par Compose au moment de sa
création : projet, service, dossier et fichiers compose. On les relit ici,
plutôt que de répéter dans la configuration ce que Docker sait déjà.

⚠️ EN SIMULATION, SEULES LES LECTURES S'EXÉCUTENT. Tout ce qui modifie passe
par `_agir()`, qui se contente alors d'écrire au journal ce qu'il aurait fait.
"""

import json
import logging
from dataclasses import dataclass, field

from .commande import executer

journal = logging.getLogger(__name__)


@dataclass
class Montage:
    genre: str              # « bind » (un dossier de la machine) ou « volume » (géré par Docker)
    source: str             # chemin sur la machine
    destination: str        # chemin vu depuis le conteneur
    ecriture: bool


@dataclass
class Conteneur:
    nom: str
    image: str              # référence déclarée, ex. lscr.io/linuxserver/radarr:latest
    image_id: str           # image réellement utilisée, « sha256:… »
    etat: str               # running, exited, restarting…
    redemarrages: int       # compteur tenu par Docker
    sante: str | None       # healthy, unhealthy, starting, ou None si l'image n'a pas de sonde
    projet: str             # vide si le conteneur n'a pas été créé par Compose
    service: str
    dossier: str
    fichiers: list
    ephemere: bool = False  # lancé avec « docker run --rm » : il disparaîtra tout seul
    montages: list = field(default_factory=list)


def _lire(brut):
    """Traduit la réponse brute de `docker inspect` en Conteneur."""
    etiquettes = brut["Config"].get("Labels") or {}
    fichiers = etiquettes.get("com.docker.compose.project.config_files", "")
    return Conteneur(
        nom=brut["Name"].lstrip("/"),
        image=brut["Config"]["Image"],
        image_id=brut["Image"],
        etat=brut["State"]["Status"],
        redemarrages=brut.get("RestartCount", 0),
        sante=(brut["State"].get("Health") or {}).get("Status"),
        projet=etiquettes.get("com.docker.compose.project", ""),
        service=etiquettes.get("com.docker.compose.service", ""),
        dossier=etiquettes.get("com.docker.compose.project.working_dir", ""),
        fichiers=[f for f in fichiers.split(",") if f],
        ephemere=bool((brut.get("HostConfig") or {}).get("AutoRemove")),
        montages=[Montage(m.get("Type", ""), m.get("Source", ""), m.get("Destination", ""),
                          bool(m.get("RW"))) for m in brut.get("Mounts") or []],
    )


class Docker:
    def __init__(self, simulation=False):
        self.simulation = simulation

    # ============================================ lectures, toujours exécutées
    def plateforme(self):
        """« linux/amd64 » : pour choisir la bonne image dans un index multi-architecture."""
        return executer(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]).strip()

    def conteneur(self, nom):
        return _lire(json.loads(executer(["docker", "inspect", "--type", "container", nom]))[0])

    def tous(self):
        """Tous les conteneurs en marche, lus en un seul appel à `docker inspect`."""
        noms = executer(["docker", "ps", "--format", "{{.Names}}"]).split()
        if not noms:
            return []
        return [_lire(b) for b in json.loads(executer(["docker", "inspect", "--type", "container", *noms]))]

    def image(self, reference_ou_id):
        """Identifiant, empreintes connues, étiquettes et environnement d'une image locale.

        Une image sans aucune empreinte de registre n'a jamais été téléchargée :
        elle a été construite sur place, et aucun registre ne peut la mettre à jour.
        """
        brut = json.loads(executer(["docker", "image", "inspect", reference_ou_id]))[0]
        empreintes = [d.split("@", 1)[1] for d in brut.get("RepoDigests") or [] if "@" in d]
        config = brut.get("Config") or {}
        return brut["Id"], empreintes, config.get("Labels") or {}, config.get("Env") or []

    def dockerfile(self, c, construction):
        """Le texte du Dockerfile d'une image construite sur place.

        `construction` vaut « compose » (on demande à Compose où il construit ce
        service) ou le chemin du dossier de construction.
        """
        if construction == "compose":
            # ⚠️ Lecture seule, mais la configuration résolue contient les valeurs
            # du .env : on n'en garde QUE le chemin de construction, rien n'est
            # affiché ni enregistré.
            config = json.loads(executer(self._compose(c, "config", "--format", "json"), delai=120))
            build = config["services"][c.service]["build"]
            dossier = build["context"]
            fichier = build.get("dockerfile") or "Dockerfile"
            chemin = fichier if fichier.startswith("/") else f"{dossier}/{fichier}"
        else:
            chemin = f"{construction}/Dockerfile"
        with open(chemin, encoding="utf-8") as f:
            return f.read()

    # ======================================= actions, neutralisées en simulation
    def _agir(self, arguments, delai=900, env=None):
        if self.simulation:
            journal.info("simulation, non exécuté : %s%s", " ".join(arguments),
                         f" (variables : {env})" if env else "")
            return ""
        return executer(arguments, delai, env)

    def _compose(self, c, *action):
        """La commande compose exacte du projet d'origine : même nom, même dossier, mêmes fichiers.

        ⚠️ `--project-directory` compte : c'est là que Compose trouve le `.env`
        du projet. Sans lui, les variables seraient vides et le conteneur
        recréé différemment de l'original.
        """
        commande = ["docker", "compose", "--project-name", c.projet,
                    "--project-directory", c.dossier]
        for fichier in c.fichiers:
            commande += ["--file", fichier]
        return commande + list(action)

    def telecharger(self, c):
        self._agir(self._compose(c, "pull", "--quiet", c.service), delai=1800)

    def construire(self, c, cc, variables):
        """Reconstruit l'image d'un conteneur construit sur place, avec des bases fraîches.

        ⚠️ `--pull` : sans lui, Docker réutilise la base gardée en cache depuis la
        dernière construction, et la reconstruction ne corrige rien.
        `variables` (par exemple le commit, GIT_SHA) passent en variables
        d'environnement pour Compose, qui les substitue dans son fichier, et en
        `--build-arg` pour une construction directe.
        """
        if cc.construction == "compose":
            self._agir(self._compose(c, "build", "--pull", c.service), delai=3600, env=variables)
            return
        commande = ["docker", "build", "--pull", "--tag", c.image]
        if cc.reseau_construction:
            commande += ["--network", cc.reseau_construction]
        for cle, valeur in variables.items():
            commande += ["--build-arg", f"{cle}={valeur}"]
        self._agir(commande + [cc.construction], delai=3600)

    def arreter(self, c):
        self._agir(["docker", "stop", c.nom], delai=300)

    def recreer(self, c):
        """Recrée le conteneur à partir de sa définition compose.

        ⚠️ `--no-deps` : on ne touche QU'À ce service. Sans lui, recréer
        qbittorrent pourrait recréer aussi gluetun, dont il dépend, et couper le
        VPN pour rien.
        ⚠️ `--pull never` et `--no-build` : l'image voulue est déjà là,
        téléchargée ou construite juste avant, ou remise en place par le retour
        arrière. Compose ne doit ni en télécharger ni en construire une autre.
        """
        self._agir(self._compose(c, "up", "--detach", "--no-deps", "--pull", "never",
                                 "--no-build", c.service), delai=600)

    def etiqueter(self, image_id, reference):
        self._agir(["docker", "tag", image_id, reference])

    def nettoyer_images(self):
        """Supprime les images orphelines, et SEULEMENT elles.

        ⚠️ `--force` sans `--all` : une image encore étiquetée, comme la copie de
        secours « :avant-maj », n'est jamais touchée.
        """
        self._agir(["docker", "image", "prune", "--force"])
