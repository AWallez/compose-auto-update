# compose-auto-update

**Mises à jour automatiques de conteneurs Docker Compose.**

Chaque nuit, l'outil regarde si une nouvelle image existe pour chaque conteneur, installe celles qu'il a le droit d'installer, vérifie que le service fonctionne encore, et remet l'ancienne version, données comprises, si ce n'est pas le cas. Les images construites sur place sont reconstruites quand leurs images de base reçoivent un correctif, toujours avec le code déjà en service. Il ne te dérange que lorsqu'une action de ta part est nécessaire.

Python 3.11, bibliothèque standard uniquement : rien à installer.

## Pourquoi cet outil existe

Il remplace un script qui confiait le travail à Watchtower puis **devinait le résultat en lisant son journal**. Cette supervision s'est trompée trois fois, toujours de la même façon : l'outil tiers changeait sa manière d'écrire (`level=warn` au lieu de `level=error`, `skipped=` au lieu de `failed=`), et des semaines de mises à jour ratées passaient pour des réussites.

La dernière enquête a aussi montré qu'une heure ronde est une mauvaise heure. Une sonde lancée toutes les deux secondes a mesuré que `lscr.io`, la passerelle des images LinuxServer, **s'effondrait à 4 h 00 min 01 s pile** (4,8 s puis 15 s sans réponse), alors que `ghcr.io`, Docker Hub et un témoin répondaient normalement à la même seconde.

D'où trois principes :

- **Ne jamais deviner.** Chaque étape est une commande dont le code de retour dit si elle a réussi.
- **Un retour arrière remet l'image ET les données.** Beaucoup d'applications convertissent leur base au premier démarrage : l'ancienne image seule ne sait plus la lire.
- **Le silence doit être fiable.** Aucune notification quand tout va bien, mais un signal de vie à chaque passe : si la passe ne s'est pas signalée, c'est Uptime Kuma qui prévient.

## Ce que fait une passe

Pour chaque conteneur, un à la fois :

1. **Nouvelle version ?** L'empreinte de l'image est demandée au registre, sans rien télécharger. Si elle a changé, on lit aussi le numéro de la nouvelle version. Pour une image construite sur place, ce sont ses images de base qui sont comparées.
2. **Tri.** Un conteneur manuel, bloqué, ou face à une montée de version majeure n'est pas touché : il est signalé.
3. **Téléchargement** (ou **reconstruction**), pendant que l'ancien conteneur continue de tourner. En cas d'échec, nouveaux essais 5 minutes puis 30 minutes plus tard, sans retarder les autres conteneurs. Puis le **contrôle** prévu, s'il y en a un : s'il échoue, rien n'est touché.
4. **Arrêt, puis copie instantanée** des dossiers de données listés (reflink btrfs : aucun octet recopié).
5. **Recréation** à partir de la définition compose d'origine.
6. **Vérification** : le conteneur tourne encore après une minute, sans avoir redémarré, sa sonde de santé Docker est au vert s'il en a une, et il répond en HTTP si une adresse est donnée.
7. **Si ça casse : retour arrière.** Les anciennes données reviennent (les données abîmées sont mises de côté, jamais supprimées), l'ancienne image est remise, et on vérifie qu'elle repart.

Après au moins une mise à jour réussie, les commandes listées dans `apres_mise_a_jour` sont lancées, par exemple pour qu'un tableau de bord relève les nouvelles versions tout de suite plutôt qu'au relevé suivant.

## Automatique, manuel, et manuel temporaire

```
automatique ──(la mise à jour échoue)──▶ retour arrière
     ▲                                          │
     │                                          ▼
     └──(mise à jour manuelle réussie)── manuel temporaire, avec sa raison
```

- **Automatique** : mis à jour la nuit.
- **Manuel par choix** (`mode = "manuel"`) : signalé, jamais touché seul, et il le reste après une mise à jour manuelle.
- **Manuel temporaire** : un conteneur automatique qui a échoué. Il porte un blocage avec sa raison, l'erreur exacte et la marche à suivre, et redevient automatique dès qu'une mise à jour manuelle réussit.

Une montée de version majeure bloque aussi le conteneur jusqu'à ton accord. Sauf si l'étiquette de l'image fige déjà la majeure (`postgres:17-alpine`, `uptime-kuma:2`) : le changement ne peut alors pas arriver par là. `segments_majeurs = 2` rend l'outil plus prudent pour les logiciels dont le deuxième nombre compte : nginx 1.30 → 1.32 attend alors ton accord, 1.30.5 → 1.30.6 non.

Un blocage se lève aussi tout seul quand le conteneur est à jour par un autre moyen (mise à jour à la main, redéploiement), sauf après un retour arrière échoué.

Le mode de chaque conteneur peut aussi être changé sans toucher à la configuration, par exemple depuis un tableau de bord : `mode NOM auto`, `mode NOM manuel`, ou `mode NOM defaut` pour revenir à la configuration. Ce choix prime sur le fichier.

## Nouveaux conteneurs

Un conteneur ajouté plus tard est pris en charge dès la passe suivante, sans rien écrire : il est suivi dans le mode prévu pour les nouveaux venus (`[decouverte] mode`, automatique par défaut), et une notification dit ce qui a été décidé pour lui.

Pour que son retour arrière soit complet, l'outil choisit seul ses dossiers de données, avec trois garde-fous :

1. **Un volume géré par Docker** appartient à son application : il est retenu.
2. **Un dossier de la machine** n'est retenu que s'il est au moins à deux niveaux sous une racine déclarée : `/volume1/docker/app/config` oui, `/volume1/docker/app` non (souvent le dossier d'une pile entière), `/volume1/docker` non plus.
3. **Un dossier monté par un autre conteneur n'est jamais retenu** : le restaurer ferait revenir l'autre en arrière avec lui. Une médiathèque partagée est écartée par cette seule règle.

Sont ignorés d'office : les images construites sur place (aucun registre à interroger, voir plus bas pour les déclarer) et les conteneurs éphémères (`docker run --rm`). Un conteneur qui n'a pas été créé par Compose est suivi, mais reste en manuel : l'outil ne saurait pas le recréer à l'identique.

## Images via lscr.io

`lscr.io` n'est qu'une passerelle vers `ghcr.io`. Toute image qui passe encore par elle est signalée une fois, avec la correction exacte à faire dans son fichier compose.

## Images construites sur place

Une image construite sur la machine (`build:` dans le fichier compose) n'existe dans aucun registre : personne n'annoncera sa nouvelle version. Ce qui vieillit, ce sont ses **images de base** (`FROM nginx:stable-alpine`, `FROM node:22-alpine`…), et ce sont elles que l'outil surveille. Quand l'une reçoit un correctif, l'image est reconstruite avec `--pull`, puis installée comme les autres : arrêt, recréation, vérification, retour arrière si ça casse.

```toml
[[conteneur]]
nom = "site"
mode = "auto"
construction = "compose"      # ou le dossier du Dockerfile, pour un « docker build » direct
depot = "/srv/site"           # le dépôt git dont vient le code
arguments = { GIT_SHA = "label:org.opencontainers.image.revision" }
segments_majeurs = 2          # nginx 1.30 → 1.32 attend ton accord
```

**L'outil ne déploie jamais de code.** Il ne reconstruit qu'avec le code déjà en service :

- si le Dockerfile a changé depuis la construction de l'image qui tourne, il ne reconstruit pas ;
- avec `depot`, le commit du dépôt doit être celui inscrit dans l'image (étiquette OCI `org.opencontainers.image.revision`). Sinon, des commits non déployés attendent : l'outil s'arrête et te demande de déployer avec ton outil habituel. Le blocage se lève tout seul dès que c'est fait. Même `appliquer` refuse.

`arguments` passe des variables à la construction : `label:X` reprend l'étiquette `X` de l'image en service, ce qui garde le même commit dans l'image reconstruite.

Les bases de référence sont relevées dans le cache local de Docker juste après chaque construction. Une image reconstruite par ton propre outil de déploiement est donc prise en compte seule, à la passe suivante, à condition qu'il construise avec `--pull`.

**Contrôle avant installation.** `controle` lance une commande sur la nouvelle image pendant que l'ancienne tourne encore. Pour un proxy comme Caddy, c'est la validation de sa configuration : une configuration invalide empêcherait aussi l'ancienne version de repartir au retour arrière, et tous les sites tomberaient. L'option vaut aussi pour les images téléchargées.

## Notifications

Une seule notification par passe, et seulement s'il y a quelque chose à faire :

| Situation | Priorité ntfy |
|---|---|
| Nouvelle version à valider à la main, nouveau conteneur, image à corriger, code à déployer | 3 |
| Échec de téléchargement, de reconstruction, de contrôle ou d'installation | 4 |
| Retour arrière lui-même échoué | 5 |

Chaque nouvelle version n'est signalée qu'une fois : la clé est son empreinte.

## Installation

```bash
git clone https://github.com/AWallez/compose-auto-update /opt/compose-auto-update
mkdir -p /etc/compose-auto-update
cp /opt/compose-auto-update/config.exemple.toml /etc/compose-auto-update/config.toml
chmod 600 /etc/compose-auto-update/config.toml
```

Adapte la configuration, puis regarde ce que l'outil ferait, sans rien modifier :

```bash
cd /opt/compose-auto-update
python3 -m compose_auto_update --config /etc/compose-auto-update/config.toml verifier
python3 -m compose_auto_update --config /etc/compose-auto-update/config.toml --simulation passe
```

Quand le résultat te convient, installe la minuterie :

```bash
cp systemd/compose-auto-update.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now compose-auto-update.timer
```

## Commandes

| Commande | Effet |
|---|---|
| `verifier` | Ce qui serait fait. Ne télécharge et ne modifie rien. |
| `passe` | La passe complète, celle que lance la minuterie. |
| `appliquer NOM` | Met un conteneur à jour tout de suite, même bloqué ou face à une majeure, jamais avec un code non déployé. |
| `mode NOM auto\|manuel\|defaut` | Bascule un conteneur ; `defaut` rend la main à la configuration. |
| `etat` | Ce que l'outil sait de chaque conteneur. |

Options : `--simulation` (tout est décrit au journal, rien n'est exécuté), `--bavard` (journal détaillé), `--config CHEMIN`.

Le journal va dans journald : `journalctl -u compose-auto-update`.

## Ce que l'outil ne fait pas

- **Il ne sait pas si tout marche pour toi.** Il vérifie que le conteneur tourne et répond, pas qu'un thème ou une extension n'a pas cassé.
- **Il ne voit pas un changement d'éditeur ou de nom d'image.** Si un projet est renommé (Jellyseerr devenu Seerr, par exemple), il continue de surveiller l'ancienne image.
- **Le retour arrière ne couvre que les dossiers listés** dans `donnees`, ou retenus par les garde-fous pour un conteneur découvert.
- **Il ne déploie pas de code.** Une image construite sur place n'est reconstruite qu'avec le code déjà en service ; livrer un nouveau code reste le rôle de ton outil de déploiement.

## Organisation du code

| Module | Rôle |
|---|---|
| `moteur.py` | Le seul qui décide : déroulé de la passe, de la mise à jour et du retour arrière |
| `registre.py` | Empreintes et versions lues dans le registre, sans télécharger |
| `docker.py` | Lectures et actions Docker / Compose ; les actions sont neutralisées en simulation |
| `donnees.py` | Copie instantanée et restauration des données |
| `decouverte.py` | Conteneurs absents de la configuration, et choix prudent de leurs données |
| `sante.py` | Vérification après mise à jour |
| `etat.py` | État persistant, blocages et leurs conseils |
| `construction.py` | Images construites sur place : bases du Dockerfile, commit du dépôt |
| `versions.py` | Lecture des numéros de version, détection des majeures |
| `image.py` | Découpage d'une référence d'image |
| `notifier.py` | Notifications ntfy et signal de vie Uptime Kuma |
| `config.py` | Lecture et contrôle de la configuration |
| `commande.py` | Le seul endroit qui lance un processus |
| `__main__.py` | Ligne de commande, verrou contre deux passes simultanées |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Les tests remplacent Docker et les registres par des imitations : ils ne touchent ni aux conteneurs, ni au réseau.

## Prérequis

Linux, Docker avec Compose v2 ou plus récent, Python 3.11 ou plus récent, et un volume **btrfs** pour les copies instantanées.
