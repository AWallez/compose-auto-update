"""Tests des garde-fous qui choisissent les données d'un conteneur découvert.

Les montages imitent ceux du NAS : dockge qui monte tout /volume1/docker, la
médiathèque montée par plusieurs conteneurs, le socket Docker.
"""

import unittest

from compose_auto_update.decouverte import donnees_probables
from compose_auto_update.docker import Conteneur, Montage

RACINES = ["/volume1/docker"]


def conteneur(nom, *montages):
    return Conteneur(nom=nom, image="x", image_id="x", etat="running", redemarrages=0, sante=None,
                     projet="p", service=nom, dossier="/", fichiers=[], montages=list(montages))


class GardeFous(unittest.TestCase):
    def test_dossier_propre_au_conteneur_retenu(self):
        radarr = conteneur("radarr", Montage("bind", "/volume1/docker/arr/radarr/config", "/config", True))
        self.assertEqual(donnees_probables(radarr, [radarr], RACINES), ["/volume1/docker/arr/radarr/config"])

    def test_volume_docker_retenu(self):
        ntfy = conteneur("ntfy", Montage("volume", "/volume1/@docker/volumes/ntfy/_data", "/cache", True))
        self.assertEqual(donnees_probables(ntfy, [ntfy], RACINES), ["/volume1/@docker/volumes/ntfy/_data"])

    def test_racine_entiere_jamais_retenue(self):
        # dockge monte tout /volume1/docker : le copier reviendrait à tout copier
        dockge = conteneur("dockge", Montage("bind", "/volume1/docker", "/volume1/docker", True),
                           Montage("bind", "/volume1/docker/monitoring/dockge/data", "/app/data", True))
        self.assertEqual(donnees_probables(dockge, [dockge], RACINES),
                         ["/volume1/docker/monitoring/dockge/data"])

    def test_dossier_de_pile_trop_haut_jamais_retenu(self):
        outil = conteneur("outil", Montage("bind", "/volume1/docker/arr", "/pile", True))
        self.assertEqual(donnees_probables(outil, [outil], RACINES), [])

    def test_dossier_partage_jamais_retenu(self):
        # la médiathèque, montée par plusieurs conteneurs, et même sous la racine
        media = Montage("bind", "/volume1/docker/partage/media", "/data", True)
        radarr, sonarr = conteneur("radarr", media), conteneur("sonarr", media)
        self.assertEqual(donnees_probables(radarr, [radarr, sonarr], RACINES), [])

    def test_hors_racine_et_lecture_seule_jamais_retenus(self):
        outil = conteneur("outil",
                          Montage("bind", "/var/run/docker.sock", "/var/run/docker.sock", True),
                          Montage("bind", "/volume1/docker/outil/config", "/config", False))
        self.assertEqual(donnees_probables(outil, [outil], RACINES), [])


if __name__ == "__main__":
    unittest.main()
