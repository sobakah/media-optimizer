# Media Optimizer Suite

Bash-Skripte zur automatisierten Konvertierung und Größenreduzierung von
Bildern, GIFs und Videos. Ein Orchestrator ruft die Unter-Skripte
nacheinander auf, jedes davon läuft auch einzeln.

## Inhalt

- [Kernfunktionen](#kernfunktionen) · [Systemanforderungen](#systemanforderungen) · [Aufbau](#aufbau)
- [Nutzung](#nutzung) · [Skripte und Optionen](#skripte-und-optionen) · [Konfiguration](#konfiguration)
- [Beachtenswertes](#beachtenswertes) · [Formatwahl](#formatwahl-was-lohnt-sich) · [Fehlersuche](#fehlersuche) · [Grenzen](#bekannte-grenzen)

## Kernfunktionen

* **Zentrale Steuerung.** `media-optimizer.sh` zählt die Dateitypen im
  Quellordner und startet die passenden Unter-Skripte.
* **Fortsetzbar.** `Strg+C` speichert Fortschritt, Statistiken und Parameter;
  der nächste Start setzt nahtlos fort.
* **Sicheres Löschen.** Originale wandern über `gio trash` oder `trash-cli` in
  den Papierkorb. Schlägt das fehl, wird nichts gelöscht, sondern am Ende
  einmal nachgefragt.
* **Verifikation vor dem Löschen.** Eine Ausgabe wird erst akzeptiert, wenn sie
  lesbar ist; bei Videos zusätzlich mit Abgleich von Laufzeit und Bildinhalt.
* **Dateisystem-Spiegelung.** In-Place oder in ein separates Zielverzeichnis
  unter Beibehaltung der Ordnerstruktur.
* **Dry-Run.** `-n` spielt jeden Aufruf durch, ohne zu schreiben oder zu löschen.
* **Konfigurationsdatei** statt Einstellungen im Skriptkopf.

## Systemanforderungen

**Fedora / RHEL / Bazzite:**
```bash
sudo dnf install ffmpeg libjxl jxl-tools libwebp-tools file trash-cli libva-utils
```

**Debian / Ubuntu / Linux Mint:**
```bash
sudo apt install ffmpeg libjxl-tools webp file trash-cli vainfo
```

`gio` ist in GNOME und KDE enthalten, `trash-cli` dient als Ersatz.
`libva-utils` bzw. `vainfo` wird nur zur Fehlersuche beim GPU-Encoding
gebraucht. Für AVIF braucht ffmpeg einen AV1-Encoder (`libsvtav1` oder
`libaom-av1`).

Der Orchestrator prüft alle Abhängigkeiten beim Start und bricht ab, statt
nach halbem Durchlauf zu scheitern.

## Aufbau

```
media-optimizer.sh          Orchestrator: Pre-Flight, Stufen, Resume
media-optimizer.conf        Einstellungen (optional)
scripts/img-to-jxl.sh       JPG/PNG  -> JXL, WebP als Fallback
scripts/gif-to-webp.sh      GIF      -> animiertes WebP oder AVIF
scripts/h264-to-h265.sh     H.264    -> HEVC (GPU oder CPU)
scripts/video-to-avif.sh    kurze tonlose Videos -> AVIF  (optional)
scripts/verify-output.sh    Ausgaben gegen die Originale prüfen
scripts/lib/common.sh       gemeinsame Funktionen, wird gesourct
```

Die drei ersten Skripte laufen im normalen Durchlauf. `video-to-avif.sh` ist
eine abschaltbare Zusatzstufe, standardmäßig aus (`--avif` bzw.
`ENABLE_AVIF_STAGE=true`). `verify-output.sh` wird immer einzeln aufgerufen.

Die Stufen laufen in dieser Reihenfolge, die Nummerierung passt sich an:
Bilder, GIFs, AVIF, Videos. AVIF steht **vor** dem HEVC-Encoding, damit kurze
tonlose Clips als AVIF enden; die HEVC-Stufe überspringt danach jede Quelle,
für die bereits eine AVIF-Ausgabe mit gleichem Namen existiert.

## Nutzung

```bash
./media-optimizer.sh -n ~/Bilder                    # erst ansehen
./media-optimizer.sh ~/Bilder                       # In-Place
./media-optimizer.sh ~/Bilder /mnt/archiv --delete  # Ziel + Originale entsorgen
```

Ohne Zielverzeichnis wird im Quellordner gearbeitet. Unter-Skripte lassen sich
einzeln aufrufen und zeigen dann ein eigenes Menü:

```bash
./scripts/h264-to-h265.sh ~/Videos
./scripts/h264-to-h265.sh -i ~/Videos --encoder cpu --crf 20
```

Positionsargumente und Optionen sind mischbar, `--help` gibt es überall.

## Skripte und Optionen

Optionen, die in mehreren Skripten gleich funktionieren:

| Option | Wirkung |
|---|---|
| `-i, --input <dir>` | Quellverzeichnis |
| `-o, --output <dir>` | Zielverzeichnis, leer = In-Place |
| `-j, --workers <n>` | parallele Worker (Default: `nproc`) |
| `--delete` / `--no-delete` | Originale in den Papierkorb (Default: siehe unten) |
| `--force-delete` | bei fehlgeschlagenem Papierkorb ohne Rückfrage `rm` |
| `--keep-larger` | Ergebnis auch behalten, wenn es größer ist |
| `-n, --dry-run` | nur anzeigen |
| `--hold` / `--no-hold` | Fenster am Ende offen halten |
| `-h, --help` | Hilfe |

### `media-optimizer.sh` (Orchestrator)

Zählt die Dateitypen, startet die Stufen und verwaltet den Resume-Zustand.
Warnt vorab, wenn gleichnamige Dateien mit verschiedenen Endungen vorliegen
(`foo.jpg` und `foo.png` zeigen beide auf `foo.jxl`).

```
      --preflight      Dateiendungen vorab per MIME-Typ korrigieren
      --verify-deep    Bildausgaben vollständig dekodieren (langsamer)
      --avif           Zusatzstufe: kurze, tonlose Videos nach AVIF
      --no-avif        diese Stufe überspringen (Default)
      --log <datei>    Protokolldatei (Default: media-optimizer.log daneben)
      --no-log         kein Protokoll schreiben
  -y, --yes            keine Rückfragen, Standardwerte verwenden
      --reset          gespeicherten Zustand verwerfen
```

### `img-to-jxl.sh` (Bilder)

JPEG wird bit-exakt verlustfrei nach JXL transcodiert. PNG wird verlustfrei
oder mit einstellbarer Qualität konvertiert; schlägt das fehl, greift ein
verlustfreier WebP-Fallback.

Passt die Endung nicht zum Inhalt (etwa ein WebP mit `.jpg`), hängt die
Behandlung vom Modus ab. **Mit Zielverzeichnis bleibt die Quelle
unangetastet**: die Datei landet unter dem richtigen Namen im Ziel. In-Place
wird die Quelldatei umbenannt, denn dort ist genau das der Zweck. Dasselbe
gilt für `--preflight` im Orchestrator, der mit Zielverzeichnis nur meldet
statt umzubenennen.

```
  -e, --effort <1-9>     JXL Effort (Default: 7)
      --png-mode <m>     lossless | lossy (Default: lossless)
      --png-quality <q>  nur bei lossy (Default: 90)
      --cjxl-threads <n> Threads je cjxl-Prozess (Default: 1)
      --verify-deep      Ausgabe vollständig dekodieren
```

### `gif-to-webp.sh` (animierte GIFs)

Wandelt GIFs in animiertes WebP (verlustfrei) oder AVIF (kleiner, verlustbehaftet).

```
  -m, --method <0-6>  WebP-Kompressionsstufe (Default: 6)
      --target <f>    webp | avif (Default: webp)
      --avif-crf <n>  AV1-Qualität bei --target avif (Default: 20)
      --verify-deep   Ausgabe mit webpinfo prüfen
```

### `h264-to-h265.sh` (Videos)

Reencoder von AVC/H.264 nach HEVC. Wählt anhand der Quell-Bitrate zwischen
Hardware-Encoding (VA-API oder Vulkan) und Software-Encoding (libx265).

```
      --encoder <mode>     auto | gpu | cpu (Default: auto)
      --threshold <kbps>   Schwelle CPU/GPU im auto-Modus (Default: 3500)
      --qp <n>             GPU-Qualität (Default: 26)
      --crf <n>            CPU CRF (Default: 22)
      --preset <p>         x265-Preset (Default: medium)
      --x265-params <s>    x265-Parameter (Default: aq-mode=3:no-sao=1)
      --min-size <mb>      Dateien darunter überspringen (Default: 5)
      --no-probe           kein Testslice vorab
      --probe-margin <p>   überspringen ab p % des Originals (Default: 90)
      --probe-duration <s> Länge des Testslice (Default: 10)
      --from-list <datei|auto>  nur die gelisteten Quelldateien
      --rename-inplace     _h265-Suffix nach dem Löschen des Originals entfernen
      --hevc-tag <t>       Container-Tag, z. B. hvc1 (Default: keiner)
      --gpu-codec <c>      hevc_vaapi | av1_vaapi | hevc_vulkan
      --gpu-device <p>     Render-Node oder "auto"
      --rc-mode <m>        CQP | VBR | ICQ | QVBR | CBR (Default: CQP)
      --bf <n>             max. B-Frames auf der GPU
      --low-power          VAAPI Low-Power-Encoder
      --force-gpu          Startprüfung überspringen
      --gpu-selftest <datei>  Optionsvarianten durchprobieren, dann beenden
      --gpu-10bit          10-Bit-Quellen in 10 Bit kodieren
      --no-verify-visual   keinen PSNR-Vergleich
      --strict-visual      auffällige Ausgaben verwerfen statt nur warnen
      --no-faststart       moov-Atom nicht nach vorn schreiben
      --no-cache           Cache-Datei ignorieren
```

Wesentliche Abläufe:

* **Probe-Slice.** Vor dem Encoding wird ein Ausschnitt aus der Dateimitte
  kodiert und mit demselben Ausschnitt des Originals verglichen (beides ohne
  Audio, gleiche Länge, über die Dateigröße gemessen). Übersprungen wird,
  sobald weniger als `100 − --probe-margin` Prozent Ersparnis zu erwarten sind.
* **Fehlertoleranz.** Meldet der Decoder Korruption, werden die intakten
  Frames gerettet. Bei solchen Dateien bleibt das Original immer erhalten,
  auch mit `--delete`.
* **Größenprüfung.** Ergebnisse, die größer als das Original sind, werden
  verworfen.
* **Fortschritt.** `>>> [12/347] Verarbeite: …`. Der Zähler läuft über alle
  gefundenen Kandidaten, auch über die still übersprungenen; am Terminal
  zeigt eine sich selbst überschreibende Zeile, wie weit das Überspringen ist.
* **Listenmodus.** `--from-list` verarbeitet nur die aufgeführten Quellpfade
  (einer pro Zeile, `#` ist Kommentar) und ignoriert dabei Cache und
  vorhandene Ausgaben. `auto` nimmt `.defekte_videos.txt` aus dem
  Zielverzeichnis, falls vorhanden.

### `video-to-avif.sh` (optionale Stufe)

Wird über `--avif` in den Durchlauf aufgenommen oder einzeln aufgerufen.
Wandelt kurze, tonlose Videos in animiertes AVIF. Ausgewählt wird nur, was
alle drei Kriterien erfüllt: Codec H.264 oder H.265, Laufzeit unter
`--max-seconds`, keine Tonspur. Die Tonbedingung ist notwendig, weil AVIF
keinen Ton speichern kann.

```
      --max-seconds <s> nur Videos kürzer als s (Default: 10)
      --crf <n>         AV1-Qualität (Default: 32)
      --crf-retry <n>   Aufschlag beim zweiten Versuch (Default: 6)
      --preset <n>      SVT-AV1 Preset 0-13 (Default: 6)
      --pix-fmt <p>     yuv420p | yuv444p (Default: yuv420p)
      --allow-audio     auch Videos mit Ton (Ton geht verloren)
      --no-verify       keinen PSNR-Vergleich
```

Wird die Ausgabe größer als das Original, läuft ein zweiter Versuch mit
höherem CRF, erst danach wird verworfen.

### `verify-output.sh` (Kontrolle und Reparatur)

Prüft ein Zielverzeichnis gegen das Quellverzeichnis. Die Zuordnung ist der
relative Pfad.

```
      --psnr-min <db>    Schwelle für Verdacht (Default: 15)
      --duration-tol <p> erlaubte Laufzeitabweichung in Prozent (Default: 2)
      --samples <n>      Stichproben pro Datei (Default: 3)
      --keep-samples <d> verglichene Einzelbilder ablegen
      --fix              defekte Ausgaben löschen und aus dem Cache nehmen
      --run              nach --fix den Reparaturlauf starten
```

| Meldung | Bedeutung |
|---|---|
| `[UNLESBAR]` | `ffprobe` findet keinen Videostream |
| `[DAUER]` | Laufzeit weicht über die Toleranz hinaus ab |
| `[BILD?]` | Laufzeit stimmt, Bildvergleich liegt unter der PSNR-Schwelle |

Ein niedriger PSNR ist ein **Verdacht, kein Beweis**. Ohne `--fix` wird nur
berichtet. `--keep-samples` legt die verglichenen Einzelbilder ab, damit sich
jede Meldung selbst beurteilen lässt. `--run` startet den Reparaturlauf mit
`--from-list` auf der Defektliste, verarbeitet also nur die gemeldeten
Dateien, und archiviert die Liste anschließend mit Zeitstempel.

`[DAUER]` trifft auch **gerettete** Dateien, die aus beschädigten Quellen
legitim kürzer sind. Die Meldung nennt deshalb beide Laufzeiten;
`--duration-tol` hebt die Grenze an.

### Protokoll

Jeder Lauf hängt einen Block an `media-optimizer.log` an, die Datei liegt
neben `media-optimizer.sh`. Sie enthält die verwendeten Einstellungen, eine
Zeile je bearbeiteter Datei und die Auswertung je Stufe:

```
================================================================
LAUF 260911-120014-926  gestartet 2026-09-11 12:00:14
  Aufruf : media-optimizer.sh
  Quelle : /tmp/lg2
  Ziel   : (In-Place)
  Einstellungen:
    DELETE_ORIGINAL        true
    ...
----------------------------------------------------------------
12:00:14  start  Stufe Bilder (2 Kandidaten)
12:00:14  img    OK        /tmp/lg2/a.jpg -> a.jxl (8.85 KB -> 7.11 KB, -19.6%)
12:00:14  img    Auswertung: 2 konvertiert, 0 übersprungen, 0 fehlgeschlagen
12:00:16  h265   OK        /tmp/lg2/unter/v.mp4 -> v_h265.mp4 (214.05 KB -> 34.85 KB, -83.7%)
----------------------------------------------------------------
LAUF 260911-120014-926  beendet 2026-09-11 12:00:16  (Code 0)
================================================================
```

Die Statuswerte je Datei sind `OK`, `VERWORFEN`, `KONFLIKT`, `PROBE`,
`GERETTET`, `BILD?`, `UNGUELTIG` und `FEHLER`. Übersprungene Dateien werden
nicht einzeln protokolliert, sonst wäre die Datei bei großen Beständen von
Cache-Treffern dominiert; sie erscheinen in der Auswertungszeile.

Ein Abbruch wird als eigene Zeile samt Exit-Code festgehalten. Die
Run-Kennung im Kopf taucht auch in der Schlusszeile auf, sodass sich
zusammengehörende Blöcke bei parallelen Läufen zuordnen lassen.

Einstellungen dazu, jeweils in der Konfigdatei, als Umgebungsvariable oder
per Flag:

| Variable | Flag | Default | Wirkung |
|---|---|---|---|
| `MO_LOG` | `--no-log` | `true` | Protokoll überhaupt schreiben |
| `MO_LOG_FILE` | `--log <datei>` | `media-optimizer.log` neben dem Skript | Pfad |
| `MO_LOG_MAX_KB` | — | `5120` | Rotationsgrenze in KB |

Überschreitet die Datei `MO_LOG_MAX_KB`, wird sie einmalig nach
`media-optimizer.log.1` verschoben. Ist der Pfad nicht schreibbar, läuft die
Konvertierung normal weiter und nur ein Hinweis erscheint.

### Vorbelegung nach Modus

`DELETE_ORIGINAL` und `RENAME_INPLACE` haben keinen festen Default, sondern
richten sich danach, ob ein Zielverzeichnis angegeben wurde:

| Modus | `DELETE_ORIGINAL` | `RENAME_INPLACE` | Hinweis |
|---|---|---|---|
| mit Zielverzeichnis | `false` | `false` | keine Meldung |
| In-Place | `true` | `true` | Hinweis mit Änderungsmöglichkeit |

Im In-Place-Modus liegt das Original sonst neben der Ausgabe und der
`_h265`-Suffix bliebe dauerhaft stehen. Weil das destruktiv ist, erscheint
ein Hinweis mit beiden Werten:

```
╔══════════════════════════════════════════════════════════════╗
║  IN-PLACE-MODUS: Originale werden veraendert                 ║
╚══════════════════════════════════════════════════════════════╝
  Kein Zielverzeichnis angegeben, daher gilt:
    Originale in den Papierkorb  : true
    _h265-Suffix danach entfernen: true
  Diese Einstellungen uebernehmen? [J/n]:
```

Der Hinweis erscheint auch dann, wenn zuvor „Standardeinstellungen nutzen"
gewählt wurde. Mit `n` lassen sich beide Werte einzeln ändern. Bei `-y` oder
ohne Terminal wird er nur angezeigt und die Werte gelten.

Eine ausdrückliche Angabe hat immer Vorrang und wird nicht überschrieben: per
CLI (`--delete`, `--no-delete`, `--rename-inplace`), per Umgebungsvariable
oder per Konfigdatei. In der mitgelieferten Konfig sind beide Werte deshalb
auskommentiert; wer dort etwas einträgt, legt es für beide Modi fest.

## Konfiguration

`media-optimizer.conf` wird geladen, wenn sie neben `media-optimizer.sh`
liegt. Alternativ `$XDG_CONFIG_HOME/media-optimizer.conf` oder ein Pfad in
`MO_CONFIG`. Löschen stellt die eingebauten Defaults wieder her.

Rangfolge: **CLI-Flag > interaktive Eingabe > Umgebungsvariable > Konfigdatei > Default.**

Die interaktive Abfrage (im Menü mit `n` auf „Standardeinstellungen nutzen?"
antworten) zeigt die Konfigwerte als Vorbelegung. Dort einstellbar sind
`MAX_WORKERS`, `DELETE_ORIGINAL`, `VERIFY_DEEP`, `JXL_EFFORT`, `PNG_MODE`,
`PNG_QUALITY`, `COMPRESSION_METHOD`, `ENCODER_MODE`, `BITRATE_THRESHOLD_KBPS`,
`GPU_QP`, `CPU_CRF`, `CPU_PRESET`, `ENABLE_PROBE`, `DISCARD_IF_LARGER`,
`KEEP_SALVAGED_CORRUPT` und der Endungs-Preflight.

Nur über Konfig, CLI oder Umgebung erreichbar: `CPU_X265_PARAMS`,
`VAAPI_DEVICE`, `GPU_CODEC`, `GPU_RC_MODE`, `GPU_BF`, `GPU_LOW_POWER`,
`HEVC_TAG`, `MIN_SIZE_MB`, `PROBE_MARGIN_PCT`, `PROBE_DURATION`,
`CJXL_THREADS`, `FASTSTART`, `GIF_KMIN`, `GIF_TARGET`, `USE_CACHE`,
`FORCE_DELETE`, `DURATION_TOLERANCE_PCT`, `RENAME_INPLACE`, `AUTO_FIX_LIST`,
`VISUAL_*` und `AVIF_*`.

### Hardware-Tuning

Die mitgelieferte Konfiguration ist auf Ryzen 7 9700X (8C/16T, Zen 5) mit
RX 9070 XT (RDNA 4) zugeschnitten.

* `pools` und `frame-threads` sind bewusst nicht gesetzt: x265 wählt auf
  16 Threads von selbst einen Pool über alle Threads und 4 Frame-Threads.
  Explizite Werte ändern nichts und engen bei einem Hardwarewechsel ein.
* `asm=avx512` ist bei x265 per Default aus, weil es auf CPUs mit halbiertem
  AVX-512-Datenpfad bremst. Zen 5 hat einen vollen Datenpfad. Die Messbefehle
  zum Gegenprüfen stehen als Kommentar in der Konfig.
* `CJXL_THREADS=1`, weil sonst jeder der 16 parallelen cjxl-Prozesse noch
  einmal 16 eigene Threads startet.

## Beachtenswertes

* **Atomares Schreiben.** Es wird immer zuerst in eine `.part`-Datei mit
  eindeutigem Namen geschrieben. Das Original wird erst ersetzt oder entsorgt,
  wenn die Ausgabe die Prüfung bestanden hat: bei Videos meldet `ffprobe` eine
  Laufzeit innerhalb von 2 % zum Original, bei Bildern ist die Datei nicht
  leer und mit `--verify-deep` zusätzlich vollständig dekodierbar.
* **Papierkorb.** Der Speicher wird erst frei, wenn der Papierkorb geleert
  wird. Scheitert das Verschieben (anderer Mount, kein `gio`/`trash-cli`),
  wird nichts gelöscht; die Pfade sammeln sich in
  `.<typ>_pending_deletes.txt` und am Ende kommt eine einmalige Rückfrage.
  Ohne Terminal bleiben die Originale erhalten.
* **Reste.** `.part`-Dateien und Lock-Verzeichnisse aus hart abgebrochenen
  Läufen werden beim nächsten Start entfernt.
* **Statusdateien** liegen versteckt im Quellverzeichnis: `.img_stats.env`,
  `.gif_stats.env`, `.h265_stats.env`, `.avif_stats.env`,
  `.video_conversion_cache.txt` und die Pending-Listen.
* **Video-Cache.** `.video_conversion_cache.txt` merkt sich abgeschlossene
  Dateien, damit ein späterer Lauf nicht erneut jede Datei per `ffprobe`
  anfasst. Eingetragen werden Quelldateien sowie, nur im In-Place-Modus, die
  erzeugten `_h265.mp4`. Verwaiste Einträge werden beim Start entfernt. Der
  Cache ist eine reine Beschleunigung; Löschen ändert nur die Laufzeit. Da
  absolute Pfade gespeichert werden, greift er nach einem Verschieben des
  Ordners nicht mehr.
* **Kandidatenzahl.** Der Fortschrittszähler zählt alle Dateien, die dem
  Suchmuster entsprechen, einschließlich der später übersprungenen. Im
  In-Place-Modus zählen die erzeugten `_h265.mp4` beim nächsten Lauf mit.
* **Zähler bei Fortsetzungen.** Geleistete Arbeit wird über Abbrüche hinweg
  fortgeschrieben. Die Cache-Treffer beschreiben dagegen den aktuellen Scan
  und werden bei jedem Start neu gezählt, sonst würde jede Fortsetzung
  dieselben Dateien erneut aufaddieren.
* **In-Place bei Videos.** Quelle und Ziel hätten denselben Pfad, deshalb
  heißt die Ausgabe `name_h265.mp4`. `--rename-inplace` entfernt den Suffix
  nach dem Löschen des Originals, aber nur wenn dessen Pfad wirklich frei ist.

### Start per Doppelklick

Aus einem Dateimanager oder über eine `.desktop`-Datei gestartet, schließt der
Terminal-Emulator das Fenster, sobald das Skript endet. Ein Exit-Handler hält
es deshalb offen, auch bei erfolgreichem Durchlauf, und nennt bei einem
Abbruch Exit-Code, Zeilennummer und den fehlgeschlagenen Befehl.

Die Erkennung wertet die Kommandozeile des Elternprozesses aus: eine
interaktive Shell hat kein Skript- und kein `-c`-Argument, ein
Terminal-Emulator dagegen schon. Aus dem Terminal gestartet wird also nicht
gewartet.

| Variable | Wirkung |
|---|---|
| `MO_HOLD=auto` | Default |
| `MO_HOLD=1` / `0` | immer / nie warten |
| `MO_HOLD_TIMEOUT=15` | nach 15 Sekunden von selbst schließen |

Für eine `.desktop`-Datei ist `MO_HOLD=1` die verlässlichste Wahl, weil die
Automatik nicht jede Startmethode kennen kann:

```ini
[Desktop Entry]
Type=Application
Name=Media Optimizer
Exec=env MO_HOLD=1 /pfad/zu/media-optimizer.sh
Terminal=true
```

Beim Start über den Orchestrator wartet nur dieser, nicht jede Stufe einzeln.

### Weiterer Durchlauf

Nach einem erfolgreichen Lauf fragt der Orchestrator:

```
  Weiteres Verzeichnis bearbeiten? [j/N]:
```

Mit `j` startet er sich mit denselben Optionen neu und fragt nach einem neuen
Quellverzeichnis; Enter beendet ihn. Die Verzeichnisangaben des ersten
Aufrufs werden dabei verworfen, alle übrigen Flags bleiben erhalten. Jeder
Durchlauf bekommt einen eigenen Block im Protokoll.

Die Frage ersetzt das Warten auf Enter beim Start per Doppelklick, es gibt
also nur eine Rückfrage. Mit `-y` oder ohne Terminal entfällt sie.

## Formatwahl: was lohnt sich?

Messwerte aus diesem Projekt, jeweils gegen die Originaldatei. WebP
verlustfrei ist bit-exakt und hat daher keinen endlichen PSNR.

**GIF-Quellen** (240×180, 3 s):

| Ziel | flächige Animation | fotoähnlich | PSNR |
|---|---|---|---|
| WebP verlustfrei (Default) | 66 % | 84 % | bit-exakt |
| AVIF CRF 20 | 46 % | 48 % | 47 / 39 dB |
| AVIF CRF 28 | 34 % | 29 % | 44 / 35 dB |

AVIF halbiert die Größe, ist dafür verlustbehaftet. Für GIFs mit Text und
harten Kanten halbiert `yuv420p` zusätzlich die Farbauflösung; `yuv444p`
kostete im Test 67 % statt 37 % für +1,6 dB, was PSNR allerdings schlecht
abbildet.

**Video-Quellen** (H.264 CRF 23, 480×360, 6 s, ohne Ton):

| Ziel | Größe vs Original | PSNR |
|---|---|---|
| AVIF CRF 18 | 160 % | 51 dB |
| AVIF CRF 28 | 112 % | 47 dB |
| AVIF CRF 32 (Default) | 90 % | 45 dB |
| AVIF CRF 35 | 75 % | 44 dB |

Die Quelle ist bereits H.264-komprimiert, AVIF muss das erst einholen. Unter
CRF 30 wird die Datei größer als das Original. Eine längere GOP ändert nichts,
SVT-AV1 nutzt ohnehin lange Abstände.

**Einschätzung.** Für GIFs ist AVIF ein echter Gewinn. Für kurze Videos ist es
eher Formatvereinheitlichung als Platzersparnis; wer Platz sparen will, fährt
mit dem HEVC- oder AV1-Pfad des Videoskripts besser, weil dort die Tonspur
erhalten bleibt und MP4 überall läuft.

**Anzeigeunterstützung.** Animiertes AVIF wird von Glycin unterstützt, also
von Loupe und den Nautilus-Vorschaubildern. Firefox animiert AVIF weiterhin
nicht. Ob die eigene Glycin-Version es kann, zeigt am schnellsten ein Blick
auf eine erzeugte Datei in Loupe.

## Fehlersuche

### GPU-Encoding startet nicht

`VAAPI_DEVICE="auto"` probiert alle Render-Nodes unter `/dev/dri/` durch.
Schlägt alles fehl, nennt das Skript den konkreten ffmpeg-Fehler. Häufige
Ursachen:

1. **Falsches Render-Node.** Bei CPU mit iGPU plus dGPU ist `renderD128` oft
   die integrierte Grafik. `ls -l /dev/dri/by-path/` ordnet die Nodes den
   PCI-Adressen zu.
2. **Fehlende Rechte.** `id | grep render`, sonst
   `sudo usermod -aG render $USER` und neu anmelden.
3. **Treiber zu alt.** RDNA 4 mit VCN 5 braucht Mesa 25.0+ und Kernel 6.13+.
4. **ffmpeg ohne VAAPI.** `ffmpeg -hide_banner -encoders | grep hevc_vaapi`
   muss eine Zeile liefern.
5. **Fedora.** Die Standardpakete sind aus Patentgründen beschnitten:
   `sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld`

Mit `--encoder gpu` bricht das Skript ab, statt still auf CPU zurückzufallen.
Scheitert die GPU erst an einer echten Datei, wird diese sofort auf der CPU
wiederholt und der Rest des Laufs bleibt dabei.

### GPU läuft, aber das Bild ist kaputt

Ein VAAPI-Encoder kann ohne Fehlermeldung unbrauchbare Bilder liefern; von
außen ist das nicht vom Erfolg zu unterscheiden. Typisches Bild: Fragmente im
oberen Bereich, der Rest einfarbig dunkelgrün. Das Grün ist ein mit Nullen
gefüllter YUV-Puffer, also Speicher, in den nie Bilddaten geschrieben wurden.

Der Selbsttest probiert Optionsvarianten an einer echten Datei durch und
bewertet jedes Ergebnis per Bildvergleich:

```bash
./scripts/h264-to-h265.sh --gpu-selftest /pfad/zu/video.mp4
```

```
  Variante      Beschreibung                       Ergebnis   PSNR
  original      Originalbefehl, Geraet nach -i     brauchbar  42 dB
  bf0           wie Original + -bf 0               brauchbar  42 dB
  hvc1          wie Original + -tag:v hvc1         KAPUTT      8 dB
  aktuell       aktuelle Skriptvorgabe             brauchbar  42 dB
  av1           av1_vaapi statt hevc_vaapi         brauchbar  47 dB
```

Bekannte Stolpersteine:

* **`-tag:v hvc1`.** Der Tag verlangt, dass VPS/SPS/PPS ausschließlich im
  `hvcC`-Kasten stehen. Liefert der Encoder sie im Datenstrom, baut der Muxer
  ein unvollständiges `hvcC`: korrekt kodiert, aber nicht mehr korrekt
  dekodierbar. Deshalb ist `HEVC_TAG` leer.
* **Pixelformat.** `format=nv12|p010` als Alternative zu schreiben überlässt
  dem Filter die Wahl. Fällt sie auf p010, während der Encoder in 8 Bit
  arbeitet, wird der Puffer nur teilweise gefüllt. Das Format wird deshalb
  fest gesetzt; 10 Bit nur mit `--gpu-10bit` und passendem Profil.
* **B-Frames.** Manche VCN-Generationen kommen über Mesa damit nicht zurecht.
  `--bf 0` probieren.
* **Ratenkontrolle.** `CQP` ist der klassische Weg, `--rc-mode VBR` die
  Alternative.
* **Anderer Encoder.** `--gpu-codec av1_vaapi` oder `hevc_vulkan` (ffmpeg 7.1+).

### Bereits erzeugte Dateien prüfen

```bash
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv --keep-samples /tmp/proben
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv --fix --run
```

## Bekannte Grenzen

* Nur `.mp4` wird als Videoquelle gesucht (bei `video-to-avif.sh` zusätzlich
  `.mov` und `.m4v`). MKV, AVI und TS bleiben unberührt.
* Videos laufen sequenziell. Bei CPU-Encoding lastet x265 die Kerne selbst
  aus, bei GPU ist Parallelität ohnehin nicht sinnvoll.
* Der PSNR-Bildvergleich ist eine Heuristik. Einzelne Stichproben können durch
  Zeitversatz beim Suchen einbrechen, bei variabler Bildrate wurden an einem
  einwandfreien Video 13, 15 und 24 dB gemessen. Deshalb zählt nur die beste
  Stichprobe und die Schwelle liegt bei 15 dB, während zerstörte Ausgaben bei
  3 bis 7 dB liegen. Gemeldete Dateien trotzdem selbst ansehen.
* Kurze Probe-Slices überschätzen die neue Bitrate leicht, weil der erste
  Keyframe anteilig stark ins Gewicht fällt. `--probe-duration 30` hilft mehr
  als eine gelockerte Schwelle.
* Die Kollisionswarnung im Orchestrator nutzt `sort | uniq` und erkennt
  Dateinamen mit Zeilenumbrüchen nicht. Die Sperre im Worker greift trotzdem.
* `--verify-deep` dekodiert jede Bildausgabe komplett und kostet spürbar Zeit.

## Entstehung

Die erste Fassung der Skripte entstand mit **Google Gemini**, eine spätere
Überarbeitung mit **Claude (Anthropic)**. Die Entwicklungsgeschichte mit
behobenen Fehlern, Messreihen und verworfenen Ansätzen steht in
[CHANGELOG.md](CHANGELOG.md).
