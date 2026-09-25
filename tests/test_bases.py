"""Tests des briques simples : références d'image, versions, configuration, état."""

import json
import tempfile
import unittest
from pathlib import Path

from compose_auto_update import config, versions
from compose_auto_update.etat import Etat
from compose_auto_update.image import DOCKER_HUB, analyser, sans_etiquette


class ReferencesImage(unittest.TestCase):
    def test_image_officielle_abregee(self):
        r = analyser("nginx:alpine")
        self.assertEqual((r.registre, r.depot, r.etiquette), (DOCKER_HUB, "library/nginx", "alpine"))

    def test_compte_docker_hub(self):
        r = analyser("louislam/uptime-kuma:2")
        self.assertEqual((r.registre, r.depot, r.etiquette), (DOCKER_HUB, "louislam/uptime-kuma", "2"))

    def test_autre_registre(self):
        r = analyser("lscr.io/linuxserver/radarr:latest")
        self.assertEqual((r.registre, r.depot, r.etiquette), ("lscr.io", "linuxserver/radarr", "latest"))

    def test_sans_etiquette_veut_dire_latest(self):
        self.assertEqual(analyser("ghcr.io/cross-seed/cross-seed").etiquette, "latest")

    def test_port_de_registre_pas_pris_pour_une_etiquette(self):
        r = analyser("localhost:5000/outil")
        self.assertEqual((r.registre, r.depot, r.etiquette), ("localhost:5000", "outil", "latest"))

    def test_docker_io_explicite(self):
        self.assertEqual(analyser("docker.io/nickfedor/watchtower:latest").registre, DOCKER_HUB)

    def test_retirer_etiquette(self):
        self.assertEqual(sans_etiquette("lscr.io/linuxserver/radarr:latest"), "lscr.io/linuxserver/radarr")
        self.assertEqual(sans_etiquette("nginx"), "nginx")


class Versions(unittest.TestCase):
    def test_etiquette_oci(self):
        self.assertEqual(versions.lire({"org.opencontainers.image.version": "v3.4.1"}, []), "v3.4.1")

    def test_etiquette_linuxserver(self):
        etiquettes = {"build_version": "Linuxserver.io version:- 2.6.5.5623-ls161 Build-date:- 2026-09-15"}
        self.assertEqual(versions.lire(etiquettes, []), "2.6.5.5623-ls161")

    def test_regle_variable_environnement(self):
        self.assertEqual(versions.lire({}, ["PATH=/bin", "NGINX_VERSION=1.31.6"], "env:NGINX_VERSION"), "1.31.6")

    def test_node_version_jamais_devinee(self):
        # uptime-kuma : NODE_VERSION est la version de Node, pas de l'application
        self.assertIsNone(versions.lire({}, ["NODE_VERSION=22.22.3"]))

    def test_majeures(self):
        cas = {"2.6.5.5623-ls161": 2, "v1.6.0-ls354": 1, "version-6.13.7": 6,
               "5.2.3_v2.0.14-ls477": 5, "v0.107.77": 0, None: None, "sans-chiffre": None}
        for version, attendu in cas.items():
            self.assertEqual(versions.majeure(version), attendu, version)

    def test_etiquettes_qui_figent_la_majeure(self):
        for etiquette in ("2", "17-alpine", "15", "v3.41.3"):
            self.assertTrue(versions.etiquette_fige_majeure(etiquette), etiquette)
        for etiquette in ("latest", "alpine", "postgresql-latest"):
            self.assertFalse(versions.etiquette_fige_majeure(etiquette), etiquette)


class Configuration(unittest.TestCase):
    def ecrire(self, texte):
        dossier = tempfile.mkdtemp()
        chemin = Path(dossier) / "config.toml"
        chemin.write_text(texte, encoding="utf-8")
        return str(chemin)

    BASE = '[general]\ndossier_copies = "/srv/copies"\n'

    def test_configuration_valide(self):
        conf = config.charger(self.ecrire(self.BASE + '''
[[conteneur]]
nom = "radarr"
mode = "auto"
donnees = ["/srv/radarr/config"]
sante = "http://127.0.0.1:7878/"
'''))
        self.assertEqual(conf.conteneurs[0].nom, "radarr")
        self.assertEqual(conf.attentes_reessai, [300, 1500])

    def test_nom_en_double_refuse(self):
        texte = self.BASE + '[[conteneur]]\nnom = "a"\nmode = "auto"\n' * 2
        with self.assertRaisesRegex(config.ErreurConfig, "deux fois"):
            config.charger(self.ecrire(texte))

    def test_mode_inconnu_refuse(self):
        with self.assertRaisesRegex(config.ErreurConfig, "mode"):
            config.charger(self.ecrire(self.BASE + '[[conteneur]]\nnom = "a"\nmode = "parfois"\n'))

    def test_chemin_relatif_refuse(self):
        texte = self.BASE + '[[conteneur]]\nnom = "a"\nmode = "auto"\ndonnees = ["config"]\n'
        with self.assertRaisesRegex(config.ErreurConfig, "absolu"):
            config.charger(self.ecrire(texte))

    def test_dossier_copies_obligatoire(self):
        with self.assertRaisesRegex(config.ErreurConfig, "dossier_copies"):
            config.charger(self.ecrire('[[conteneur]]\nnom = "a"\nmode = "auto"\n'))


class EtatPersistant(unittest.TestCase):
    def setUp(self):
        self.chemin = Path(tempfile.mkdtemp()) / "etat.json"

    def test_cycle_blocage(self):
        etat = Etat(self.chemin)
        etat.bloquer("radarr", "telechargement", "registre muet", "TLS handshake timeout")
        self.assertEqual(etat.blocage("radarr")["raison"], "telechargement")
        self.assertIn("Relance", etat.blocage("radarr")["conseil"])
        etat.debloquer("radarr")
        self.assertIsNone(etat.blocage("radarr"))

    def test_sauvegarde_puis_relecture(self):
        etat = Etat(self.chemin)
        etat.marquer_notifie("radarr", "sha256:abc")
        export = self.chemin.parent / "portail" / "maj.json"
        etat.sauver(export)
        relu = Etat(self.chemin)
        self.assertTrue(relu.deja_notifie("radarr", "sha256:abc"))
        self.assertEqual(json.loads(export.read_text(encoding="utf-8")), relu.donnees)

    def test_historique_plafonne(self):
        etat = Etat(self.chemin)
        for i in range(50):
            etat.noter("radarr", "essai", str(i))
        historique = etat.conteneur("radarr")["historique"]
        self.assertEqual(len(historique), 20)
        self.assertEqual(historique[-1]["detail"], "49")


if __name__ == "__main__":
    unittest.main()
