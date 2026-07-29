# Pro-Avif Admin Dashboard

Tableau de bord Flask permettant de contrôler qui a le droit d'utiliser l'application Pro-Avif (Flutter) : blocage global avec raison, licences permanentes ou temporaires, et activation/désactivation de comptes individuels.

## Principe

Chaque client qui utilise l'application Pro-Avif possède **son propre backend et sa propre base MongoDB**. Ce tableau de bord ne dépend d'aucune API ni d'aucun backend fixe : il se connecte **directement** à la base MongoDB de chaque client (via son URI de connexion) pour lire et modifier :

- la collection `app_license` (blocage / licence),
- la collection `users` (activation / désactivation individuelle).

Chaque base cliente ajoutée est identifiée par un **nom** (ex. "Ferme Dupont SARL") pour savoir à qui elle appartient. On peut ainsi gérer autant de clients que nécessaire depuis un seul et même tableau de bord.

Sa propre base de données (comptes admin du dashboard, liste des bases clientes, raisons de blocage prédéfinies, journal d'actions) est un simple fichier **SQLite** local (`instance/admin.db`) — jamais commité, jamais partagé avec les clients.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# éditer .env si besoin (SECRET_KEY, identifiants du premier compte admin)
```

## Lancer en local

```bash
python app.py
```

Ouvre `http://127.0.0.1:5050`. Un compte admin par défaut est créé automatiquement au premier démarrage (voir `.env` : `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`, `admin` / `admin123` par défaut) — **à changer immédiatement en production**, via la page "Équipe Admin".

Ensuite, depuis la page "Bases clients", ajoute la base MongoDB de chaque client via le bouton "Ajouter une base client" (nom du client, URI de connexion MongoDB, nom de la base).

## Fonctionnalités

- **Bases clients** : ajouter/retirer la connexion vers la base MongoDB de chaque client, avec statut en direct (active / bloquée / injoignable) et compteurs d'utilisateurs.
- **Licence & Blocage** (par client) : bloquer instantanément l'accès pour tous les utilisateurs d'un client avec une raison (ex. "Version d'essai terminée"), ou débloquer. Attribuer une licence permanente ou temporaire (durée en jours) qui expire automatiquement côté backend du client (le champ `app_license.license_end` est vérifié à chaque connexion).
- **Raisons de blocage** : liste réutilisable de raisons prédéfinies, personnalisable, commune à tous les clients.
- **Utilisateurs** (par client) : liste des comptes de l'app pour ce client, activation/désactivation individuelle.
- **Équipe Admin** : gestion des comptes ayant accès à ce tableau de bord.
- **Journal** : historique des actions effectuées depuis ce tableau de bord.

## Prérequis côté backend client

Le backend FastAPI de chaque client (`Pro-avif-2026-Backend`) doit inclure la collection `app_license` et la vérification associée au login (déjà en place dans `database.py` / `routers/auth.py` / `routers/license.py`), ainsi que l'app Flutter à jour (écran de blocage). Sans ces modifications côté client, le blocage réalisé ici depuis MongoDB n'aura aucun effet visible dans l'app.

## Déploiement

Comme pour les autres dashboards du projet, ce tableau de bord est prévu pour tourner avec un serveur WSGI (ex. gunicorn) derrière un reverse proxy, avec les variables d'environnement définies sur le serveur plutôt que via `.env`.
