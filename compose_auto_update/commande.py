"""Exécution des commandes système.

⚠️ C'EST LE SEUL ENDROIT DU PROGRAMME QUI LANCE UN PROCESSUS. Tout passe par
`executer()` : un seul point à surveiller, à remplacer dans les tests, et où
la même règle s'applique partout.
"""

import logging
import subprocess

journal = logging.getLogger(__name__)


class ErreurCommande(Exception):
    """Une commande a échoué. `erreur` contient ce qu'elle a écrit en sortie d'erreur."""

    def __init__(self, arguments, code, erreur):
        self.arguments = arguments
        self.code = code
        self.erreur = (erreur or "").strip()
        super().__init__(f"{' '.join(arguments)} a échoué (code {code}) : {self.erreur}")


def executer(arguments, delai=600):
    """Lance une commande et renvoie sa sortie standard.

    ⚠️ JAMAIS DE SHELL. Les arguments sont passés en liste : un nom de conteneur
    ou un chemin qui contiendrait un espace ou un « ; » reste UN argument, il ne
    peut pas devenir une deuxième commande.
    """
    journal.debug("commande : %s", " ".join(arguments))
    try:
        resultat = subprocess.run(arguments, capture_output=True, text=True,
                                  timeout=delai, check=False)
    except subprocess.TimeoutExpired:
        raise ErreurCommande(arguments, -1, f"délai de {delai} s dépassé") from None
    if resultat.returncode != 0:
        raise ErreurCommande(arguments, resultat.returncode, resultat.stderr or resultat.stdout)
    return resultat.stdout
