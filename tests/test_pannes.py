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


if __name__ == "__main__":
    unittest.main()
