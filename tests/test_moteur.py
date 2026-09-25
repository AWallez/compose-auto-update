"""Tests du moteur : chaque scénario de la vraie vie, avec de faux Docker et registre.

Aucun de ces tests ne touche Docker, le réseau ou le disque de données : les
faux objets ci-dessous jouent leur rôle et notent tout ce qu'on leur demande.
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from compose_auto_update.commande import ErreurCommande
from compose_auto_update.config import Conf, ConfConteneur
from compose_auto_update.docker import Conteneur, Montage
from compose_auto_update.etat import Etat
from compose_auto_update.moteur import Moteur


class FauxDocker:
    def __init__(self):
        self.conteneurs = {}
        self.images = {}
        self.actions = []                   # tout ce qui aurait modifié le système
        self.echecs_telechargement = 0      # nombre de téléchargements à faire rater
        self.etats_apres_recreation = []    # état du conteneur après chaque recréation

    def ajouter(self, nom, image, empreinte_locale, version, projet="pile",
                etat="running", ephemere=False, montages=()):
        image_id = f"sha256:id-{nom}"
        self.conteneurs[nom] = Conteneur(
            nom=nom, image=image, image_id=image_id, etat=etat, redemarrages=0, sante=None,
            projet=projet, service=nom if projet else "", dossier="/srv/pile", fichiers=[],
            ephemere=ephemere, montages=list(montages))
        empreintes = [empreinte_locale] if empreinte_locale else []
        self.images[image_id] = (image_id, empreintes, {"org.opencontainers.image.version": version}, [])

    def plateforme(self):
        return "linux/amd64"

    def conteneur(self, nom):
        return self.conteneurs[nom]

    def tous(self):
        return list(self.conteneurs.values())

    def image(self, reference):
        return self.images[reference]

    def plan_de_construction(self, c, construction):
        raise KeyError(f"le service {c.service} n'a pas de section « build »")

    def telecharger(self, c):
        self.actions.append(f"telecharger {c.nom}")
        if self.echecs_telechargement:
            self.echecs_telechargement -= 1
            raise ErreurCommande(["docker", "compose", "pull"], 1, "TLS handshake timeout")

    def arreter(self, c):
        self.actions.append(f"arreter {c.nom}")

    def recreer(self, c):
        self.actions.append(f"recreer {c.nom}")
        if self.etats_apres_recreation:
            etat = self.etats_apres_recreation.pop(0)
            self.conteneurs[c.nom] = dataclasses.replace(self.conteneurs[c.nom], etat=etat)

    def etiqueter(self, image_id, reference):
        self.actions.append(f"etiqueter {image_id} {reference}")

    def nettoyer_images(self):
        self.actions.append("nettoyer")


class FauxRegistre:
    """Propose la même nouvelle image (empreinte « sha256:neuve ») pour tout conteneur."""

    def __init__(self, version):
        self.version = version

    def empreinte(self, ref):
        return "sha256:neuve"

    def configuration(self, ref, empreinte, plateforme):
        return {"org.opencontainers.image.version": self.version}, []

    def version_par_etiquettes(self, ref, empreinte):
        return None                      # aucune étiquette sœur : la version reste inconnue


class FauxNotificateur:
    def __init__(self):
        self.envois = []

    def envoyer(self, titre, message, priorite=3):
        self.envois.append((titre, message, priorite))
        return True


def monter(mode="auto", actuelle="6.4.4", nouvelle="6.5.0", etiquette="latest",
           registre="ghcr.io", deja_a_jour=False, etat="running"):
    """Un radarr configuré qui tourne en `actuelle`, et un registre qui propose `nouvelle`."""
    dossier = Path(tempfile.mkdtemp())
    conf = Conf(fichier_etat=str(dossier / "etat.json"), exports=[],
                dossier_copies=str(dossier / "copies"), copies_conservees=3,
                observation=0, delai_sante=0, attentes_reessai=[300, 1500],
                ntfy_url="", ntfy_sujet="", ntfy_jeton="", lien="", kuma_push="",
                identifiants={}, conteneurs=[ConfConteneur("radarr", mode)],
                racines_donnees=["/volume1/docker"])
    docker = FauxDocker()
    docker.ajouter("radarr", f"{registre}/linuxserver/radarr:{etiquette}",
                   "sha256:neuve" if deja_a_jour else "sha256:vieille", actuelle, etat=etat)
    attentes = []
    moteur = Moteur(conf, docker, FauxRegistre(nouvelle), Etat(conf.fichier_etat),
                    FauxNotificateur(), attendre=attentes.append)
    return moteur, docker, attentes


class Scenarios(unittest.TestCase):
    def test_deja_a_jour_rien_ne_se_passe(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        bilan = moteur.passe()
        self.assertEqual(docker.actions, ["nettoyer"])
        self.assertEqual(moteur.notificateur.envois, [])     # silence : rien à signaler
        self.assertFalse(bilan.erreurs)

    def test_mise_a_jour_reussie_sans_notification(self):
        moteur, docker, _ = monter()
        bilan = moteur.passe()
        self.assertEqual(bilan.mis_a_jour, [("radarr", "6.4.4", "6.5.0")])
        self.assertEqual(docker.actions[:3], [
            "telecharger radarr", "arreter radarr",
            "etiqueter sha256:id-radarr ghcr.io/linuxserver/radarr:avant-maj"])
        self.assertIn("recreer radarr", docker.actions)
        self.assertIsNone(moteur.etat.blocage("radarr"))
        self.assertEqual(moteur.notificateur.envois, [])     # une réussite ne notifie pas

    def test_nouvelle_version_cassee_retour_arriere(self):
        moteur, docker, _ = monter()
        docker.etats_apres_recreation = ["exited", "running"]   # la neuve plante, l'ancienne repart
        bilan = moteur.passe()
        self.assertIn("etiqueter sha256:id-radarr ghcr.io/linuxserver/radarr:latest", docker.actions)
        self.assertEqual(docker.actions.count("recreer radarr"), 2)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "installation")
        self.assertFalse(bilan.urgent)
        titre, message, priorite = moteur.notificateur.envois[0]
        self.assertEqual(priorite, 4)
        self.assertIn("ancienne version remise en place", message)

    def test_retour_arriere_lui_meme_rate_alerte_urgente(self):
        moteur, docker, _ = monter()
        docker.etats_apres_recreation = ["exited", "exited"]    # ni la neuve ni l'ancienne ne repartent
        bilan = moteur.passe()
        self.assertTrue(bilan.urgent)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "retour_arriere")
        self.assertEqual(moteur.notificateur.envois[0][2], 5)

    def test_telechargement_rate_trois_fois_passe_en_manuel(self):
        moteur, docker, attentes = monter()
        docker.echecs_telechargement = 3
        bilan = moteur.passe()
        self.assertEqual(attentes, [300, 1500])                   # 5 min, puis 25 min
        self.assertEqual(docker.actions.count("telecharger radarr"), 3)
        self.assertNotIn("arreter radarr", docker.actions)        # le service n'a jamais été coupé
        blocage = moteur.etat.blocage("radarr")
        self.assertEqual(blocage["raison"], "telechargement")
        self.assertIn("TLS handshake timeout", blocage["erreur"])
        self.assertEqual(len(bilan.erreurs), 1)

    def test_telechargement_rate_une_fois_puis_reussit(self):
        moteur, docker, attentes = monter()
        docker.echecs_telechargement = 1
        bilan = moteur.passe()
        self.assertEqual(attentes, [300])
        self.assertEqual(len(bilan.mis_a_jour), 1)
        self.assertIsNone(moteur.etat.blocage("radarr"))

    def test_montee_majeure_bloquee_sans_rien_telecharger(self):
        moteur, docker, _ = monter(actuelle="6.4.4", nouvelle="7.0.0")
        moteur.passe()
        self.assertNotIn("telecharger radarr", docker.actions)
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "majeure")
        self.assertEqual(moteur.notificateur.envois[0][2], 3)
        self.assertIn("6.4.4 → 7.0.0", moteur.notificateur.envois[0][1])

    def test_etiquette_numerotee_pas_de_controle_de_majeure(self):
        # uptime-kuma:2 : la majeure ne peut pas changer, peu importe la version lue
        moteur, docker, _ = monter(etiquette="2", actuelle=None, nouvelle=None)
        bilan = moteur.passe()
        self.assertEqual(len(bilan.mis_a_jour), 1)

    def test_version_illisible_bloquee_par_prudence(self):
        moteur, docker, _ = monter(actuelle=None, nouvelle=None)
        moteur.passe()
        self.assertEqual(moteur.etat.blocage("radarr")["raison"], "majeure")
        self.assertIn("illisible", moteur.etat.blocage("radarr")["message"])

    def test_conteneur_arrete_laisse_tel_quel(self):
        moteur, docker, _ = monter(etat="exited")
        bilan = moteur.passe()
        self.assertNotIn("telecharger radarr", docker.actions)
        self.assertFalse(bilan.erreurs)

    def test_manuel_une_seule_notification_par_version(self):
        moteur, docker, _ = monter(mode="manuel")
        moteur.passe()
        moteur.passe()
        self.assertEqual(len(moteur.notificateur.envois), 1)
        self.assertNotIn("telecharger radarr", docker.actions)

    def test_bloque_puis_mise_a_jour_manuelle_reussie_repasse_en_auto(self):
        moteur, docker, _ = monter()
        moteur.etat.bloquer("radarr", "telechargement", "registre muet")
        moteur.passe()
        self.assertNotIn("telecharger radarr", docker.actions)   # bloqué : la nuit n'y touche plus
        moteur.appliquer("radarr")
        self.assertIn("recreer radarr", docker.actions)
        self.assertIsNone(moteur.etat.blocage("radarr"))          # de nouveau automatique

    def test_appliquer_passe_outre_la_montee_majeure(self):
        moteur, docker, _ = monter(actuelle="6.4.4", nouvelle="7.0.0")
        bilan = moteur.appliquer("radarr")
        self.assertEqual(len(bilan.mis_a_jour), 1)


class ModeChoisiDepuisLaPage(unittest.TestCase):
    def test_le_choix_prime_sur_la_configuration(self):
        moteur, docker, _ = monter(mode="auto")
        moteur.choisir_mode("radarr", "manuel")
        moteur.passe()
        self.assertNotIn("telecharger radarr", docker.actions)
        moteur.choisir_mode("radarr", None)                    # retour à la configuration
        moteur.passe()
        self.assertIn("telecharger radarr", docker.actions)

    def test_un_manuel_de_configuration_peut_passer_en_auto(self):
        moteur, docker, _ = monter(mode="manuel")
        moteur.choisir_mode("radarr", "auto")
        self.assertEqual(len(moteur.passe().mis_a_jour), 1)

    def test_le_mode_affiche_change_tout_de_suite(self):
        # le bug du 25/09 : le choix était enregistré, mais la page montrait l'ancien mode
        moteur, _, _ = monter(mode="auto")
        moteur.choisir_mode("radarr", "manuel")
        self.assertEqual(moteur.etat.conteneur("radarr")["mode"], "manuel")
        moteur.choisir_mode("radarr", None)
        self.assertEqual(moteur.etat.conteneur("radarr")["mode"], "auto")

    def test_conteneur_inconnu_refuse(self):
        moteur, _, _ = monter()
        with self.assertRaises(ValueError):
            moteur.choisir_mode("fantome", "auto")


class Decouverte(unittest.TestCase):
    def test_nouveau_conteneur_suivi_en_auto_et_signale_une_seule_fois(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        docker.ajouter("outil", "ghcr.io/editeur/outil:latest", "sha256:neuve", "1.0.0",
                       montages=[Montage("bind", "/volume1/docker/outil/config", "/config", True)])
        bilan = moteur.passe()
        self.assertEqual(len(bilan.nouveaux), 1)
        self.assertIn("/volume1/docker/outil/config", bilan.nouveaux[0][1])
        self.assertIn("Nouveaux conteneurs", moteur.notificateur.envois[0][1])
        moteur.passe()
        self.assertEqual(len(moteur.notificateur.envois), 1)   # pas de seconde notification

    def test_nouveau_conteneur_mis_a_jour_automatiquement(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        docker.ajouter("outil", "ghcr.io/editeur/outil:latest", "sha256:vieille", "6.4.4")
        bilan = moteur.passe()
        self.assertIn(("outil", "6.4.4", "6.5.0"), bilan.mis_a_jour)

    def test_image_construite_sur_place_introuvable_signalee_une_fois(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        docker.ajouter("maison", "portfolio-web", None, None)
        self.assertNotIn("maison", [cc.nom for cc in moteur.conteneurs_a_traiter()])
        # ...mais la raison est notée, pour que la page puisse l'afficher
        self.assertEqual(moteur.etat.donnees["ignores"]["maison"]["etiquette"], "local")
        moteur.passe()
        self.assertIn("maison", moteur.notificateur.envois[0][1])
        self.assertIn("pas mise à jour", moteur.notificateur.envois[0][1])
        moteur.passe()
        self.assertEqual(len(moteur.notificateur.envois), 1)   # une seule fois
        self.assertNotIn("maison", moteur.etat.donnees["conteneurs"])   # pas de ligne fantôme

    def test_conteneur_ephemere_ignore(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        docker.ajouter("passager", "ghcr.io/x/y:latest", "sha256:neuve", "1.0", ephemere=True)
        self.assertNotIn("passager", [cc.nom for cc in moteur.conteneurs_a_traiter()])

    def test_conteneur_hors_compose_reste_manuel_meme_si_on_choisit_auto(self):
        moteur, docker, _ = monter(deja_a_jour=True)
        docker.ajouter("isole", "ghcr.io/x/isole:latest", "sha256:vieille", "6.4.4", projet="")
        moteur.choisir_mode("isole", "auto")
        bilan = moteur.passe()
        self.assertNotIn("telecharger isole", docker.actions)
        self.assertEqual(bilan.en_attente[0][0], "isole")


class ApresMiseAJour(unittest.TestCase):
    """Le cas du 25/09 : la carte gardait la date d'avant la mise à jour."""

    def monter(self, **options):
        moteur, docker, attentes = monter(**options)
        moteur.conf.apres_mise_a_jour = ["/outils/versions.sh"]
        return moteur

    def test_lance_apres_une_mise_a_jour(self):
        moteur = self.monter()
        with mock.patch("compose_auto_update.moteur.executer") as executer:
            moteur.passe()
        executer.assert_called_once_with(["/outils/versions.sh"], delai=300)

    def test_rien_quand_rien_n_a_change(self):
        moteur = self.monter(deja_a_jour=True)
        with mock.patch("compose_auto_update.moteur.executer") as executer:
            moteur.passe()
        executer.assert_not_called()

    def test_un_echec_n_annule_pas_la_mise_a_jour(self):
        moteur = self.monter()
        with mock.patch("compose_auto_update.moteur.executer",
                        side_effect=ErreurCommande(["/outils/versions.sh"], 1, "boum")):
            bilan = moteur.passe()
        self.assertEqual(len(bilan.mis_a_jour), 1)
        self.assertFalse(bilan.erreurs)


class AvertissementLscr(unittest.TestCase):
    def test_signale_une_seule_fois_avec_la_bonne_correction(self):
        moteur, docker, _ = monter(registre="lscr.io", deja_a_jour=True)
        bilan = moteur.passe()
        self.assertEqual(len(bilan.avertissements), 1)
        self.assertIn("ghcr.io/linuxserver/radarr", bilan.avertissements[0][1])
        self.assertEqual(len(moteur.passe().avertissements), 0)

    def test_rien_a_signaler_via_ghcr(self):
        moteur, _, _ = monter(registre="ghcr.io", deja_a_jour=True)
        self.assertEqual(moteur.passe().avertissements, [])


if __name__ == "__main__":
    unittest.main()
