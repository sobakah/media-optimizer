# Changelog

Entwicklungsgeschichte der Media Optimizer Suite. Die README beschreibt den
aktuellen Stand; hier steht, wie er zustande kam und warum einzelne
Entscheidungen so ausgefallen sind.

Die erste Fassung der vier Skripte entstand mit **Google Gemini**, eine
spätere Überarbeitung mit **Claude (Anthropic)**.

---

## Ausgangsfassung

`media-optimizer.sh` als Orchestrator, dazu `img-to-jxl.sh`,
`gif-to-webp.sh` und `h264-to-h265.sh`. Enthalten waren bereits
Resume über Statusdateien, Papierkorb statt `rm`, Endungskorrektur per
MIME-Typ, Spiegelung der Ordnerstruktur, parallele Worker über `xargs` und
die GPU/CPU-Umschaltung anhand der Quell-Bitrate.

---

## Überarbeitung

### Behobene Fehler der Ausgangsfassung

| Fund | Auswirkung |
|---|---|
| `mkdir -p ""` | Der In-Place-Modus brach den Orchestrator sofort mit Exit 1 ab. |
| `gif2webp -lossless` | Die Option existiert nicht. Der Aufruf schlug bei jeder Datei fehl, die GIF-Stufe hat nie etwas konvertiert. |
| `cwebp -q 9` | Qualität 9 von 100 im PNG-Fallback, vermutlich mit `-z 9` verwechselt. |
| `BATCH_MAP` | Videos wurden vor der Verarbeitung eingetragen und nach einem Abbruch beim Resume dauerhaft übersprungen. |
| Namenskollision | `foo.jpg` und `foo.png` teilten sich Ziel- und Temp-Datei, bei parallelen Workern mit Datenverlust. |
| `find *.mp4` | Fand auch `.part.mp4` aus abgebrochenen Läufen. |
| `eval` in `prompt_val` | Pfadeingaben mit `$(...)` wurden ausgeführt. |
| `find \| wc -l` | Ohne `\|\| true` riss ein Permission-Denied unter `pipefail` den Orchestrator mit. |
| PNG mit festem `-q 75` | Verlustbehaftet bei gleichzeitig gelöschtem Original, ohne Hinweis in der Ausgabe. |
| Kein Abgleich vor dem Löschen | Eine bei voller Platte abgeschnittene Datei genügte der Prüfung „größer als 0 Byte". |

### Neue Grundlagen

* `lib/common.sh` gegen die dreifach duplizierten Hilfsfunktionen.
* Konfigurationsdatei mit der Rangfolge CLI > interaktive Eingabe >
  Umgebung > Konfig > Default.
* `--help` und Optionen in allen Skripten, Dry-Run, Abhängigkeitsprüfung.
* Verifikation der Ausgaben vor jeder Löschentscheidung.
* Rückfrage statt hartem `rm`, wenn der Papierkorb fehlschlägt.
* Exit-Handler, der das Fenster bei Start per Doppelklick offen hält und
  Exit-Code, Zeilennummer und fehlgeschlagenen Befehl nennt.

### Eigene Fehler während der Überarbeitung

Diese Fehler wurden durch die Überarbeitung eingeführt und danach behoben.
Sie stehen hier, weil die Ursachen bei künftigen Änderungen erneut drohen.

| Fehler | Ursache | Konsequenz im Code |
|---|---|---|
| `-tag:v hvc1` gesetzt | Der Tag verlangt Parametersätze ausschließlich im `hvcC`-Kasten. Liefert der Encoder sie im Datenstrom, ist die Datei korrekt kodiert, aber nicht mehr dekodierbar: Fragmente oben, Rest grün. | `HEVC_TAG` ist leer, `hvc1` nur über `--hevc-tag` |
| `format=nv12\|p010` | Als Alternative geschrieben handelt der Filter das Format selbst aus. Fällt die Wahl auf p010 ohne `main10`, wird der Puffer nur teilweise gefüllt. | Format wird fest gesetzt, 10 Bit nur mit `--gpu-10bit` |
| `export MO_HOLD=0` im Orchestrator | `export` setzt die Variable auch in der eigenen Shell, der Orchestrator schaltete sich damit selbst das Warten ab. | `env MO_HOLD=0` nur für die Kindprozesse |
| Encoder-Argumente vor der Geräteerkennung gebaut | Die Probe lief mit `-vaapi_device auto` und schlug fehl, während das Encoding funktionierte. | `gpu_encoder_args()` wird nach der Erkennung und je Datei vor der Probe neu gebaut |
| Konfigdatei in Workern geblockt | Werte, die der Orchestrator nicht kennt, kamen nie an. | `load_config` sichert vorhandene Umgebungswerte und lädt in jedem Skript |
| `verify_visual` nicht exportiert | In parallelen Workern „command not found", was als fehlgeschlagene Prüfung gewertet wurde. | zentrales `export -f` in `common.sh` |
| PSNR-Prüfung mit Löschrecht | Eine Heuristik entschied über Dateien. Einzelne Stichproben brechen bei variabler Bildrate ein (13, 15, 24 dB an einem einwandfreien Video). | Bewertet wird die beste Stichprobe, Schwelle 15 dB, Standard ist nur warnen |
| `COUNT_CACHE_SKIPPED` fortgeschrieben | Jede Fortsetzung zählte dieselben Cache-Treffer erneut, Werte im fünfstelligen Bereich. | Zähler beschreibt den aktuellen Scan und startet bei 0 |
| Endungskorrektur änderte die Quelle | Die Korrektur benannte die Quelldatei auch dann um, wenn ein Zielverzeichnis angegeben war. Damit war die Zusicherung „Quelle bleibt unangetastet" verletzt. | `_handle_wrong_extension` unterscheidet nach Modus; `--preflight` meldet im Zielmodus nur |
| `run_stage` deutete Exit ≥ 124 als Abbruch | Ein nicht ausführbares Unterskript (126) wurde als Benutzerabbruch gemeldet. | nur 130, 124 und 125 gelten als Abbruch |

### Messungen

Grundlage der Voreinstellungen, nachvollziehbar in der README.

* **PNG nach JXL.** `-q` bildet auf eine Butteraugli-Distanz ab: 75 → d2.35,
  90 → d1.00, 93 → d0.73. Verlustfrei kostet die vierfache Größe gegenüber
  `-q 75` und rund 60 % mehr Rechenzeit. Gewählt: `-q 90` in der Konfig,
  verlustfrei als Skript-Default.
* **GIF.** WebP verlustfrei liegt bei 66 bis 84 % der GIF-Größe, AVIF bei
  CRF 20 bei 46 bis 48 %. Default bleibt WebP, weil bit-exakt.
* **Kurze Videos nach AVIF.** Unter CRF 30 wird die Datei größer als das
  H.264-Original; bei CRF 32 bleiben rund 10 % Ersparnis. Eine längere GOP
  ändert nichts.
* **x265-Parameter.** `pools=16` und `frame-threads=4` entsprechen auf
  16 Threads dem, was x265 selbst wählt, und wurden entfernt. `asm=avx512`
  bleibt als Konfigoption, weil Zen 5 einen vollen 512-Bit-Datenpfad hat.
* **cjxl.** Ohne `--num_threads=1` startet jeder der 16 parallelen Prozesse
  noch einmal 16 eigene Threads.

### Modusabhaengige Vorbelegung

`DELETE_ORIGINAL` und `RENAME_INPLACE` haben keinen festen Default mehr. Mit
Zielverzeichnis stehen beide auf `false`, im In-Place-Modus auf `true` samt
Hinweis, der sich beantworten laesst. Vorher war `false` fest verdrahtet, was
im In-Place-Modus dazu fuehrte, dass Original und Ausgabe nebeneinander
liegen blieben und der `_h265`-Suffix dauerhaft bestehen blieb.

### Protokoll

Fortlaufende Logdatei neben dem Orchestrator mit Einstellungen, bearbeiteten
Dateien und Auswertung je Lauf. Uebersprungene Dateien werden bewusst nicht
einzeln festgehalten, damit die Datei bei grossen Bestaenden nicht von
Cache-Treffern dominiert wird.

### Weiterer Durchlauf

Nach einem erfolgreichen Lauf laesst sich der Orchestrator mit denselben
Optionen fuer ein anderes Verzeichnis neu starten. Der Neustart geht ueber
`exec`; weitergereichter Zustand wie Run-ID und aufgeloeste Einstellungen
wird vorher entfernt. Die Run-ID enthaelt seither einen Zufallsanteil, weil
`exec` die PID beibehaelt und zwei Laeufe derselben Sekunde sonst dieselbe
Kennung bekaemen.

### Werkzeuge, die aus Fehlersuche entstanden

* **`verify-output.sh`** prüft ein Zielverzeichnis gegen die Originale in drei
  Stufen und kann defekte Ausgaben gezielt neu erzeugen lassen.
* **`--gpu-selftest`** probiert Optionsvarianten an einer echten Datei durch
  und bewertet jedes Ergebnis per Bildvergleich. Notwendig, weil ein
  VAAPI-Encoder ohne Fehlermeldung unbrauchbare Bilder liefern kann.
* **`--from-list`** verarbeitet nur aufgeführte Quellpfade, ignoriert dabei
  Cache und vorhandene Ausgaben.
* **`video-to-avif.sh`** für kurze, tonlose Videos.

### Verworfene Diagnosen

Zwei Erklärungen für das grüne Bild passten zum Symptom und waren trotzdem
falsch. Beide Male lag es nicht am Encoder, sondern an einer Option, die die
Überarbeitung hinzugefügt hatte.

* **„p010-Verwechslung im Filter."** Plausibel, weil ein genullter YUV-Puffer
  genau dieses Grün ergibt. Die Korrektur auf festes `nv12` änderte nichts.
* **„hevc_vaapi ist auf RDNA 4 unter Mesa grundsätzlich defekt."** Übernommen,
  obwohl die Beobachtung dagegensprach, dass das Encoding mit der
  Ausgangsfassung funktioniert hatte. Genau dieser Widerspruch führte zur
  Bisektion und damit zur tatsächlichen Ursache `-tag:v hvc1`.

Lehre für künftige Fehlersuche: Wenn eine Erklärung einer gesicherten
Beobachtung widerspricht, ist die Erklärung falsch, nicht die Beobachtung.
Der Vergleich mit einer nachweislich funktionierenden Fassung schlägt jede
Theorie.
