"""Modes de panne : ce qui pouvait mal tourner sans que personne ne le voie.

Chaque test part d'une panne reproduite (réponse étrange, registre exotique,
disque plein…) et vérifie qu'elle est soit rattrapée, soit signalée, mais
jamais présentée comme une réussite ni laissée à mi-chemin.
"""

import dataclasses
import errno
import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from compose_auto_update import donnees, sante
from compose_auto_update.commande import ErreurCommande
from compose_auto_update.etat import Etat
from compose_auto_update.image import analyser
from compose_auto_update.moteur import Moteur
from compose_auto_update.notifier import Notificateur
from compose_auto_update.registre import ErreurRegistre, Registre

from .test_moteur import monter


class TelechargementSansEffet(unittest.TestCase):
    """« docker compose pull » réussit, mais l'image en service ne change pas.

    Vu avec un miroir de registre en retard, un fichier compose qui désigne une
    autre image que celle du conteneur, ou un service qui ne se télécharge pas.
    Avant : « mis à jour 6.4.4 → 6.5.0 », sans notification (une réussite est
    silencieuse), et le même arrêt-copie-recréation recommencé chaque nuit.
    Maintenant : échec signalé, blocage avec sa propre marche à suivre, et les
    données laissées telles quelles, puisque la nouvelle version n'a jamais tourné.
    """

    def test_n_est_pas_presente_comme_une_reussite(self):
        moteur, docker, _ = monter()
        docker.telechargement_sans_effet = True
        bilan = moteur.passe()
        self.assertEqual(bilan.mis_a_jour, [])
        self.assertEqual(len(bilan.erreurs), 1)
        self.assertIn("n'a pas changé", bilan.erreurs[0][1])
        blocage = moteur.etat.blocage("radarr")
        self.assertEqual(blocage["raison"], "image_inchangee")
        self.assertIn("fichier compose", blocage["conseil"])       # pas « lis les notes de version »
        self.assertEqual(moteur.etat.conteneur("radarr")["version"], "6.4.4")
        self.assertEqual(moteur.notificateur.envois[0][2], 4)      # tu es prévenu

    def test_les_donnees_ne_sont_pas_restaurees(self):
        # Seule l'ancienne version a tourné : restaurer effacerait ses dernières écritures
        moteur, docker, _ = monter()
        docker.telechargement_sans_effet = True
        with mock.patch("compose_auto_update.moteur.donnees") as donnees:
            moteur.passe()
        donnees.copier.assert_called_once()
        donnees.restaurer.assert_not_called()
        self.assertEqual(docker.conteneur("radarr").image_id, "sha256:id-radarr")

    def test_temoin_une_nouvelle_version_cassee_restaure_les_donnees(self):
        moteur, docker, _ = monter()
        docker.etats_apres_recreation = ["exited", "running"]
        with mock.patch("compose_auto_update.moteur.donnees") as donnees:
            moteur.passe()
        donnees.restaurer.assert_called_once()
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "installation")

    def test_ne_recommence_pas_chaque_nuit(self):
        moteur, docker, _ = monter()
        docker.telechargement_sans_effet = True
        moteur.passe()
        arrets = docker.actions.count("arreter radarr")
        moteur.passe()
        self.assertEqual(docker.actions.count("arreter radarr"), arrets)

    def test_une_vraie_mise_a_jour_passe_toujours(self):
        moteur, docker, _ = monter()
        self.assertEqual(len(moteur.passe().mis_a_jour), 1)
        self.assertEqual(docker.conteneur("radarr").image_id, "sha256:image-neuve")


class MiseAJourInterrompue(unittest.TestCase):
    """Le programme tué net en pleine mise à jour : redémarrage, arrêt du service, manque de mémoire.

    Avant : rien ne le disait, jamais. La passe suivante prenait le conteneur
    arrêté pour un arrêt voulu, et la nouvelle version jamais vérifiée pour une
    version à jour. SystemExit joue ici le rôle du signal : rien ne le rattrape.
    """

    @staticmethod
    def suivant(moteur, docker):
        """Le programme suivant : il relit l'état sur le disque, comme après un redémarrage."""
        return Moteur(moteur.conf, docker, moteur.registre, Etat(moteur.conf.fichier_etat),
                      moteur.notificateur, attendre=lambda secondes: None)

    def tuer_apres_l_arret(self):
        moteur, docker, _ = monter()
        arreter = docker.arreter

        def arreter_puis_tuer(c):
            arreter(c)
            docker.conteneurs[c.nom] = dataclasses.replace(docker.conteneurs[c.nom], etat="exited")
            raise SystemExit("tué")

        docker.arreter = arreter_puis_tuer
        with self.assertRaises(SystemExit):
            moteur.passe()
        docker.arreter = arreter
        return self.suivant(moteur, docker), docker

    def tuer_pendant_la_verification(self):
        moteur, docker, _ = monter()
        recreer = docker.recreer

        def recreer_puis_tuer(c):
            recreer(c)        # la nouvelle version tourne, mal : elle redémarre en boucle
            docker.conteneurs[c.nom] = dataclasses.replace(docker.conteneurs[c.nom], etat="restarting")
            raise SystemExit("tué")

        docker.recreer = recreer_puis_tuer
        with self.assertRaises(SystemExit):
            moteur.passe()
        docker.recreer = recreer
        return self.suivant(moteur, docker), docker

    def test_conteneur_laisse_arrete_alerte_urgente(self):
        moteur, docker = self.tuer_apres_l_arret()
        bilan = moteur.passe()
        self.assertTrue(bilan.interrompu)
        titre, message, priorite = moteur.notificateur.envois[0]
        self.assertEqual(priorite, 5)
        self.assertIn("INTERROMPUE", titre)
        self.assertIn("6.4.4 → 6.5.0", message)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "interrompue")
        self.assertEqual(docker.conteneur("radarr").etat, "exited")     # rien n'est tenté seul

    def test_version_jamais_verifiee_alerte_une_fois_et_reste_bloquee(self):
        moteur, _ = self.tuer_pendant_la_verification()
        moteur.passe()
        self.assertEqual(moteur.notificateur.envois[0][2], 5)
        moteur.passe()                    # « à jour », mais rien ne dit qu'elle marche
        self.assertEqual(len(moteur.notificateur.envois), 1)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "interrompue")

    def test_le_releve_de_la_journee_n_attend_pas_la_passe(self):
        moteur, _ = self.tuer_apres_l_arret()
        moteur.releve()
        self.assertEqual(moteur.notificateur.envois[0][2], 5)
        moteur.passe()
        self.assertEqual(len(moteur.notificateur.envois), 1)          # pas deux fois

    def test_la_page_leve_le_blocage(self):
        moteur, _ = self.tuer_pendant_la_verification()
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "interrompue")
        moteur.appliquer("radarr")        # tu as vérifié : la version est déjà là
        self.assertIsNone(moteur.etat.blocage("radarr"))

    def test_marque_sur_le_disque_avant_l_arret_et_retiree_apres(self):
        moteur, docker, _ = monter()
        lu, arreter = [], docker.arreter

        def arreter_en_lisant_l_etat(c):
            lu.append(json.loads(Path(moteur.conf.fichier_etat).read_text(encoding="utf-8")))
            arreter(c)

        docker.arreter = arreter_en_lisant_l_etat
        moteur.passe()
        self.assertEqual(lu[0]["conteneurs"]["radarr"]["en_cours"]["vers"], "6.5.0")
        fin = json.loads(Path(moteur.conf.fichier_etat).read_text(encoding="utf-8"))
        self.assertNotIn("en_cours", fin["conteneurs"]["radarr"])

    def test_sans_marque_possible_rien_n_est_touche(self):
        moteur, docker, _ = monter()
        disque_plein = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(Etat, "sauver", side_effect=disque_plein):
            with self.assertRaises(OSError):          # l'état de fin de passe non plus
                moteur.passe()
        self.assertNotIn("arreter radarr", docker.actions)
        self.assertIn("non commencée", moteur.notificateur.envois[0][1])


class ErreurImprevue(unittest.TestCase):
    """Une exception que personne n'attendait, au milieu d'une mise à jour (un bogue).

    Avant : elle traversait la passe. Le service restait arrêté ou sur la
    nouvelle version, sans retour arrière, et l'état n'était pas enregistré.
    """

    def monter(self, echecs):
        moteur, docker, _ = monter()
        recreer, appels = docker.recreer, []

        def recreer_avec_bogue(c):
            appels.append(c.nom)
            if len(appels) <= echecs:
                raise RuntimeError("bogue imprévu")
            recreer(c)

        docker.recreer = recreer_avec_bogue
        return moteur, docker, appels

    def test_retour_arriere_et_trace_gardee_au_journal(self):
        moteur, docker, appels = self.monter(echecs=1)
        with self.assertLogs("compose_auto_update.moteur", "ERROR") as journal:
            bilan = moteur.passe()
        self.assertEqual(len(appels), 2)                            # l'ancienne a été recréée
        self.assertEqual(docker.conteneur("radarr").image_id, "sha256:id-radarr")
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "installation")
        self.assertIn("bogue imprévu", bilan.erreurs[0][1])
        self.assertTrue(any(r.exc_info for r in journal.records))   # le bogue n'est pas caché
        enregistre = json.loads(Path(moteur.conf.fichier_etat).read_text(encoding="utf-8"))
        self.assertIsNotNone(enregistre["conteneurs"]["radarr"]["blocage"])

    def test_retour_arriere_impossible_alerte_urgente(self):
        moteur, _, _ = self.monter(echecs=2)
        with self.assertLogs("compose_auto_update.moteur", "ERROR"):
            bilan = moteur.passe()
        self.assertTrue(bilan.urgent)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "retour_arriere")
        self.assertEqual(moteur.notificateur.envois[0][2], 5)


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


class CopieAMoitieFaite(unittest.TestCase):
    """La copie des données échoue en route (disque plein, dossier introuvable).

    Avant : la copie partielle restait, et comptait parmi les copies gardées.
    Trois échecs de suite, et `elaguer` effaçait la dernière copie complète.
    """

    def copier(self, racine):
        sources = Path(tempfile.mkdtemp())
        for nom in ("config", "base"):
            (sources / nom).mkdir()

        def cp(arguments, *args, **kwargs):
            destination = Path(arguments[-1])
            if destination.name.endswith("base"):
                raise ErreurCommande(arguments, 1, "cp: No space left on device")
            destination.mkdir()

        with mock.patch("compose_auto_update.donnees.executer", side_effect=cp):
            with self.assertRaises(ErreurCommande):
                donnees.copier([sources / "config", sources / "base"], racine, "radarr",
                               "20261103-071500")

    def test_la_copie_partielle_est_effacee(self):
        racine = Path(tempfile.mkdtemp())
        self.copier(racine)
        self.assertFalse((racine / "radarr" / "20261103-071500").exists())

    def test_les_copies_d_avant_restent(self):
        racine = Path(tempfile.mkdtemp())
        (racine / "radarr" / "20261102-071500").mkdir(parents=True)
        self.copier(racine)
        self.assertTrue((racine / "radarr" / "20261102-071500").is_dir())


class DisquePlein(unittest.TestCase):
    """L'état ne peut pas être écrit : l'ancien reste intact, et rien ne traîne à côté."""

    def test_aucun_fichier_temporaire_abandonne(self):
        dossier = Path(tempfile.mkdtemp())
        etat = Etat(dossier / "etat.json")
        etat.sauver()
        avant = (dossier / "etat.json").read_text(encoding="utf-8")
        etat.noter("radarr", "test")
        with mock.patch("compose_auto_update.etat.os.replace",
                        side_effect=OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))):
            with self.assertRaises(OSError):
                etat.sauver()
        self.assertEqual(sorted(p.name for p in dossier.iterdir()), ["etat.json"])
        self.assertEqual((dossier / "etat.json").read_text(encoding="utf-8"), avant)


if __name__ == "__main__":
    unittest.main()
