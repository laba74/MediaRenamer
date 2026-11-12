# MediaRenamer — Plan du projet

## 1. Objectif

MediaRenamer est un outil en ligne de commande configurable qui renomme des photos et vidéos à partir de leurs métadonnées, avec des libellés et des corrections horaires propres aux appareils. Le projet vise un usage ponctuel, simple à relancer et facile à maintenir sur le long terme. Il ne modifie pas les métadonnées d’origine, ne classe pas les fichiers en sous-dossiers et ne déduplique pas le contenu.

## 2. Principes de fonctionnement

1. Parcourir le dossier source et sélectionner les extensions définies dans `conf/config.json` (récursivement avec `--recursive`).
2. Extraire les métadonnées avec ExifTool, appelé comme programme externe.
3. Associer chaque fichier à un appareil via `Make`, `Model` et, si disponible, `SerialNumber` dans `conf/devices.json`.
4. Appliquer l’offset configuré à la date EXIF retenue pour calculer la date effective du nom. Ne jamais écrire dans les métadonnées originales.
5. Construire une seule fois le nom cible avec la même logique en aperçu et en application.
6. Sans `--apply`, afficher un aperçu sans modifier les fichiers; avec `--apply`, effectuer les renommages ou copies demandés.

## 3. Règles fonctionnelles

- **Appareil inconnu :** en mode interactif, demander un libellé et un offset, enregistrer la réponse dans `conf/devices.json`, puis utiliser cette définition pour tous les fichiers correspondants du lot. Résoudre les appareils inconnus avant toute modification de fichiers. Prévoir une erreur claire en mode non interactif si une réponse est impossible.
- **Appariement :** comparaison insensible à la casse, première entrée correspondante; un numéro de série vide n’est pas un critère.
- **Dates et fuseaux :** documenter la source de date choisie et les limites des métadonnées sans fuseau. Normaliser en UTC uniquement lorsque le fuseau de la date est connu ou explicitement fourni; permettre une politique de fuseau pour le lot (`--tz`). Signaler les heures ambiguës ou inexistantes plutôt que d’inventer silencieusement une conversion. Les offsets d’appareil corrigent l’horloge, ils ne remplacent pas le fuseau.
- **Fichiers auxiliaires GoPro (`.LRV`, `.THM`) :** les associer au fichier vidéo principal de même base, reprendre sa date et son appareil, puis conserver l’extension. En l’absence du principal dans le lot, avertir et ne pas renommer.
- **Métadonnées absentes/illisibles :** rapporter l’erreur, ignorer le fichier et ne jamais utiliser silencieusement la date du système.
- **Collisions :** planifier des noms cibles uniques et ajouter `_01`, `_02`, etc. avant l’extension si nécessaire, en tenant compte des fichiers déjà présents et de l’insensibilité à la casse sur certains systèmes.
- **Récursivité :** avec `--recursive`, analyser tous les sous-répertoires de la source; sans l'option, seul le dossier source est lu. Elle ne met jamais l'arborescence à plat. Exclure `.logs/` et, si elle se trouve dans la source, la destination.
- **Destination :** sans `--dest`, renommer en place (chaque fichier reste dans son sous-dossier); avec `--dest`, copier sous le nom calculé et laisser les sources intactes. En mode récursif, recréer sous `--dest` l'arborescence relative d'origine, en ne créant que les dossiers qui reçoivent au moins un fichier (jamais de répertoire vide, y compris en aperçu). Prévenir les écrasements.
- **Portée par dossier :** les collisions (`_01`, `_02`…) et l'association des auxiliaires GoPro se résolvent dans le dossier cible / dossier d'origine du fichier, pas sur l'ensemble du lot.
- **Journal et rapport :** afficher un bilan d’exécution et conserver un journal dans `.logs/rename.log`, sans exposer inutilement de données personnelles des métadonnées.

## 4. Nommage et configuration

`conf/config.json` définit le modèle de nom, les extensions, le mode aperçu par défaut et le niveau de journalisation. Tokens prévus : `{YYYY}`, `{MM}`, `{DD}`, `{HH}`, `{MIN}`, `{SEC}`, `{DEVICE}`, `{ORIGINAL}`. Le modèle doit être validé au démarrage; les caractères invalides et les chemins inattendus sont refusés ou nettoyés de manière déterministe.

`conf/devices.json` contient une liste d’appareils avec `id`, critères `match`, `label` et `offset` au format signé `HH:MM:SS`. Un appareil peut également avoir un fuseau IANA si nécessaire. Valider les JSON, doublons et offsets avant le traitement.

### Fichiers par défaut et fichiers personnels

Le contenu de `conf/config.json` et surtout de `conf/devices.json` (marques, modèles, numéros de série, offsets) est personnel et évolue avec l’usage : l’outil réécrit `devices.json` à chaque nouvel appareil.

- **Modèles versionnés :** `conf/config.default.json` et `conf/devices.default.json` (liste vide) sont dans git et ne sont jamais modifiés par l’outil.
- **Fichiers personnels non versionnés :** `conf/config.json` et `conf/devices.json` sont dans `.gitignore`.
- **Création automatique :** si un fichier personnel est absent, l’outil le crée par copie du modèle et l’indique à l’utilisateur. Un fichier existant n’est jamais écrasé.
- **Écritures sûres :** les réécritures de `devices.json` passent par un fichier temporaire puis un remplacement atomique, après une copie `devices.json.bak`, pour qu’une interruption ne corrompe pas le fichier.
- **Sauvegarde :** la documentation utilisateur avertit que ces deux fichiers ne sont pas dans git, que leur perte oblige à ressaisir tous les appareils et offsets, et recommande de les copier régulièrement hors du dépôt.

## 5. Interface prévue

- `--source PATH` : dossier source requis hors mode vérification.
- `--dest PATH` : copier les fichiers renommés vers une destination (arborescence d'origine recréée avec `--recursive`, sans dossier vide).
- `--apply` : exécuter les opérations; aperçu par défaut.
- `--recursive` : analyser aussi les sous-dossiers (sans aplatir l'arborescence).
- `--device ID` : forcer l’appareil pour le lot.
- `--tz TIMEZONE` : préciser le fuseau de référence du lot.
- `--check` : valider la configuration et afficher la version/localisation d’ExifTool.
- `--help` : aide de commande.

## 6. Choix techniques et dépendances

- Python 3.8+ et bibliothèque standard uniquement pour le code applicatif.
- ExifTool, exécuté via `subprocess`; recherche dans `bin/` (sous-dossiers versionnés inclus, ex. `bin/exiftool-13.62_64/`), puis `PATH`, puis `EXIFTOOL_PATH`. Sous Windows, l’exécutable doit s’appeler `exiftool.exe` (la variante `exiftool(-k).exe` attend une touche à la fin et doit être renommée), à côté de son dossier `exiftool_files/`. Vérifier le binaire au démarrage et signaler clairement son absence.
- Pas de dépendances Python tierces à installer.
- Licence MIT pour le code original. ExifTool reste sous sa propre licence. Si un binaire tiers est redistribué, fournir ses avis et sa licence dans `THIRD-PARTY-NOTICES` ou `LICENSES/`; ne pas présenter l’ensemble comme étant sous MIT.

## 7. Arborescence de départ

```text
MediaRenamer/
├── media_renamer.py       # Point d’entrée
├── pyproject.toml         # Projet uv et configuration pytest
├── uv.lock                # Verrouillage des dépendances de développement
├── conf/
│   ├── config.default.json   # Modèle de configuration (versionné)
│   ├── devices.default.json  # Modèle d’appareils, liste vide (versionné)
│   ├── config.json           # Configuration personnelle (créée au 1er lancement; non versionnée)
│   └── devices.json          # Appareils personnels (créé au 1er lancement; non versionné)
├── docs/
│   ├── PLAN.MD            # Ce plan et les règles du projet
│   ├── BACKLOG.md         # Épopées et user stories
│   └── TROUBLESHOOTING.md # Guide de dépannage
├── tests/                 # Tests pytest (test_<module>.py par module)
├── README.md              # Présentation et guide de démarrage
├── AGENTS.md              # Règles de code (humains et agents IA)
├── LICENSE                # MIT pour le code original
├── THIRD-PARTY-NOTICES    # Licences des composants tiers, si redistribués
├── .gitignore
├── bin/                   # ExifTool déposé manuellement (ex. bin/exiftool-13.62_64/); non versionné
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
