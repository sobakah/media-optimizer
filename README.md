# Media Optimizer Suite

Eine Bash-Skript-Sammlung zur automatisierten Konvertierung und Größenreduzierung von Mediendateien (Bilder, GIFs und Videos). 

## Kernfunktionen

* **Zentrale Steuerung:** Das Skript `media-optimizer.sh` fungiert als Orchestrator, fragt Parameter ab und ruft die jeweiligen Unter-Skripte sequenziell auf.
* **Resume-Funktion:** Vorgänge können mit `Strg+C` abgebrochen werden. Der aktuelle Fortschritt, Datei-Statistiken und gewählte Parameter werden in temporären `.env`-Dateien gespeichert, sodass der Lauf später nahtlos fortgesetzt werden kann.
* **Sicheres Löschen:** Originaldateien werden nach erfolgreicher Konvertierung über `gio trash` oder `trash-cli` in den Desktop-Papierkorb verschoben, statt sie direkt per `rm` zu löschen.
* **Dateiendungs-Korrektur (Magic Bytes):** Das Skript prüft vor der Verarbeitung den MIME-Type der Dateien (z.B. ein als `.jpg` benanntes WebP-Bild) und korrigiert die Dateiendung bei Abweichungen automatisch.
* **Dateisystem-Spiegelung:** Zieldateien können In-Place (im Quellverzeichnis) oder unter Beibehaltung der Unterordner-Struktur in einem separaten Zielverzeichnis abgelegt werden.

## Systemanforderungen

Folgende Pakete müssen auf dem System installiert sein:

**Fedora / RHEL / Bazzite:**
```bash
sudo dnf install ffmpeg libjxl jxl-tools libwebp-tools file trash-cli
```

**Debian / Ubuntu / Linux Mint:**
```bash
sudo apt install ffmpeg libjxl-tools webp file trash-cli
```
*(Hinweis: `gio` ist in den meisten GNOME/KDE-Umgebungen standardmäßig enthalten. `trash-cli` dient als Fallback).*

## Skript-Übersicht

Das Repository besteht aus vier Skripten, die zusammen oder als Standalone-Anwendungen ausgeführt werden können.

### 1. `media-optimizer.sh` (Orchestrator)
Analysiert den angegebenen Quellordner, zählt die zu verarbeitenden Dateitypen und startet die entsprechenden Unter-Skripte. Führt optional den Pre-Flight-Check zur Korrektur von Dateiendungen aus.

### 2. `img-to-jxl.sh` (Bilder)
Konvertiert Bilder parallel (`xargs -P`).
* **JPG/JPEG:** Verlustfreies Transcoding zu JPEG XL (`cjxl`).
* **PNG:** Konvertierung zu JPEG XL. Schlägt dies fehl, erfolgt ein Fallback auf WebP (`cwebp`).

### 3. `gif-to-webp.sh` (Animierte GIFs)
Wandelt animierte GIFs verlustfrei in animierte WebP-Dateien um (`gif2webp`). Die Verarbeitung erfolgt ebenfalls parallel über `xargs`.

### 4. `h264-to-h265.sh` (Videos)
Re-Encoder für AVC/H.264-Videos zu HEVC/H.265.
* **Encoder-Auswahl:** Wechselt abhängig von einer definierten Bitraten-Schwelle automatisch zwischen Hardware-Encoding (VA-API) und Software-Encoding (libx265).
* **Probe-Slice:** Konvertiert optional vorab ein 10-Sekunden-Segment. Ist die simulierte H.265-Bitrate höher als die des Originals, wird die Datei übersprungen.
* **Fehlertoleranz:** Erkennt Abbruchfehler durch beschädigte Quelldateien. Intakte Frames werden in eine lesbare Zieldatei gerettet.
* **Größenprüfung:** Resultierende Dateien, die größer als das Original sind, werden automatisch verworfen.

## Nutzung

### Gesamter Durchlauf (Empfohlen)
Startet die Parameter-Abfrage für alle Dateitypen:
```bash
./media-optimizer.sh /pfad/zum/input /pfad/zum/output
```
*(Wird `/pfad/zum/output` weggelassen, speichert das Skript die Dateien im Quellordner).*

### Standalone-Nutzung
Die Unter-Skripte können einzeln aufgerufen werden. Wenn sie nicht über den Orchestrator gestartet werden, rufen sie ein eigenes interaktives Setup-Menü auf:
```bash
./h264-to-h265.sh /pfad/zu/videos
```

## Hinweise zur Ausführung

* **Atomares Schreiben:** Alle Skripte schreiben zunächst in temporäre `.part`-Dateien. Die Originaldatei wird erst ersetzt oder in den Papierkorb verschoben, wenn die Zieldatei erfolgreich geschlossen wurde und größer als 0 Byte ist.
* **Papierkorb:** Wenn die Option zum Löschen der Originaldateien gewählt wurde, landen diese im System-Papierkorb. Der Speicherplatz wird physisch erst freigegeben, wenn der Papierkorb durch den Nutzer geleert wird.