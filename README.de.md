# Snapmaker U1 Agent für OrcaSlicer – Installation und Einrichtung

Das Plugin `u1_moonraker_agent.py` bindet den Snapmaker U1 per LAN an OrcaSlicer an. Damit gehen:

- Senden und Drucken mit Zeitraffer, Flow-Kalibrierung, Bett-Nivellierung und Slot-Zuordnung
- Druckerstatus, Kamera und Filament-Sync
- ein eigener Tab „U1“ mit Kamera, Temperaturen, Steuerung und Filament-Slots

## Voraussetzungen

- **OrcaSlicer 2.5.0** (getestet mit Beta und RC auf macOS, Apple Silicon). Die Alpha geht auch, aber ohne automatisches Zuweisen der Filament-Profile.
- Der U1 ist im selben Netz erreichbar. Test im Browser: `http://<U1-IP>:7125/server/info` muss eine JSON-Antwort zeigen.
- Moonraker läuft ohne API-Key (Standard beim U1).

## 1. Drucker-Agents einschalten

**Einstellungen → Developer → Experimental Features → „Use printer agents instead of print hosts“** aktivieren.

## 2. Plugin installieren

1. Plugin holen, auf einem der zwei Wege:
   - **Aus OrcaCloud (empfohlen):** [Snapmaker U1 Agent](https://cloud.orcaslicer.com/app/plugins/plugin-hub/3e39ee3b-3abe-44e3-9f7d-80722f12c0e2) im Plugin Hub abonnieren. OrcaSlicer installiert es und bietet Updates an.
   - **Als Datei:** **Plugins → Install local plugin** → `u1_moonraker_agent.py` auswählen. Nur einen der beiden Wege nutzen, sonst läuft der Agent doppelt.
2. Im Plugins-Dialog das Plugin **„Snapmaker U1 Agent“** aktivieren, ebenso seine beiden Funktionen „Snapmaker U1 (Moonraker)“ und „U1“.
3. OrcaSlicer komplett beenden (Cmd+Q) und neu starten.

## 3. Drucker-Preset einrichten

1. Im Tab **Vorbereiten** das U1-Preset wählen, mit dem du druckst. Daneben auf **Bearbeiten** klicken.
2. Oben rechts den **Experten-Modus** einschalten.
3. **Grundlegende Informationen → Erweiterte Einstellungen**:
   - **Print Host:** `<U1-IP>` (IP des U1)
   - **Drucker-Agent:** „Snapmaker U1 (Moonraker)“
4. Mit dem **Disketten-Symbol** speichern.

## 4. Erste Verbindung

- Beim Installieren fragt OrcaSlicer, ob das Plugin Threads starten und die gesliceten Projekte lesen darf: mit **Ja** bestätigen (in der Alpha kommt die Frage nach den Threads erst beim ersten Verbinden).
- Den Zugriff auf das Netzwerk (den U1) beim ersten Verbinden ebenfalls erlauben. Die Rechte stehen danach im Plugins-Dialog und lassen sich dort ändern.
- Tab **Gerät** öffnen.
- Danach erscheinen Status, Temperaturen und Kamera. Der Tab **„U1“** zeigt die volle Druckeransicht.

## 5. Filamente

- **Sync-Knopf im Filament-Bereich** drücken. Ab OrcaSlicer 2.5 Beta weist das Plugin danach jedem Slot automatisch das passende Profil zu, z. B. Snapmaker PLA SnapSpeed oder PETG HF. Es richtet sich nach Hersteller, Typ, Untertyp und Farbe der Spule.
- Gibt es kein Profil des Herstellers, nimmt es ein Generic-Profil mit demselben Untertyp, z. B. Generic PLA Silk.
- Der Knopf **„Profile zuweisen“** im Tab „U1“ macht dasselbe, ohne neu zu synchronisieren.
- Hat ein Filament-Profil ungespeicherte Änderungen, fragt OrcaSlicer wie gewohnt, was damit passieren soll.
- In der Alpha geht das automatische Zuweisen nicht. Dort die Profile einmal von Hand wählen, danach ordnet der Sync die Slots ihnen zu.
- Die Slot-Zuordnung im Druckdialog funktioniert unabhängig davon.

## 6. Drucken

- **Druckauftrag senden** öffnen und wählen, welcher Slot welches Filament druckt.
- Zeitraffer, Flow-Kalibrierung und Bett-Nivellierung nach Wunsch einstellen.
- **Senden** lädt die Datei nur hoch, **Drucken** lädt hoch und startet den Druck mit den gewählten Optionen.

## Plugin aktualisieren

Die neue `u1_moonraker_agent.py` nach
`~/Library/Application Support/OrcaSlicer/orca_plugins/u1_moonraker_agent.py/`
kopieren (vorhandene Datei ersetzen) und OrcaSlicer neu starten. Alternativ erneut über **Install local plugin** installieren.

## Bekannte Grenzen (OrcaSlicer 2.5)

- **Drucker-Sync-Knopf** (oben rechts beim Drucker): bleibt zwar beim Preset, kann die vier Düsen des U1 aber nicht abgleichen. Für Filamente den Knopf im Filament-Bereich nehmen.
- **Profile zuweisen:** Ein Slot bekommt nur ein neues Profil, wenn er im Projekt schon ein Filament desselben Typs hat (nach dem Sync ist das so).
- **Kamera:** Einzelbilder (etwa 5 pro Sekunde), kein flüssiges Video.
- **Gerät-Tab:** zeigt nur einen Extruder. Alle vier Tools gibt es im Tab „U1“.

## Fehlersuche

- **Agent fehlt in der Liste „Drucker-Agent“:** Plugin im Plugins-Dialog aktivieren und OrcaSlicer neu starten.
- **„Speicher muss eingelegt werden“:** Prüfen, ob im Preset der Agent „Snapmaker U1 (Moonraker)“ gespeichert ist. Ältere Workaround-Dateien müssen weg: `~/Library/Application Support/OrcaSlicer/printers/797581801.json` und `Snapmaker U1.json`.
- **Logs:**
  - Plugin: `~/Library/Application Support/OrcaSlicer/log/python_*.log`, Zeilen beginnen mit `[u1_moonraker]`.
  - OrcaSlicer: `debug_*.log.0` im selben Ordner.
