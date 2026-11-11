# MediaRenamer — Plan du projet

## 1. Objectif

MediaRenamer est un outil en ligne de commande configurable qui renomme des photos et vidéos à partir de leurs métadonnées, avec des libellés et des corrections horaires propres aux appareils. Le projet vise un usage ponctuel, simple à relancer et facile à maintenir sur le long terme. Il ne modifie pas les métadonnées d’origine, ne classe pas les fichiers en sous-dossiers et ne déduplique pas le contenu.

## 2. Principes de fonctionnement

1. Parcourir le dossier source et sélectionner les extensions définies dans `config.json` (récursivement avec `--recursive`).
2. Extraire les métadonnées avec ExifTool, appelé comme programme externe.
3. Associer chaque fichier à un appareil via `Make`, `Model` et, si disponible, `SerialNumber` dans `devices.json`.
4. Appliquer l’offset configuré à la date EXIF retenue pour calculer la date effective du nom. Ne jamais écrire dans les métadonnées originales.
5. Construire une seule fois le nom cible avec la même logique en aperçu et en application.
6. Sans `--apply`, afficher un aperçu sans modifier les fichiers; avec `--apply`, effectuer les renommages ou copies demandés.

## 3. Règles fonctionnelles

- **Appareil inconnu :** en mode interactif, demander un libellé et un offset, enregistrer la réponse dans `devices.json`, puis utiliser cette définition pour tous les fichiers correspondants du lot. Résoudre les appareils inconnus avant toute modification de fichiers. Prévoir une erreur claire en mode non interactif si une réponse est impossible.
- **Appariement :** comparaison insensible à la casse, première entrée correspondante; un numéro de série vide n’est pas un critère.
- **Dates et fuseaux :** documenter la source de date choisie et les limites des métadonnées sans fuseau. Normaliser en UTC uniquement lorsque le fuseau de la date est connu ou explicitement fourni; permettre une politique de fuseau pour le lot (`--tz`). Signaler les heures ambiguës ou inexistantes plutôt que d’inventer silencieusement une conversion. Les offsets d’appareil corrigent l’horloge, ils ne remplacent pas le fuseau.
- **Fichiers auxiliaires GoPro (`.LRV`, `.THM`) :** les associer au fichier vidéo principal de même base, reprendre sa date et son appareil, puis conserver l’extension. En l’absence du principal dans le lot, avertir et ne pas renommer.
- **Métadonnées absentes/illisibles :** rapporter l’erreur, ignorer le fichier et ne jamais utiliser silencieusement la date du système.
- **Collisions :** planifier des noms cibles uniques et ajouter `_01`, `_02`, etc. avant l’extension si nécessaire, en tenant compte des fichiers déjà présents et de l’insensibilité à la casse sur certains systèmes.
- **Destination :** sans `--dest`, renommer en place; avec `--dest`, copier sous le nom calculé et laisser les sources intactes. Prévenir les écrasements.
- **Journal et rapport :** afficher un bilan d’exécution et conserver un journal dans `.logs/rename.log`, sans exposer inutilement de données personnelles des métadonnées.

## 4. Nommage et configuration

`config.json` définit le modèle de nom, les extensions, le mode aperçu par défaut et le niveau de journalisation. Tokens prévus : `{YYYY}`, `{MM}`, `{DD}`, `{HH}`, `{MIN}`, `{SEC}`, `{DEVICE}`, `{ORIGINAL}`. Le modèle doit être validé au démarrage; les caractères invalides et les chemins inattendus sont refusés ou nettoyés de manière déterministe.

`devices.json` contient une liste d’appareils avec `id`, critères `match`, `label` et `offset` au format signé `HH:MM:SS`. Un appareil peut également avoir un fuseau IANA si nécessaire. Valider les JSON, doublons et offsets avant le traitement.

## 5. Interface prévue

- `--source PATH` : dossier source requis hors mode vérification.
- `--dest PATH` : copier les fichiers renommés vers une destination.
- `--apply` : exécuter les opérations; aperçu par défaut.
- `--recursive` : inclure les sous-dossiers.
- `--device ID` : forcer l’appareil pour le lot.
- `--tz TIMEZONE` : préciser le fuseau de référence du lot.
- `--check` : valider la configuration et afficher la version/localisation d’ExifTool.
- `--help` : aide de commande.

## 6. Choix techniques et dépendances

- Python 3.8+ et bibliothèque standard uniquement pour le code applicatif.
- ExifTool, exécuté via `subprocess`; recherche dans `bin/`, puis `PATH`, puis `EXIFTOOL_PATH`. Vérifier le binaire au démarrage et signaler clairement son absence.
- Pas de dépendances Python tierces à installer.
- Licence MIT pour le code original. ExifTool reste sous sa propre licence. Si un binaire tiers est redistribué, fournir ses avis et sa licence dans `THIRD-PARTY-NOTICES` ou `LICENSES/`; ne pas présenter l’ensemble comme étant sous MIT.

## 7. Arborescence de départ

```text
MediaRenamer/
├── media_renamer.py       # Point d’entrée (à implémenter)
├── config.json            # Configuration d’exemple
├── devices.json           # Appareils configurés (initialement vide)
├── PLAN.MD                # Ce plan et les règles du projet
├── README.md              # Présentation et guide de démarrage
├── LICENSE                # MIT pour le code original
├── THIRD-PARTY-NOTICES    # Licences des composants tiers, si redistribués
├── .gitignore
├── bin/                   # ExifTool déposé manuellement; non versionné
└── .logs/                 # Journaux d’exécution; non versionnés
```

## 8. Pérennité et documentation de dépannage

Le projet privilégie des formats de configuration simples, une dépendance externe explicite et des messages d’erreur exploitables. La documentation utilisateur doit expliquer comment vérifier ExifTool, inspecter les tags d’un fichier, corriger une entrée d’appareil, ajuster un offset, diagnostiquer l’absence de date, les fuseaux et les collisions. Il s’agit d’une philosophie de maintenabilité, pas d’une promesse d’absence de mises à jour ou de problèmes.

## 9. Étapes de réalisation

1. Implémenter la validation de configuration et `--check`.
2. Extraire métadonnées et dates, identifier les appareils et gérer l’interaction.
3. Construire un plan déterministe, appariement des auxiliaires et résolution des collisions.
4. Ajouter aperçu et application/copie avec protections contre les écrasements.
5. Produire rapports et journal; documenter installation, usage et dépannage.
6. Tester les cas d’erreur, les formats de date, les fuseaux, les noms en collision et les systèmes de fichiers usuels.
