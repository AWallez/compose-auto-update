"""Modes de panne : ce qui pouvait mal tourner sans que personne ne le voie.

Chaque test part d'une panne reproduite (réponse étrange, registre exotique,
disque plein…) et vérifie qu'elle est soit rattrapée, soit signalée, mais
jamais présentée comme une réussite ni laissée à mi-chemin.
"""

import errno
import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from compose_auto_update import sante
from compose_auto_update.etat import Etat
from compose_auto_update.image import analyser
from compose_auto_update.notifier import Notificateur
from compose_auto_update.registre import ErreurRegistre, Registre

from .test_moteur import monter


class ReponseHttpEtrange(unittest.TestCase):
    """L'adresse de santé répond autre chose que du HTTP (BadStatusLine).

    Avant : l'exception traversait la vérification, puis toute la passe. Pas de
    retour arrière, la nouvelle version laissée en place, l'état non enregistré.
    """

    def test_retour_arriere_au_lieu_d_un_arret_en_route(self):
        moteur, docker, _ = monter()
        moteur.conf.delai_sante = 1
        moteur.conf.conteneurs[0].sante = "http://radarr.invalid:7878/"
        moteur.attendre = lambda secondes: time.sleep(0.05)

        def ouvrir(*args, **kwargs):
            # La nouvelle image répond n'importe quoi, l'ancienne répond bien
            if docker.conteneur("radarr").image_id == "sha256:image-neuve":
                raise http.client.BadStatusLine("SSH-2.0-OpenSSH")
            return mock.MagicMock()

        ouvreur = mock.Mock(open=mock.Mock(side_effect=ouvrir))
        with mock.patch("compose_auto_update.sante.urllib.request.build_opener",
                        return_value=ouvreur):
            bilan = moteur.passe()
        self.assertEqual(bilan.mis_a_jour, [])
        self.assertFalse(bilan.urgent)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "installation")
        self.assertEqual(docker.conteneur("radarr").image_id, "sha256:id-radarr")
        enregistre = json.loads(Path(moteur.conf.fichier_etat).read_text(encoding="utf-8"))
        self.assertEqual(enregistre["conteneurs"]["radarr"]["blocage"]["raison"], "installation")

    def test_reponse_non_http_n_est_pas_une_reponse(self):
        horloge = iter(range(100)).__next__
        ouvreur = mock.Mock(open=mock.Mock(side_effect=http.client.BadStatusLine("x")))
        with mock.patch("compose_auto_update.sante.urllib.request.build_opener",
                        return_value=ouvreur):
            ok, raison = sante._repond("http://x.invalid/", 5, 1, lambda s: None, horloge)
        self.assertFalse(ok)
        self.assertIn("aucune réponse", raison)

    def test_adresse_invalide_echoue_sans_exception(self):
        ok, raison = sante._repond("radarr", 5, 1, lambda s: None, iter(range(100)).__next__)
        self.assertFalse(ok)
        self.assertIn("radarr", raison)


class ManifesteExotique(unittest.TestCase):
    """Un registre qui renvoie un manifeste sans « config » (ancien format v1, artefact…).

    Avant : KeyError, qui n'est pas une erreur d'examen : TOUTE la passe
    s'arrêtait, pour tous les conteneurs, à cause d'une seule image.
    """

    REF = analyser("ghcr.io/editeur/outil:latest")

    def test_manifeste_sans_configuration(self):
        with mock.patch.object(Registre, "_json", return_value={"schemaVersion": 1, "fsLayers": []}):
            with self.assertRaises(ErreurRegistre):
                Registre().configuration(self.REF, "sha256:x", "linux/amd64")

    def test_index_sans_empreinte(self):
        index = {"mediaType": "application/vnd.oci.image.index.v1+json",
                 "manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}]}
        with mock.patch.object(Registre, "_json", return_value=index):
            with self.assertRaises(ErreurRegistre):
                Registre().configuration(self.REF, "sha256:x", "linux/amd64")

    def test_reponse_qui_n_est_pas_un_objet(self):
        with mock.patch.object(Registre, "_json", return_value=["inattendu"]):
            with self.assertRaises(ErreurRegistre):
                Registre().configuration(self.REF, "sha256:x", "linux/amd64")


class NotificationEtrange(unittest.TestCase):
    """Une notification qui échoue ne doit jamais faire échouer la passe (notifier.py).

    Avant : une réponse non HTTP ou une adresse mal écrite levait une exception
    hors de la liste attendue. La passe s'arrêtait avant d'enregistrer l'état,
    alors que les mises à jour avaient eu lieu.
    """

    def test_reponse_non_http(self):
        n = Notificateur("http://ntfy.invalid", "sujet", kuma_push="http://kuma.invalid/push")
        with mock.patch("compose_auto_update.notifier.urllib.request.urlopen",
                        side_effect=http.client.BadStatusLine("x")):
            self.assertFalse(n.envoyer("titre", "message"))
            n.signal_de_vie(True, "ok")              # ne lève rien

    def test_adresse_sans_protocole(self):
        n = Notificateur("ntfy.exemple", "sujet", kuma_push="kuma.exemple/push")
        self.assertFalse(n.envoyer("titre", "message"))
        n.signal_de_vie(True, "ok")

    def test_la_passe_enregistre_l_etat_malgre_tout(self):
        moteur, _, _ = monter()
        moteur.notificateur = Notificateur("ntfy.exemple", "sujet")
        moteur.registre.version = "7.0.0"            # montée majeure : de quoi notifier
        moteur.passe()
        self.assertTrue(Path(moteur.conf.fichier_etat).exists())


if __name__ == "__main__":
    unittest.main()
