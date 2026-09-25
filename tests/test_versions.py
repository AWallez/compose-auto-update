"""Tests de la recherche de version pour les images qui n'en déclarent aucune.

Les cas viennent du NAS, relevés le 25/09/2026 : postgres range sa version dans
PG_VERSION, et uptime-kuma, dockge, ntfy et umami n'en déclarent aucune.
"""

import unittest
from unittest import mock

from compose_auto_update import versions
from compose_auto_update.image import analyser
from compose_auto_update.registre import ErreurRegistre, Registre

from .test_moteur import monter


class VariableAuNomDuLogiciel(unittest.TestCase):
    def test_postgres_range_sa_version_dans_pg_version(self):
        env = ["GOSU_VERSION=1.19", "PG_VERSION=17.11"]
        self.assertEqual(versions.lire({}, env, depot="library/postgres"), "17.11")

    def test_la_version_de_node_n_est_pas_celle_d_uptime_kuma(self):
        env = ["NODE_VERSION=22.22.3", "YARN_VERSION=1.22.22"]
        self.assertIsNone(versions.lire({}, env, depot="louislam/uptime-kuma"))

    def test_sans_depot_aucune_devinette(self):
        self.assertIsNone(versions.lire({}, ["NGINX_VERSION=1.31.6"]))
        self.assertEqual(versions.lire({}, ["NGINX_VERSION=1.31.6"], depot="library/nginx"), "1.31.6")


class VersionDansUneEtiquette(unittest.TestCase):
    def test_lecture(self):
        cas = {"2.5.5": "2.5.5", "v2.28.0": "v2.28.0", "17.11-alpine3.22": "17.11",
               "postgresql-3.0.0": "3.0.0", "2": "2", "latest": None, "next": None,
               "nightly3": None, "alpine3.22": None}
        for etiquette, attendu in cas.items():
            self.assertEqual(versions.de_etiquette(etiquette), attendu, etiquette)

    def test_la_plus_precise(self):
        # les étiquettes réelles qui désignaient l'image en service, le 25/09
        self.assertEqual(versions.la_plus_precise(["2.5.5", "2", "next"]), "2.5.5")
        self.assertEqual(versions.la_plus_precise(["latest", "beta", "1.5.0", "1"]), "1.5.0")
        self.assertEqual(versions.la_plus_precise(["latest", "v2.28", "v2", "v2.28.0"]), "v2.28.0")
        self.assertIsNone(versions.la_plus_precise(["latest", "beta"]))


class RechercheDansLeRegistre(unittest.TestCase):
    def test_docker_hub_une_seule_requete(self):
        page = {"results": [{"name": "latest", "digest": "sha256:autre"},
                            {"name": "2.5.5", "digest": "sha256:la-notre"},
                            {"name": "2", "digest": "sha256:la-notre"},
                            {"name": "next", "digest": "sha256:la-notre"}]}
        registre = Registre()
        with mock.patch.object(registre, "_hub", return_value=page) as hub:
            version = registre.version_par_etiquettes(analyser("louislam/uptime-kuma:2"),
                                                      "sha256:la-notre")
        self.assertEqual(version, "2.5.5")
        self.assertIn("/namespaces/louislam/repositories/uptime-kuma/tags", hub.call_args[0][0])

    def test_image_officielle_dans_l_espace_library(self):
        registre = Registre()
        with mock.patch.object(registre, "_hub", return_value={"results": []}) as hub:
            self.assertIsNone(registre.version_par_etiquettes(analyser("postgres:17-alpine"), "x"))
        self.assertIn("/namespaces/library/repositories/postgres/", hub.call_args[0][0])

    def test_autre_registre_plus_hautes_versions_d_abord(self):
        # umami, sur ghcr.io : « postgresql-latest » est la même image que « 3.4.0 »
        noms = ["postgresql-latest", "postgresql-v2.20.2", "postgresql-3.0.0",
                "3.3.1", "3.4.0", "3.4", "latest"]
        empreintes = {"3.4.0": "sha256:la-notre", "3.4": "sha256:la-notre"}
        demandees = []

        def empreinte(ref):
            demandees.append(ref.etiquette)
            return empreintes.get(ref.etiquette, "sha256:autre")

        registre = Registre()
        with mock.patch.object(registre, "_json", return_value={"tags": noms}), \
                mock.patch.object(registre, "empreinte", side_effect=empreinte):
            version = registre.version_par_etiquettes(
                analyser("ghcr.io/umami-software/umami:postgresql-latest"), "sha256:la-notre")
        self.assertEqual(version, "3.4.0")
        self.assertEqual(demandees, ["3.4.0"])      # la plus haute d'abord : trouvée au 1er essai

    def test_nombre_d_essais_limite(self):
        registre = Registre()
        with mock.patch.object(registre, "_json", return_value={"tags": [f"1.{i}" for i in range(50)]}), \
                mock.patch.object(registre, "empreinte", return_value="sha256:autre") as empreinte:
            self.assertIsNone(registre.version_par_etiquettes(analyser("ghcr.io/x/y:latest"), "z"))
        self.assertEqual(empreinte.call_count, 10)


class DansLaPasse(unittest.TestCase):
    def monter(self, reponses, a_jour=True):
        """Un radarr sans aucune version déclarée, et un registre qui répond `reponses`."""
        moteur, docker, _ = monter(actuelle=None, nouvelle=None, deja_a_jour=a_jour, mode="manuel")
        moteur.registre.version_par_etiquettes = mock.Mock(side_effect=reponses)
        return moteur, docker

    def test_version_trouvee_puis_gardee(self):
        moteur, _ = self.monter(["6.4.4"])
        moteur.passe()
        self.assertEqual(moteur.etat.conteneur("radarr")["version"], "6.4.4")
        moteur.passe()                                                  # le lendemain
        self.assertEqual(moteur.etat.conteneur("radarr")["version"], "6.4.4")
        self.assertEqual(moteur.registre.version_par_etiquettes.call_count, 1)

    def test_nouvelle_version_trouvee_aussi(self):
        moteur, _ = self.monter(["6.4.4", "6.5.0"], a_jour=False)
        moteur.passe()
        self.assertEqual(moteur.etat.conteneur("radarr")["disponible"]["version"], "6.5.0")
        self.assertIn("6.4.4 → 6.5.0", moteur.notificateur.envois[0][1])

    def test_registre_muet_rien_de_garde_rien_de_casse(self):
        moteur, _ = self.monter([ErreurRegistre("injoignable"), "6.4.4"])
        bilan = moteur.passe()
        self.assertFalse(bilan.erreurs)
        self.assertIsNone(moteur.etat.conteneur("radarr")["version"])
        moteur.passe()                                                  # on réessaie
        self.assertEqual(moteur.etat.conteneur("radarr")["version"], "6.4.4")


if __name__ == "__main__":
    unittest.main()
