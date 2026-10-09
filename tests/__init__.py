"""Tests de compose-auto-update.

⚠️ UN SEUL DOSSIER TEMPORAIRE POUR TOUTE LA SUITE (09/10/2026). Les tests créent
leurs dossiers par `tempfile.mkdtemp()` sans les effacer : chaque passage en
laissait une centaine dans /tmp. Sur le NAS, où les tests précèdent chaque
déploiement, /tmp est en mémoire vive. Tous ces dossiers naissent désormais dans
un dossier commun, effacé à la fin.
"""
import atexit
import shutil
import tempfile

tempfile.tempdir = tempfile.mkdtemp(prefix="cau-tests-")
atexit.register(shutil.rmtree, tempfile.tempdir, True)
