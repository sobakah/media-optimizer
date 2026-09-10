# Media Optimizer Suite

Eine Bash-Skript-Sammlung zur automatisierten Konvertierung und Größenreduzierung von Mediendateien (Bilder, GIFs und Videos).

## Kernfunktionen

* **Zentrale Steuerung:** Das Skript `media-optimizer.sh` fungiert als Orchestrator, fragt Parameter ab und ruft die jeweiligen Unter-Skripte sequenziell auf.
* **Resume-Funktion:** Vorgänge können mit `Strg+C` abgebrochen werden. Der aktuelle Fortschritt, Datei-Statistiken und gewählte Parameter werden in `.env`-Dateien gespeichert, sodass der Lauf später nahtlos fortgesetzt werden kann.
* **Sicheres Löschen:** Originaldateien werden nach erfolgreicher Konvertierung über `gio trash` oder `trash-cli` in den Desktop-Papierkorb verschoben, statt sie direkt per `rm` zu löschen. Schlägt der Papierkorb fehl, wird nichts gelöscht, sondern am Ende des Laufs einmal nachgefragt.
* **Verifikation vor dem Löschen:** Eine Zieldatei wird erst akzeptiert, wenn sie tatsächlich lesbar ist, bei Videos zusätzlich mit Abgleich der Laufzeit gegen das Original.
* **Dateiendungs-Korrektur (Magic Bytes):** Das Skript prüft auf Wunsch vor der Verarbeitung den MIME-Type der Dateien (z. B. ein als `.jpg` benanntes WebP-Bild) und korrigiert die Dateiendung bei Abweichungen automatisch.
* **Dateisystem-Spiegelung:** Zieldateien können In-Place (im Quellverzeichnis) oder unter Beibehaltung der Unterordner-Struktur in einem separaten Zielverzeichnis abgelegt werden.
* **Dry-Run:** Jeder Aufruf lässt sich mit `-n` vorab durchspielen, ohne dass geschrieben oder gelöscht wird.
* **Konfigurationsdatei:** Wiederkehrende Einstellungen stehen in `media-optimizer.conf` statt im Skriptkopf.

## Systemanforderungen

Folgende Pakete müssen auf dem System installiert sein:

**Fedora / RHEL / Bazzite:**
```bash
sudo dnf install ffmpeg libjxl jxl-tools libwebp-tools file trash-cli libva-utils
```

**Debian / Ubuntu / Linux Mint:**
```bash
sudo apt install ffmpeg libjxl-tools webp file trash-cli vainfo
```

*(Hinweis: `gio` ist in den meisten GNOME/KDE-Umgebungen standardmäßig enthalten. `trash-cli` dient als Fallback. `libva-utils` bzw. `vainfo` wird nur zur Fehlersuche beim GPU-Encoding gebraucht.)*

Alle Abhängigkeiten werden beim Start einmal geprüft. Fehlt etwas, bricht der Orchestrator sofort ab, statt nach halbem Durchlauf zu scheitern.

## Aufbau

```
media-optimizer.sh          Orchestrator, Resume, Pre-Flight
media-optimizer.conf        Einstellungen (optional)
scripts/img-to-jxl.sh       JPG/PNG -> JXL, WebP als Fallback
scripts/gif-to-webp.sh      GIF -> animiertes WebP
scripts/h264-to-h265.sh     H.264-MP4 -> HEVC
scripts/verify-output.sh    Zielverzeichnis gegen die Originale prüfen
scripts/lib/common.sh       gemeinsame Helfer, wird gesourct
```

## Skript-Übersicht

### 1. `media-optimizer.sh` (Orchestrator)

Analysiert den angegebenen Quellordner, zählt die zu verarbeitenden Dateitypen und startet die entsprechenden Unter-Skripte. Führt optional den Pre-Flight-Check zur Korrektur von Dateiendungen aus und warnt vor gleichnamigen Dateien mit verschiedenen Endungen, da `foo.jpg` und `foo.png` beide auf `foo.jxl` zeigen würden.

```
  -i, --input  <dir>   Quellverzeichnis
  -o, --output <dir>   Zielverzeichnis (leer = In-Place)
      --delete         Originale nach Erfolg in den Papierkorb
      --no-delete      Originale behalten (Default)
      --force-delete   Falls Papierkorb fehlschlägt: ohne Rückfrage rm
      --verify-deep    Bildausgaben vollständig dekodieren (langsamer)
      --preflight      Dateiendungen vorab per MIME-Typ korrigieren
  -y, --yes            Keine Rückfragen, Standardwerte verwenden
  -n, --dry-run        Nur anzeigen, nichts schreiben
      --reset          Gespeicherten Zustand verwerfen und neu starten
```

### 2. `img-to-jxl.sh` (Bilder)

Konvertiert Bilder parallel (`xargs -P`).

* **JPG/JPEG:** Verlustfreies Transcoding zu JPEG XL (`cjxl`), bit-exakt umkehrbar. Keine Qualitätseinstellung wirkt auf diesen Pfad.
* **PNG:** Konvertierung zu JPEG XL, verlustfrei oder mit einstellbarer Qualität. Schlägt das fehl, erfolgt ein Fallback auf verlustfreies WebP (`cwebp -lossless -z 9`).
* Jeder Zielname wird per Lock-Verzeichnis exklusiv beansprucht, damit zwei Worker nicht gleichzeitig dieselbe Ausgabe schreiben.

```
  -j, --workers <n>      Parallele Worker (Default: nproc)
  -e, --effort  <1-9>    JXL Effort (Default: 7)
      --png-mode  <m>    lossless | lossy   (Default: lossless)
      --png-quality <q>  Nur bei --png-mode lossy (Default: 90)
      --cjxl-threads <n> Threads pro cjxl-Prozess (Default: 1, da parallel)
      --verify-deep      Ausgabe vollständig dekodieren
```

`-q` bildet intern auf eine Butteraugli-Distanz ab. Gemessen an einem 1080p-Testbild, Prozentwerte relativ zur PNG-Größe:

| `-q` | Distanz | fotoähnlich | Screenshot/Text |
|---|---|---|---|
| 75 | d2.35 | 7 % | 19 % |
| 90 | d1.00, visuell verlustfrei | 12 % | 33 % |
| 93 | d0.73 | 14 % | 39 % |
| verlustfrei | d0 | 52 % | 55 % |

### 3. `gif-to-webp.sh` (Animierte GIFs)

Wandelt animierte GIFs verlustfrei in animierte WebP-Dateien um (`gif2webp`). Die Verarbeitung erfolgt ebenfalls parallel über `xargs`. Ergebnisse, die größer als das Original wären, werden verworfen.

```
  -j, --workers <n>   Parallele Worker (Default: nproc)
  -m, --method  <0-6> Kompressionsstufe (Default: 6)
      --keep-larger   Ergebnis auch behalten, wenn es größer ist
      --verify-deep   Ausgabe mit webpinfo prüfen
```

### 4. `h264-to-h265.sh` (Videos)

Re-Encoder für AVC/H.264-Videos zu HEVC/H.265.

* **Encoder-Auswahl:** Wechselt abhängig von einer definierten Bitraten-Schwelle automatisch zwischen Hardware-Encoding (VA-API) und Software-Encoding (libx265). Beim Start wird einmal geprüft, ob HEVC über VA-API überhaupt funktioniert.
* **Probe-Slice:** Konvertiert optional vorab ein Segment aus der Dateimitte und vergleicht es mit demselben Ausschnitt des Originals, beides ohne Audio, gleiche Länge, über die Dateigröße gemessen. Übersprungen wird, sobald weniger als 10 % Ersparnis zu erwarten sind.
* **Fehlertoleranz:** Erkennt Abbruchfehler durch beschädigte Quelldateien. Intakte Frames werden in eine lesbare Zieldatei gerettet. Bei geretteten Dateien bleibt das Original erhalten, auch mit `--delete`.
* **Größenprüfung:** Resultierende Dateien, die größer als das Original sind, werden automatisch verworfen.
* **Kompatibilität:** `-tag:v hvc1` und `-movflags +faststart` für Abspielbarkeit in Apple-Playern und beim Streaming.
* **Bildkontrolle:** Nach dem Encoding werden an mehreren Stellen Einzelbilder aus Quelle und Ziel per PSNR verglichen, denn die Laufzeitprüfung allein erkennt zerstörte Farbformate nicht (ein grünes Bild hat die korrekte Dauer). Bewertet wird die **beste** Stichprobe: einzelne schlechte Werte entstehen durch Zeitversatz beim Suchen und sind kein Defekt. Standardmäßig wird nur gewarnt und die Datei behalten, die Liste landet in `.video_verdaechtig.txt`. `--strict-visual` verwirft stattdessen, `--no-verify-visual` schaltet die Prüfung ab.
* **Pixelformat:** Auf der GPU wird fest mit `format=nv12` gearbeitet, also 8 Bit. 10-Bit-Quellen werden reduziert. `--gpu-10bit` schaltet auf `p010` plus `-profile:v main10` um; beides gehört zwingend zusammen, sonst entsteht ein grünes Bild.

```
      --encoder <mode>     auto | gpu | cpu   (Default: auto)
      --threshold <kbps>   Schwelle CPU/GPU im auto-Modus (Default: 3500)
      --qp <n>             GPU CQP (Default: 26)
      --crf <n>            CPU CRF (Default: 22)
      --preset <p>         CPU x265-Preset (Default: medium)
      --min-size <mb>      Dateien darunter überspringen (Default: 5)
      --no-probe           Kein Testslice vorab
      --probe-margin <p>   Überspringen ab p % des Originals (Default: 90)
                           100 = nur wenn die Ausgabe wirklich größer wird
      --probe-duration <s> Länge des Testslice (Default: 10)
      --keep-larger        Größere Ergebnisse trotzdem behalten
      --gpu-device <p>     VAAPI-Render-Node, oder "auto"
      --force-gpu          Startprüfung überspringen
      --x265-params <s>    x265-Parameter (Default: aq-mode=3:no-sao=1)
      --no-faststart       moov-Atom nicht nach vorn schreiben
      --rename-inplace     _h265-Suffix nach dem Löschen des Originals entfernen
      --from-list <datei>  Nur die dort aufgeführten Quelldateien verarbeiten
      --no-cache           Cache-Datei ignorieren
```

`--from-list auto` nimmt `.defekte_videos.txt` aus dem Zielverzeichnis, falls
vorhanden, und läuft sonst normal durch. Liegt eine Liste vor, ohne dass sie
angefordert wurde, weist das Skript darauf hin, wechselt aber nicht von
selbst auf die Teilmenge: eine veraltete Liste würde den restlichen Bestand
stillschweigend ausblenden. Wer das trotzdem als Standard will, setzt
`AUTO_FIX_LIST=true` in der Konfig.

Damit die Liste nicht veralten kann, verschiebt `verify-output.sh --run` sie
nach der Reparatur nach `.defekte_videos.erledigt-<zeitstempel>.txt`.

`--from-list` erwartet eine Datei mit einem Quellpfad pro Zeile (`#` ist
Kommentar). Cache und vorhandene Ausgaben werden dabei ignoriert, weil die
Auswahl ausdrücklich getroffen wurde: die genannten Dateien werden neu
erzeugt und überschrieben. Fehlende Pfade werden gemeldet und übersprungen.

Im In-Place-Modus heißt die Ausgabe `name_h265.mp4`, weil Quelle und Ziel
sonst denselben Pfad hätten. Der Suffix bleibt standardmäßig stehen, auch
wenn das Original gelöscht wurde. `--rename-inplace` bzw.
`RENAME_INPLACE=true` benennt die Datei danach zurück auf `name.mp4`.

Umbenannt wird nur, wenn der Pfad des Originals frei ist. Ohne `--delete`,
bei geretteten Dateien und wenn der Papierkorb fehlschlug, bleibt das
Original liegen und der Suffix wird beibehalten, statt die Quelldatei zu
überschreiben. Nachteil der Option: nach dem Umbenennen ist am Dateinamen
nicht mehr erkennbar, welche Videos konvertiert wurden.

### 5. `verify-output.sh` (Kontrolle und Reparatur)

Prüft ein Zielverzeichnis gegen das Quellverzeichnis. Die Zuordnung ist der
relative Pfad, weil die Ordnerstruktur gespiegelt und der Dateiname
beibehalten wird. Geprüft wird in drei Stufen: ist die Datei lesbar, stimmt
die Laufzeit, passt der Bildinhalt (PSNR-Stichproben).

```
  -i, --input   <dir>   Quellverzeichnis (die Originale)
  -o, --output  <dir>   Zielverzeichnis (die konvertierten Dateien)
  -j, --workers <n>     Parallele Prüfungen (Default: nproc)
      --psnr-min <db>   Schwelle für „zerstört" (Default: 20)
      --samples <n>     Stichproben pro Datei (Default: 3)
      --duration-tol <p> Erlaubte Laufzeitabweichung in Prozent (Default: 2)
      --fix             Defekte Ausgaben löschen und aus dem Cache nehmen
      --run             Nach --fix h264-to-h265.sh neu starten
```

Die drei Meldungen bedeuten:

| Status | Bedeutung |
|---|---|
| `[UNLESBAR]` | `ffprobe` findet keinen Videostream, die Datei ist unbrauchbar |
| `[DAUER]` | Die Laufzeit weicht um mehr als die Toleranz vom Original ab, oder die Ausgabe meldet gar keine Dauer |
| `[BILD?]` | Laufzeit stimmt, aber der Bildvergleich liegt unter der PSNR-Schwelle |

`[DAUER]` deutet meist auf eine abgeschnittene Datei hin, etwa bei voller
Platte. Es trifft aber auch **gerettete Dateien**: wenn der Encoder eine
beschädigte Quelle nur teilweise lesen konnte, ist die Ausgabe legitim
kürzer. Solche Dateien sind kein Defekt und würden bei `--fix` unnötig
gelöscht und neu erzeugt, mit demselben Ergebnis. Die Meldung nennt deshalb
beide Laufzeiten, und `--duration-tol` hebt die Grenze an.

Ein niedriger PSNR ist ein **Verdacht, kein Beweis**. Mit `--keep-samples <dir>`
werden die verglichenen Einzelbilder abgelegt, sodass sich die Meldung selbst
beurteilen lässt. Ohne `--fix` wird nur berichtet und nichts verändert. Mit `--fix` werden die
defekten Ausgaben gelöscht und die zugehörigen Quelldateien aus
`.video_conversion_cache.txt` entfernt (mit `.bak`-Sicherung), sodass ein
normaler Lauf genau diese Dateien neu erzeugt und die intakten überspringt.
`--run` startet ihn direkt, und zwar mit `--from-list` auf der Defektliste.
Es wird also nur wiederholt, was gemeldet wurde, statt den ganzen Baum
erneut abzugehen.

Bei vorhandenem Cache ist der Zeitgewinn gering, weil gecachte Dateien
ohnehin kein `ffprobe` auslösen. Fehlt der Cache, ist er deutlich: in einem
Test mit 30 Dateien und 2 Defekten 14 statt 70 `ffprobe`-Aufrufe und
2,8 statt 5,8 Sekunden. Der eigentliche Vorteil ist aber, dass ausschließlich
die gemeldeten Dateien angefasst werden und die Reparatur auch dann greift,
wenn die defekte Ausgabe noch an ihrem Platz liegt.

Ausgaben ohne passendes Original werden nur gezählt und nicht angetastet.
Ein identisches Quell- und Zielverzeichnis wird abgelehnt, das Skript ist
nicht für den In-Place-Modus gedacht.

```bash
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv             # nur prüfen
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv --fix --run # reparieren
```

## Nutzung

### Gesamter Durchlauf (Empfohlen)

Startet die Parameter-Abfrage für alle Dateitypen:

```bash
./media-optimizer.sh /pfad/zum/input /pfad/zum/output
```

*(Wird `/pfad/zum/output` weggelassen, speichert das Skript die Dateien im Quellordner.)*

Erst einmal ansehen, was passieren würde:

```bash
./media-optimizer.sh -n /pfad/zum/input
```

### Standalone-Nutzung

Die Unter-Skripte können einzeln aufgerufen werden. Wenn sie nicht über den Orchestrator gestartet werden, rufen sie ein eigenes interaktives Setup-Menü auf:

```bash
./scripts/h264-to-h265.sh /pfad/zu/videos
./scripts/h264-to-h265.sh -i /pfad/zu/videos --encoder cpu --crf 20
```

Positionsargumente und Optionen lassen sich mischen. `--help` gibt es bei jedem Skript.

## Konfiguration

`media-optimizer.conf` wird automatisch geladen, wenn sie neben `media-optimizer.sh` liegt. Alternative Orte sind `$XDG_CONFIG_HOME/media-optimizer.conf` oder ein Pfad in `MO_CONFIG`. Löschen oder Umbenennen stellt die eingebauten Defaults wieder her.

Rangfolge: **CLI-Flag > interaktive Eingabe > Umgebungsvariable > Konfigdatei > Default.**

Die interaktive Abfrage, erreichbar über ein `n` auf „Standardeinstellungen für ALLE Medienarten nutzen?", zeigt die Konfigwerte als Vorbelegung und überschreibt sie für den laufenden Durchgang. Dort einstellbar sind:

`MAX_WORKERS`, `DELETE_ORIGINAL`, `VERIFY_DEEP`, `JXL_EFFORT`, `PNG_MODE`, `PNG_QUALITY`, `COMPRESSION_METHOD`, `ENCODER_MODE`, `BITRATE_THRESHOLD_KBPS`, `GPU_QP`, `CPU_CRF`, `CPU_PRESET`, `ENABLE_PROBE`, `DISCARD_IF_LARGER`, `KEEP_SALVAGED_CORRUPT` und der Endungs-Preflight.

Nur über Konfig, CLI oder Umgebung erreichbar:

`CPU_X265_PARAMS`, `VAAPI_DEVICE`, `MIN_SIZE_MB`, `PROBE_MARGIN_PCT`, `PROBE_DURATION`, `CJXL_THREADS`, `FASTSTART`, `GIF_KMIN`, `USE_CACHE`, `FORCE_DELETE`, `DURATION_TOLERANCE_PCT`.

### Hardware-Tuning

Die mitgelieferte Konfiguration ist auf einen Ryzen 7 9700X (8C/16T, Zen 5) mit RX 9070 XT (RDNA4) zugeschnitten.

* `pools` und `frame-threads` sind bewusst nicht gesetzt. x265 wählt auf 16 Threads von selbst einen Pool über alle 16 Threads und 4 Frame-Threads. Explizite Werte ändern dort nichts und engen bei einem Hardwarewechsel nur ein.
* `asm=avx512` ist bei x265 per Default aus, weil es auf CPUs mit halbiertem AVX-512-Datenpfad bremst. Zen 5 hat einen vollen 512-Bit-Datenpfad, dort kann es helfen. In der Konfig aktiviert, die Messbefehle zum Gegenprüfen stehen als Kommentar daneben.
* `CJXL_THREADS=1`, weil sonst jeder der 16 parallelen cjxl-Prozesse noch einmal 16 eigene Threads startet.

## Hinweise zur Ausführung

* **Atomares Schreiben:** Alle Skripte schreiben zunächst in temporäre `.part`-Dateien mit eindeutigem Namen. Die Originaldatei wird erst ersetzt oder in den Papierkorb verschoben, wenn die Zieldatei die Verifikation bestanden hat. Bei Videos heißt das: `ffprobe` meldet eine Laufzeit, die auf 2 % zum Original passt. Bei Bildern: die Datei ist nicht leer, mit `--verify-deep` zusätzlich vollständig dekodierbar.
* **Papierkorb:** Wenn die Option zum Löschen der Originaldateien gewählt wurde, landen diese im System-Papierkorb. Der Speicherplatz wird physisch erst freigegeben, wenn der Papierkorb durch den Nutzer geleert wird.
* **Papierkorb nicht verfügbar:** Bei Dateien auf anderen Mounts oder ohne `gio` und `trash-cli` schlägt das Verschieben fehl. Dann wird nichts gelöscht. Die betroffenen Pfade sammeln sich in `.<typ>_pending_deletes.txt`, und am Ende des Laufs kommt eine einmalige Rückfrage. Ohne Terminal bleiben die Originale erhalten.
* **Reste aufräumen:** `.part`-Dateien und Lock-Verzeichnisse aus hart abgebrochenen Läufen werden beim nächsten Start entfernt.
* **Statusdateien:** `.img_stats.env`, `.gif_stats.env`, `.h265_stats.env`, `.video_conversion_cache.txt` und die Pending-Listen liegen versteckt im Quellverzeichnis.
* **Zähler bei Fortsetzungen:** Die Statusdatei `.h265_stats.env` schreibt geleistete Arbeit über Abbrüche hinweg fort: neu kodiert, gerettet, verworfen, fehlgeschlagen, Byte-Summen und Laufzeit. Jede Datei geht dort genau einmal ein, weil sie danach im Cache steht. Die Cache-Treffer beschreiben dagegen den aktuellen Scan und werden bei jedem Start neu gezählt, sonst würde jede Fortsetzung dieselben Dateien erneut aufaddieren. In der Auswertung heißt die Zeile deshalb „Cache (dieser Scan)".
* **Video-Cache:** `.video_conversion_cache.txt` merkt sich abgeschlossene Dateien, damit ein späterer Lauf nicht erneut `ffprobe` über jede Datei laufen lässt. Eingetragen werden Quelldateien sowie, nur im In-Place-Modus, die erzeugten `_h265.mp4`, weil `find` diese im nächsten Lauf wieder einsammelt. Bei separatem Zielordner werden keine Ausgabepfade vermerkt. Verwaiste Einträge, deren Datei nicht mehr existiert, werden beim Start entfernt. Der Cache ist eine reine Beschleunigung: `--no-cache` oder Löschen der Datei ändert nur die Laufzeit, nicht das Ergebnis. Da absolute Pfade gespeichert werden, greift er nach einem Verschieben des Ordners nicht mehr.

## GPU-Encoding funktioniert nicht

`VAAPI_DEVICE="auto"` probiert alle Render-Nodes unter `/dev/dri/` durch. Schlägt trotzdem alles fehl, nennt das Skript den konkreten ffmpeg-Fehler und passende Diagnosebefehle. Häufige Ursachen:

1. **Falsches Render-Node.** Bei einer CPU mit iGPU plus dGPU ist `renderD128` oft die integrierte Grafik, die dGPU liegt auf `renderD129`. `ls -l /dev/dri/by-path/` ordnet die Nodes den PCI-Adressen zu.
2. **Fehlende Rechte.** Der Benutzer muss in der Gruppe `render` sein: `id | grep render`, sonst `sudo usermod -aG render $USER` und neu anmelden.
3. **Mesa zu alt.** RDNA4 mit VCN 5 braucht Mesa 25.0 oder neuer und Kernel 6.13 oder neuer.
4. **ffmpeg ohne VAAPI gebaut.** `ffmpeg -hide_banner -encoders | grep hevc_vaapi` muss eine Zeile liefern.

Prüfen lässt sich das mit `vainfo --display drm --device /dev/dri/renderD128 | grep -i 'hevc.*enc'`. Gesucht ist `VAProfileHEVCMain : VAEntrypointEncSlice`.

Scheitert die GPU erst an einer echten Datei, wird diese sofort auf der CPU wiederholt und der Rest des Laufs bleibt bei der CPU. Ein Durchlauf kann daran also nicht mehr abbrechen.

## Start per Doppelklick

Wird ein Skript aus einem Dateimanager oder über eine `.desktop`-Datei
gestartet, schließt der Terminal-Emulator das Fenster, sobald das Skript
endet. Weder die Auswertung noch eine Fehlermeldung wären dann lesbar.
Deshalb hält ein Exit-Handler das Fenster in diesem Fall offen, **auch bei
erfolgreichem Durchlauf**, damit die Statistiken geprüft werden können:

```
║  GESAMTER VORGANG ERFOLGREICH BEENDET                         ║
╚══════════════════════════════════════════════════════════════╝

[FERTIG] Ohne Fehler beendet.
Fenster bleibt offen. Enter zum Schliessen.
```

Bei einem Abbruch nennt der Handler zusätzlich Exit-Code, Zeilennummer und
den fehlgeschlagenen Befehl:

```
[ABBRUCH] Skript endete mit Code 1.
          Zeile 412: `rm /pfad/datei` (Code 1)

Fenster bleibt offen. Enter zum Schliessen.
```

Die Erkennung wertet die Kommandozeile des Elternprozesses aus. Eine
interaktive Shell hat kein Skript- und kein `-c`-Argument, ein Terminal-
Emulator oder `sh -c ...` dagegen schon. Aus dem Terminal gestartet wird
also nicht gewartet, per Doppelklick schon.

Steuerbar über `--hold`, `--no-hold` oder die Umgebung:

| Variable | Wirkung |
|---|---|
| `MO_HOLD=auto` | Default: offen halten, wenn kein interaktives Terminal die Eltern ist |
| `MO_HOLD=1` | immer offen halten |
| `MO_HOLD=0` | nie offen halten |
| `MO_HOLD_TIMEOUT=15` | nach 15 Sekunden von selbst schließen (0 = warten) |

Für eine `.desktop`-Datei ist `MO_HOLD=1` die verlässlichste Wahl, weil die
Automatik nicht jede Startmethode kennen kann:

```ini
[Desktop Entry]
Type=Application
Name=Media Optimizer
Exec=env MO_HOLD=1 /pfad/zu/media-optimizer.sh
Terminal=true
```

Ohne Terminal, etwa bei „Als Programm ausführen" in GNOME Files, wird nicht
gewartet, weil dort niemand die Meldung sehen könnte.

Beim Start über `media-optimizer.sh` wartet nur der Orchestrator am Ende,
nicht jede der drei Stufen einzeln. Standalone aufgerufene Unter-Skripte
halten das Fenster dagegen selbst offen.

## Grünes Bild mit Artefakten (behoben)

Symptom: Das Video ist überwiegend einfarbig dunkelgrün, nur im oberen
Bereich bewegen sich Pixel, der Ton ist einwandfrei.

Ursache war **`-tag:v hvc1`**, nachgewiesen per Bisektion mit
`--gpu-selftest` auf einer RX 9070 XT: der Originalbefehl und alle anderen
Varianten lieferten 42 dB, allein die Variante mit `hvc1` fiel auf 8 dB.

Der Tag `hvc1` verlangt, dass die Parametersätze (VPS/SPS/PPS)
ausschließlich im `hvcC`-Kasten der Sample-Beschreibung stehen und nicht im
Datenstrom. Liefert der Hardware-Encoder sie in-band, baut der Muxer ein
unvollständiges `hvcC`. Die Datei ist dann korrekt kodiert, aber nicht mehr
korrekt dekodierbar: der Decoder beginnt mit falschen Parametern, füllt nur
einen Teil des Bildpuffers und lässt den Rest auf Null. Ein YUV-Puffer aus
Nullen (Y=0, U=0, V=0) erscheint als Dunkelgrün. Der Ton ist nicht betroffen,
weil er per `-c:a copy` durchkopiert wird.

Es war also kein Encoder- und kein Treiberfehler, sondern falsche
Container-Signalisierung. Deshalb ist `HEVC_TAG` jetzt leer (ffmpeg-Vorgabe
`hev1`) und `hvc1` nur noch über `--hevc-tag hvc1` erreichbar.

Bereits erzeugte Dateien lassen sich finden und neu erzeugen:

```bash
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv --keep-samples /tmp/proben
./scripts/verify-output.sh -i ~/Videos -o /mnt/archiv --fix --run
```

Dieses Fehlerbild liegt im Test bei 4 bis 7 dB PSNR und wird damit
zuverlässig erkannt, weit unterhalb der Schwelle von 15 dB.

## Bekannte Grenzen

* Die Kollisionswarnung im Orchestrator nutzt `sort | uniq`. Dateinamen mit Zeilenumbrüchen werden dort nicht erkannt, die Sperre im Worker greift trotzdem.
* Videos laufen sequenziell. Bei CPU-Encoding lastet x265 die Kerne selbst aus, bei GPU ist Parallelität ohnehin nicht sinnvoll.
* `--verify-deep` dekodiert jede Bildausgabe komplett und kostet spürbar Zeit. Ohne das Flag wird nur auf nicht-leere Ausgabe geprüft.
* Der PSNR-Bildvergleich ist eine Heuristik. Einzelne Stichproben können durch Zeitversatz beim Suchen deutlich einbrechen; bei variabler Bildrate wurden an einem einwandfreien Video 13, 15 und 24 dB gemessen. Deshalb zählt nur die beste Stichprobe und die Schwelle liegt bei 15 dB, während echte Zerstörung bei 3 bis 7 dB liegt. Trotzdem: gemeldete Dateien selbst ansehen, bevor etwas gelöscht wird.
* Kurze Probe-Slices überschätzen die neue Bitrate leicht, weil der erste Keyframe anteilig stark ins Gewicht fällt. Bei Grenzfällen hilft `--probe-duration 30` mehr als eine gelockerte Schwelle.

## Entstehung

Die ursprüngliche Fassung der vier Skripte wurde mit **Google Gemini** erstellt.

Eine spätere Überarbeitung mit **Claude (Anthropic)** hat den Code auditiert, in einer Testumgebung durchgespielt und erweitert. Dabei behobene Fehler:

| Fund | Auswirkung |
|---|---|
| `mkdir -p ""` | Der In-Place-Modus brach den Orchestrator sofort mit Exit 1 ab. |
| `gif2webp -lossless` | Diese Option existiert nicht, gif2webp arbeitet ohnehin verlustfrei. Der Aufruf schlug bei jeder Datei fehl, die GIF-Stufe hat nie etwas konvertiert. |
| `cwebp -q 9` | Qualität 9 von 100 im PNG-Fallback, vermutlich mit `-z 9` verwechselt. |
| `BATCH_MAP` | Videos wurden vor der Verarbeitung eingetragen und nach einem Abbruch beim Resume dauerhaft übersprungen. |
| Namenskollision | `foo.jpg` und `foo.png` teilten sich Ziel- und Temp-Datei, bei parallelen Workern mit Datenverlust. |
| `find *.mp4` | Fand auch `.part.mp4` aus abgebrochenen Läufen. |
| `eval` in `prompt_val` | Pfadeingaben mit `$(...)` wurden ausgeführt. |
| `find \| wc -l` | Ohne `\|\| true` riss ein Permission-Denied unter `pipefail` den Orchestrator mit. |
| PNG mit festem `-q 75` | Verlustbehaftet bei gleichzeitig gelöschtem Original, ohne dass die Ausgabe darauf hinwies. |
| Kein Abgleich vor dem Löschen | Eine bei voller Platte abgeschnittene Datei genügte der Prüfung „größer als 0 Byte". |

Ergänzt wurden Dry-Run, Konfigurationsdatei, `--help`, Abhängigkeitsprüfung, Verifikation der Ausgaben, die Rückfrage bei fehlgeschlagenem Papierkorb, `lib/common.sh` gegen dreifach duplizierte Hilfsfunktionen sowie die GPU-Erkennung mit Fallback pro Datei.

Zwei Fehler stammten aus dieser Überarbeitung selbst und sind ebenfalls behoben: eine GPU-Startprüfung, die `hevc_vaapi` ohne `-rc_mode` aufrief und deshalb funktionierende Hardware verwarf, sowie eine Konfig-Ladelogik, die Werte nicht bis zu den Worker-Skripten durchreichte.
