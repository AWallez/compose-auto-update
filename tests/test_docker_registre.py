"""Tests des deux modules qui parlent au monde extérieur, sans jamais le contacter."""

import json
import unittest
from unittest import mock

from compose_auto_update.docker import Conteneur, Docker
from compose_auto_update.image import analyser
from compose_auto_update.registre import ErreurRegistre, Registre


def conteneur():
    return Conteneur(nom="arr-radarr", image="lscr.io/linuxserver/radarr:latest",
                     image_id="sha256:ancien", etat="running", redemarrages=0, sante=None,
                     projet="arr", service="radarr", dossier="/volume1/docker/arr",
                     fichiers=["/volume1/docker/arr/docker-compose.yml"])


class Simulation(unittest.TestCase):
    def test_en_simulation_aucune_action_n_est_executee(self):
        with mock.patch("compose_auto_update.docker.executer") as executer:
            d = Docker(simulation=True)
            d.telecharger(conteneur())
            d.arreter(conteneur())
            d.recreer(conteneur())
            d.etiqueter("sha256:ancien", "x:y")
            d.nettoyer_images()
        executer.assert_not_called()

    def test_recreation_ne_touche_que_ce_service(self):
        with mock.patch("compose_auto_update.docker.executer") as executer:
            Docker().recreer(conteneur())
        arguments = executer.call_args[0][0]
        self.assertIn("--no-deps", arguments)
        self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
        self.assertEqual(arguments[-1], "radarr")
        self.assertEqual(arguments[arguments.index("--project-directory") + 1], "/volume1/docker/arr")


class LectureDeDockerInspect(unittest.TestCase):
    def test_tous_les_conteneurs_en_un_appel(self):
        reponse_inspect = json.dumps([{
            "Name": "/arr-radarr", "Image": "sha256:ancien", "RestartCount": 2,
            "Config": {"Image": "lscr.io/linuxserver/radarr:latest", "Labels": {
                "com.docker.compose.project": "arr", "com.docker.compose.service": "radarr",
                "com.docker.compose.project.working_dir": "/volume1/docker/arr",
                "com.docker.compose.project.config_files": "/volume1/docker/arr/docker-compose.yml"}},
            "State": {"Status": "running"},
            "HostConfig": {"AutoRemove": False},
            "Mounts": [{"Type": "bind", "Source": "/volume1/docker/arr/radarr/config",
                        "Destination": "/config", "RW": True}],
        }])
        with mock.patch("compose_auto_update.docker.executer",
                        side_effect=["arr-radarr\n", reponse_inspect]):
            (c,) = Docker().tous()
        self.assertEqual(c.nom, "arr-radarr")               # la barre oblique de Docker est retirée
        self.assertEqual((c.projet, c.service, c.redemarrages), ("arr", "radarr", 2))
        self.assertIsNone(c.sante)                           # pas de sonde de santé dans l'image
        self.assertFalse(c.ephemere)
        self.assertEqual(c.montages[0].source, "/volume1/docker/arr/radarr/config")


class ChoixDeLArchitecture(unittest.TestCase):
    def test_l_image_du_nas_est_choisie_dans_l_index(self):
        reponses = {
            "manifests/sha256:index": {"mediaType": "application/vnd.oci.image.index.v1+json",
                                       "manifests": [
                                           {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                                           {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]},
            "manifests/sha256:amd": {"config": {"digest": "sha256:conf"}},
            "blobs/sha256:conf": {"config": {"Labels": {"org.opencontainers.image.version": "6.5.0"},
                                             "Env": ["A=1"]}},
        }
        registre = Registre()
        registre._json = lambda ref, chemin, accept: reponses[chemin]
        etiquettes, env = registre.configuration(analyser("ghcr.io/linuxserver/radarr"), "sha256:index", "linux/amd64")
        self.assertEqual(etiquettes["org.opencontainers.image.version"], "6.5.0")
        self.assertEqual(env, ["A=1"])

    def test_architecture_absente(self):
        registre = Registre()
        registre._json = lambda ref, chemin, accept: {"manifests": []}
        with self.assertRaises(ErreurRegistre):
            registre.configuration(analyser("ghcr.io/x/y"), "sha256:index", "linux/amd64")


if __name__ == "__main__":
    unittest.main()
