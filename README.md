
zieh dir diesen kompletten chat rein und dann gehts hier weiter:
https://claude.ai/share/713dfce6-2bf0-4a82-9ad2-4178dff52381

21. Mai
Ich kann den geteilten Chat leider nicht abrufen.


Failed to fetch: https://claude.ai/share/713dfce6-2bf0-4a82-9ad2-4178dff52381
Failed to fetch: https://claude.ai/share/713dfce6-2bf0-4a82-9ad2-4178dff52381
Das hat leider nicht geklappt – geteilte Claude-Chats (claude.ai/share/...) sind durch die robots.txt gesperrt, und ich habe auch keine Verbindung zu früheren Unterhaltungen, an die ich anknüpfen könnte. Ich kann den Inhalt also nicht selbst auslesen.

Damit es trotzdem weitergehen kann, gibt es zwei einfache Wege:

Du kopierst den relevanten Inhalt des Chats direkt hier rein – entweder den ganzen Verlauf oder zumindest die Teile, auf denen wir aufbauen sollen. Plain Text reicht völlig.

Oder du fasst kurz zusammen, worum es ging und wo ihr stehengeblieben seid, plus was jetzt der nächste Schritt sein soll.

Sobald ich den Kontext habe, machen wir direkt weiter. Was war denn das Thema?

DarkCurrent_20260521_115546.png

DarkCurrent_20260521_115546_aggregated.csv
csv


DarkCurrent_20260521_115546_per_cycle.csv
csv


PS-2600A_Dark_Current_Characterization.py
py


PS-2600A_Pro.py
py


ich hab den code für mein spektrometer von pasco: soweit so gut, aber ich bekomme immer diesen fehler: C:\Users\Christian\Downloads>python usb_test.py SEARCH DEVICE FOUND: \\?\usb#vid_0945&pid_0002#7&2a6b6cb2&0&3#{9a9a65ee-a425-482d-ae44-80da1a1210c6} PASCO FOUND: \\?\usb#vid_0945&pid_000

pasted


output vom script: ================================================================= DARK CURRENT CHARACTERIZATION -- cyclic design 10 cycles x 24 steps Comparing spectral dark region vs optical black pixels =================================================================

pasted


output vom script: ================================================================= DARK CURRENT CHARACTERIZATION -- cyclic design 10 cycles x 24 steps Comparing spectral dark region vs optical black pixels =================================================================

pasted

reicht dir das.

21. Mai
Ja, das reicht völlig. Ich habe den ganzen Verlauf, die Charakterisierungs-Ergebnisse und den Plot. Lass mich erst deine konkreten Fragen beantworten, dann sehe ich mir den aktuellen Code an und baue die OB-Korrektur ein.

Zuerst der Code – ich muss schauen, was du aktuell hast.


Ran a command, viewed a file
Ran a command, viewed a file
Ich muss den mittleren Teil sehen, besonders den Hardware-Thread und den GUI-Aufbau.


Viewed 3 files
Viewed 3 files
Ich habe jetzt den vollständigen Code. Lass mich zuerst deine inhaltlichen Fragen beantworten, bevor ich umbaue.

Deine Fragen
1. Ist das 2500ms-Limit damit automatisch behoben?

Hier muss ich zwischen zwei verschiedenen Dingen unterscheiden, die leicht durcheinandergeraten:

Das, was wir früher als „Linearitätsgrenze" interpretiert haben, war kein echter Hardware-Knick. Schau dir Plot 1 nochmal an: Der parametrische Fit ist eine Gerade (Bias + Rate·t), aber auf der logarithmischen x-Achse sieht eine Gerade zwangsläufig wie eine nach oben gebogene Kurve aus. Die Daten liegen perfekt auf dem Fit — auch bei 2500ms und sogar bei den zwei Punkten darüber (das sind die orangen Quadrate ganz rechts, bei ~3000–4000ms). Es gibt in deinen aktuellen Daten keinen sichtbaren Sättigungsknick. Das R² von 0.9958 über den gesamten Bereich bestätigt das.

Die OB-Korrektur (corrected = raw - ob_mean) ist davon ohnehin komplett unabhängig. Sie subtrahiert keinen berechneten Dunkelstrom, sondern den tatsächlich gemessenen Dunkelwert dieses einen Frames. Egal wie nichtlinear der Dunkelstrom mit der Zeit ansteigen würde — die OB-Pixel machen exakt dieselbe Nichtlinearität mit, weil sie auf demselben Die sitzen. Das Modell „kann" gar nicht mehr falsch liegen, weil es kein Modell mehr ist.

Aber: Die OB-Korrektur löst nur das Dunkelstrom-Problem. Was sie nicht behebt, ist eine echte ADC-Sättigung des Nutzsignals. Wenn ein heller Peak die vollen ~3900 ADC erreicht, ist der Pixel-Topf physikalisch voll — da hilft kein Abziehen. Das 2500ms-Limit ist als Schutz gegen Übersteuerung des Nutzsignals also weiterhin sinnvoll, aber nicht mehr wegen Dunkelstrom-Nichtlinearität. Kurz: behalte das Limit, aber die Begründung dafür ändert sich.

2. Verfälschung im Spektrum oberhalb 2500ms?

Mit OB-Korrektur: für den Dunkelstrom-Anteil nein, das ist sauber. Die einzige reale Gefahr oberhalb von 2500ms ist, dass der Dunkelstrom selbst (~160 ADC bei 2500ms, Tendenz steigend) irgendwann einen merklichen Teil des 12-bit-Bereichs auffrisst und dir Dynamikumfang für echte Peaks wegnimmt. Das ist ein Argument fürs Limit, aber kein Korrektur-Fehler.

3. Temperaturkompensation — ja. Genau das ist der Clou. Die OB-Pixel sehen dieselbe Die-Temperatur wie die Spektralpixel, in jedem einzelnen Frame. Drift über die Session, Aufwärmen, Umgebungstemperatur — alles automatisch drin. Plot 4 zeigt es: bei dir war der Sensor schon im Gleichgewicht, aber der Mechanismus würde Drift sofort wegrechnen.

4. Auf Sensortemperatur schließen? Indirekt ja. Der OB-Mean bei langer Integrationszeit ist im Wesentlichen ein Thermometer in ADC-Counts. Du kannst aus deinen Daten sogar grob kalibrieren: Rate ≈ 41 ADC/s heißt, eine Verschiebung des OB-Means korreliert mit Temperatur. Eine absolute Temperatur in °C kriegst du ohne zweiten Referenzpunkt nicht, aber relative Drift („Sensor wird wärmer") ist klar ablesbar. Ich baue das GUI-Feld so, dass es den OB-Mean live anzeigt — das ist dein Thermometer-Proxy.

Jetzt baue ich das ein. Plan:

Hardware-Thread gibt zusätzlich zum Spektrum den OB-Mean mit (Header-Bytes 4–63, 30 × uint16, mit Slope/Offset-Korrektur aus deiner Charakterisierung)
Neue Config-Werte (OB + parametrischer Fallback)
Dark-Correction-Logik bekommt einen Modus-Umschalter: „Optical Black" (neu, default) vs. „Parametric" (alter Fallback)
Neues GUI-Anzeigefeld: Live-OB-Mean in ADC + grobe relative Temperatur-Tendenz
OB-Mean wird in den CSV-Export aufgenommen
Lass mich das Signal im Thread und die process_new_spectrum-Schnittstelle anschauen — das Signal trägt aktuell nur np.ndarray.


Find all signal usage to update emit signatures
Find all signal usage to update emit signatures
Jetzt habe ich alles. Ich baue um. Erst eine Arbeitskopie anlegen.


Copy file to working dir and check line endings
Copy file to working dir and check line endings
Datei hat CRLF (Windows). Ich konvertiere für die Bearbeitung auf LF und am Ende zurück.


Convert CRLF to LF for editing
Convert CRLF to LF for editing
Jetzt die Änderungen. 1. Config-Block — neue OB-Werte und Modus-Konstante:


Add optical black config values and dark correction mode
Add optical black config values and dark correction mode
2. Hardware-Thread — Signal erweitern, damit der OB-Mean mitgeschickt wird, und die Header-Bytes auswerten:


Edited 3 files
Edited 3 files
3. GUI — State-Flags und neues Anzeigefeld. Erst die Init-Flags:


Add dark mode state and OB tracking variables
Add dark mode state and OB tracking variables
Jetzt baue ich Row 2 des Control-Panels um: einen Modus-Umschalter (Optical Black / Parametric) und das Live-OB-Anzeigefeld. Die Bias/Rate-Spinboxen bleiben als Fallback erhalten.


Add dark mode selector and OB live readout to control panel
Add dark mode selector and OB live readout to control panel
4. Handler-Methoden — apply_dark_mode neu, und der OB-Readout. Ich füge sie neben toggle_dark_correction ein:


Add dark mode handler and OB readout update next to toggle_dark_correction
Add dark mode handler and OB readout update next to toggle_dark_correction
5. process_new_spectrum — neue Signatur (nimmt jetzt 3 Argumente) und die OB-Korrekturlogik:


Rewrite dark correction in process_new_spectrum to support OB mode
Rewrite dark correction in process_new_spectrum to support OB mode
6. CSV-Export — den OB-Mean als Metadatenzeile in den Spektrum-Export aufnehmen:


Add OB mean metadata to spectrum CSV export
Add OB mean metadata to spectrum CSV export
Jetzt die Syntax prüfen und kontrollieren, ob noch Stellen mit der alten current_integration_time_us-Referenz Probleme machen:


Compile-check and verify signal/integration references
Compile-check and verify signal/integration references
Syntax OK. Die current_integration_time_us im Thread bleibt korrekt (Thread-intern), und process_new_spectrum empfängt die Integrationszeit jetzt sauber über das Signal statt durch einen Querzugriff auf den Thread — das ist auch robuster.

Eine Detailprüfung noch: PyQt6 muss eine int-Signal-Payload an einen int-Slot binden. current_integration_time_us ist immer ein Python-int (kommt aus START_INTEGRATION_TIME_US bzw. int(...)-Casts), passt also. Schnelle Gegenprüfung der Threadlogik per AST-Test ohne echte Hardware:


Verify function signatures and OB pixel arithmetic
Verify function signatures and OB pixel arithmetic
Eine Kleinigkeit: apply_dark_mode wird sowohl von currentIndexChanged (sendet ein int-Argument) als auch direkt aufgerufen. Ich hatte *args eingeplant, aber AST zeigt nur self — das *args ist als vararg separat. Prüfe:


Confirm apply_dark_mode accepts varargs from signal
Confirm apply_dark_mode accepts varargs from signal
*args ist da — der Slot verträgt also sowohl das int von currentIndexChanged als auch den direkten Aufruf. Alles korrekt. Jetzt zurück auf CRLF konvertieren und ausliefern:


Ran a command, read a file
Ran a command, read a file
Fertig. Hier ist, was sich geändert hat und worauf du achten solltest.

Was eingebaut wurde
Config-Block. Deine Charakterisierungswerte sind jetzt drin: OB_CORRECTION_SLOPE = 1.008180, OB_CORRECTION_OFFSET = -0.6377, und der parametrische Fallback Bias = 61.57 / Rate = 40.94. Dazu die Header-Geometrie (Bytes 4–63 = 30 OB-Pixel) und ein Modus-Schalter mit Optical Black als Default.

Hardware-Thread. Das Spektrum-Signal trägt jetzt drei Werte: Pixel-Array, OB-Mean und Integrationszeit. Pro Frame werden die Header-Bytes 4–63 als 30 × uint16 ausgelesen und gemittelt — kein zweiter USB-Zugriff, der Header kam ja ohnehin immer mit.

Dark Correction. Im Modus „Optical Black" rechnet das Programm corrected = raw − (1.0082·ob_mean − 0.638). Kein Bias, keine Rate, keine Integrationszeit-Annahme. Der Modus „Parametric" bleibt als Fallback erhalten; die Bias/Rate-Spinboxen werden ausgegraut, wenn OB aktiv ist, damit klar ist, dass sie dann nichts tun.

GUI-Feld. Eine neue Zeile unter dem Control-Panel zeigt live: Dark Level: 64.32 ADC | Integration: 50.0 ms | Sensor temp (relative): baseline (cold). Der Temperatur-Hinweis zieht den integrationszeitbedingten Anteil ab, sodass der Restwert grob die Die-Temperatur widerspiegelt — relativ zum Charakterisierungs-Baseline, nicht absolut in °C.

CSV-Export. Spektrum-Exporte bekommen Kopfzeilen mit OB-Mean, Integrationszeit und Korrektur-Status.

Zwei Dinge, die du wissen solltest
Der OB_TEMP_REFERENCE_ADC = 61.5 ist mein Ansatz für den Baseline-Wert aus deiner Charakterisierung (OB-Mean bei kurzer Belichtung). Wenn du den Temperatur-Hinweis genauer haben willst, miss bei kaltem Sensor einen Frame bei 10 µs und trag den OB-Mean dort ein.

Zum 2500ms-Limit: Ich habe es bewusst nicht angefasst. Aus den genannten Gründen — die OB-Korrektur macht den Dunkelstrom temperatur- und nichtlinearitätssicher, aber das Limit schützt weiterhin den Dynamikumfang des Nutzsignals. Falls du es lockern willst, sag Bescheid, dann ist es eine Ein-Zeilen-Änderung an MAX_INTEGRATION_TIME_US.


Ps 2600a pro
PY 
können wir den balken für die anzeige der wellenlänge und wert auch im wasserfallspektrum haben?

21. Mai

Architektierte Cursor-Tracking und Intensitätsanzeige für Spektrogramm
Architektierte Cursor-Tracking und Intensitätsanzeige für Spektrogramm
Klar, das ist straight-forward. Ich brauche eine eigene InfiniteLine auf dem Heatmap-Canvas und einen zweiten Mouse-Proxy. Weil im Wasserfall die Y-Achse Frame-Index ist, kann der Cursor dann auch noch Frame + Intensität an dieser Stelle aus dem Buffer lesen.


Ran a command, edited a file
Ran a command, edited a file
Jetzt den Handler direkt unter handle_mouse_movement einhängen:


Edited a file, ran a command, read a file
Edited a file, ran a command, read a file
Was eingebaut wurde:

Im Heatmap-Canvas gibt es jetzt zwei InfiniteLines als Fadenkreuz — weiß gestrichelt, damit sie auf dem inferno-Colormap sichtbar sind. Ein zweiter SignalProxy auf dem Heatmap-Scene-Objekt schickt Mausbewegungen an handle_heatmap_mouse_movement.

Die Anzeige oben rechts (label_cursor_info) ist dieselbe wie im Scope-Tab, zeigt aber beim Hovern über den Wasserfall: Pixel, Wellenlänge in nm, Frame-Index und die Intensität an genau diesem Punkt — gelesen direkt aus dem heatmap_buffer. Frame 0 ist der neueste, höhere Indizes sind älter, genauso wie die Y-Achse aufgebaut ist.


Ps 2600a pro
PY 
1779364787355_image.png

README (1).md
755 lines

md

der wert sprengt das layout, es springt dann wenn dort ein zahlenwert drin steht.

ansonsten läuft es. wir müssen jetzt noch unsere readme anpassen, diese ist anbei mit den neuen wahrheiten.

21. Mai

Orchestrierte Layout-Fix und README-Aktualisierung mit neuen Features
Orchestrierte Layout-Fix und README-Aktualisierung mit neuen Features
Zwei Sachen — erst der Layout-Fix (schnell), dann die README.


LF working copy
LF working copy
Das Springen kommt daher, dass der Label beim ersten Wert die Höhe neu berechnet. Fix: feste Höhe setzen und sicherstellen, dass der Initialtext bereits die maximale Zeilenhöhe reserviert.


Edited a file, ran a command
Edited a file, ran a command
Gut. Jetzt die README. Ich lese sie durch und update dann alles auf einmal:


Ran a command, created a file, read a file
Ran a command, created a file, read a file
Layout-Fix: der Label bekommt jetzt setFixedHeight(32) und der Initialtext hat bereits die gleiche Struktur wie der Live-Text — damit reserviert Qt beim ersten Render die richtige Höhe und springt nicht mehr wenn der erste echte Wert reinkommt.

README — was sich geändert hat:

Der Dark Current-Abschnitt ist komplett neu geschrieben. Optical Black ist jetzt die primäre Methode mit eigenem Unterabschnitt inkl. der Korrelationswerte aus deiner Charakterisierung. Parametric folgt danach als Fallback. Das Live-Anzeigefeld hat einen eigenen Absatz.

Die Payload-Tabelle hat eine neue Zeile für Bytes 4–63 mit der Erklärung der OB-Pixel, und die ursprüngliche „Bytes 0–63 = Header/Metadata"-Beschreibung ist korrigiert.

Der 2.5-Sekunden-Abschnitt ist sachlich überarbeitet: die alte Aussage, dass das Limit durch das Dunkelstrom-Korrekturmodell erzwungen wird, ist gestrichen. Stattdessen steht da, dass OB-Korrektur keiner Zeitannahme bedarf und das Limit als konservative Betriebsgrenze erhalten bleibt.

Der Charakterisierungs-Script-Abschnitt ist neu — beschreibt den Zweck, den zyklischen Messansatz und die Outputs.

Die Konfigurationstabelle enthält jetzt alle neuen Konstanten (OB_CORRECTION_SLOPE, OB_CORRECTION_OFFSET, OB_PIXEL_COUNT, DEFAULT_DARK_MODE) und die korrigierten Defaultwerte (61.57 / 40.94 statt 60.5 / 45.0).


Ps 2600a pro
PY 

Readme
Document · MD 
1779365374926_image.png
das springt im layout leider immer noch. fixe das mal. (durch die zahlen in sensor temp) wo wir gerade dabei sind, können wir noch light und dark mode für das layout haben? und können wir noch context sensitive help für alle felder und controls haben, die nach iwie 2 sekunden sichtbar werden?

wenn du schon dabei bist, erstelle gleich noch eine referenze library für alle gängigen spektrallampen aus rein- und mischgas sowie 2/3/4 banden leuchtstoffröhren und die gängigen farben von reinen LEDs  wenn du weisst, was ich meine.

21. Mai

Orchestrierte umfangreiche GUI-Überhaul mit Layout-Fix, Theme-System und Spektralbibliothek
Orchestrierte umfangreiche GUI-Überhaul mit Layout-Fix, Theme-System und Spektralbibliothek
Alles klar. Jetzt schreibe ich alle Änderungen in einem Durchgang. Das sind 9 gezielte Operationen.


Edited 3 files
Edited 3 files
Jetzt die komplette neue Referenzbibliothek:


Replace reference library generator with full spectral library
Replace reference library generator with full spectral library
Jetzt __init__ — is_dark_mode State-Flag hinzufügen:


Add is_dark_mode flag to DashboardWindow init
Add is_dark_mode flag to DashboardWindow init
Theme-Button in die Verbindungsleiste und OB-Label Layout-Fix:


Add theme toggle button to connection bar
Add theme toggle button to connection bar
OB-Readout Label — alle 10 Spalten, Mindestbreite, Monospace für die Zahlen:


Edited 2 files
Edited 2 files
Jetzt alle Context-Help-Tooltips und den Theme-Button zur Mindesthöhen-Liste:


Add context help to all controls and include theme button in minimum height loop
Add context help to all controls and include theme button in minimum height loop
Jetzt toggle_theme und apply_theme Methoden sowie das angepasste update_ob_readout für den Dark Mode:


Add toggle_theme and apply_theme methods after apply_dark_mode
Add toggle_theme and apply_theme methods after apply_dark_mode
Und update_ob_readout — Zahlenformat auf feste Breite umstellen damit der Text nicht springt:


Edited a file, ran a command
Edited a file, ran a command
Schnell prüfen ob alle neuen Symbole erreichbar sind und die Referenzbibliothek korrekt generiert wird:


Test spectral generation logic
Test spectral generation logic
Gut — die Spektrum-Generierung läuft sauber, das erste Traceback ist nur der Stub-Versuch und irrelevant. Syntax ist OK. Jetzt ausgeben:


Ran a command, read a file
Ran a command, read a file
Hier ist was reingebaut wurde:

Layout-Fix — drei Maßnahmen zusammen: der Label spannt jetzt alle 10 Spalten (1, 10), hat setMinimumWidth(700) und setSizePolicy(Expanding, Fixed). Die Zahlenwerte im Text haben jetzt feste Feldbreiten ({ob:6.2f}, {integration_ms:7.1f}, {delta:+5.1f}) und die Temp-Strings werden mit Leerzeichen auf gleiche Länge aufgefüllt. Das verhindert, dass sich das Grid bei wechselnden Zahlenwerten neu berechnet.

Light / Dark Mode — Button in der Verbindungsleiste. Dark Theme nutzt die Catppuccin-Palette (dunkelblau-grau, keine reinen Schwarz-Weiß-Kontraste). Beim Umschalten wechseln auch die pyqtgraph-Plot-Hintergründe, Achsenfarben und Kurvenfarben. Die farbigen Info-Labels oben passen sich ebenfalls an.

Context Help — eine ContextHelp-Klasse mit QTimer (2000 ms) und QEvent-Filter auf jedem Widget. Nach 2 Sekunden Hovern erscheint ein detaillierter Tooltip direkt unter dem jeweiligen Control. Alle Buttons, Spinboxen, Combo-Boxen und der OB-Readout-Label haben eigene Texte.

Referenzbibliothek — 22 Spektren: Wasserstoff, Quecksilber, Natrium, Neon, Argon, Helium, Krypton, Xenon, Cadmium, Neon-Argon-Mix, Leuchtstoff 2-band / 3-band (Dreibandleuchtstoff) / 4-band, sowie LEDs in Rot / Orange / Amber / Gelb / Grün / Cyan / Blau / Violett / Weiß Warm-Neutral-Kalt. Gasentladungslinien als enge Gaußkurven (FWHM 0,5 nm), LED-Banden als breite (14–120 nm FWHM). Die alte Datei wird automatisch erkannt und beim nächsten Start ersetzt — einfach das Programm neu starten.


Ps 2600a pro
PY 
1779366218361_image.png
1779366245870_image.png
check mal die glitches im design im dark mode für den connect button und light mode, das ist abgeschnitten.

21. Mai

Identifizierte Styling-Konflikte und konzipierte Theme-aware Lösungen
Identifizierte Styling-Konflikte und konzipierte Theme-aware Lösungen
Ich sehe beide Probleme: der Connect-Button bekommt lightgray/lightcoral als inline-Style gesetzt, was die Theme-Stylesheet-Kaskade überschreibt (Widget-Level hat Vorrang vor App-Level). Der Theme-Button braucht eine Mindestbreite damit „Light Mode" nicht abgeschnitten wird.


Ran a command, viewed a file
Ran a command, viewed a file
Klare Struktur. Inline-Styles auf dem Connect-Button entfernen, stattdessen eine zentrale Methode _style_connect_button() die den Theme-Zustand kennt. Gleichzeitig dem Theme-Button eine Mindestbreite geben.


Edited 3 files
Edited 3 files
Jetzt die zentrale _style_connect_button-Methode hinzufügen und apply_theme so erweitern, dass es den Connect-Button ebenfalls aktualisiert:


Add _style_connect_button helper and call it from apply_theme
Add _style_connect_button helper and call it from apply_theme
apply_theme muss _style_connect_button am Ende aufrufen:


Viewed 2 files, edited a file
Viewed 2 files, edited a file
Python-Typenhinweis bool | None geht erst ab 3.10 — sicher machen:


Edited a file, ran a command, read a file
Edited a file, ran a command, read a file
Die beiden Fixes:

Connect-Button Glitch — das Problem war, dass setStyleSheet() direkt auf dem Widget sitzt und damit höhere Priorität hat als das App-Level-Stylesheet. In Dark Mode war der Button deshalb immer lightgray/lightcoral, egal was das Theme sagte. Die Lösung: alle drei Stellen (Init, connect_device, disconnect_device) rufen jetzt _style_connect_button() auf, das sowohl den Theme-Zustand als auch den Verbindungsstatus kennt. Im Dark Mode ist "Disconnect" jetzt ein sattes Rosa-Rot auf dunklem Grund, "Connect" ein neutrales Grau — beide bleiben lesbar.

„Light Mode" abgeschnitten — setMinimumWidth(120) auf dem Theme-Button. Der Button hat jetzt immer genug Platz für den längeren der beiden Texte.


Ps 2600a pro
PY 

reference_spectra.csv
csv


color_science.py
py


gui_cie_tab.py
py


gui_dashboard.py
py


gui_theme.py
py


spectrometer_core.py
py

GUI_CRI.png
GUI_Waterfall.png
GUI_LightMode.png
GUI_DarkMode.png

spectrometer_config.json
27 lines

json


app_config.py
105 lines

py


PS-2600A_Pro.py
39 lines

py


requirements.txt
10 lines

txt


# PASCO PS-2600A – Native WinUSB Spectrometer Dashboard A high-performance, real-time spectroscopy software suite for the PASCO PS-2600A spectrometer, providing direct low-level USB communication through the native Windows WinUSB stack — completely bypassing the official PASCO software ecosystem.

pasted

anbei mein aktueller code und die aktuelle readme.md für die version. kannst du die readme mal anpassen auf die aktuellen features?

11:34
Schönes Redesign — modulare Struktur, CIE/CCT/CRI-Tab, JSON-Config. Ich schaue mir die Module kurz an, damit die README exakt zu dem passt, was wirklich drin ist.


Ran 2 commands
Ran 2 commands
Wichtige Beobachtung: die Wavelength-Koeffizienten haben sich geändert — [75.45192816, 0.345838363, -2.33680103e-05, 1.22593755e-09] statt der alten Werte. Ich schaue noch kurz, was der CIE-Tab und summarize liefern:


Check summarize output, CIE tab features, dashboard tabs and exports
Check summarize output, CIE tab features, dashboard tabs and exports
Ich habe genug Kontext. Die README braucht ein größeres Update: drei Tabs (Scope/Heatmap/CIE), neuer CIE-CCT-CRI-Abschnitt, modulare Architektur, JSON-Config, Savitzky-Golay-Peakfinding, scipy/matplotlib-Abhängigkeiten, neue Wellenlängenkoeffizienten. Ich schreibe sie komplett neu.


Check for photometric features and peak detection method
Check for photometric features and peak detection method
Jetzt habe ich alles. Ich schreibe die README neu:


Created 2 files, ran a command
Created 2 files, ran a command

Rewritten README for modular redesign with CIE tab, JSON config, savgol peaks
README.md

Clear stale outputs from earlier turns

Script

Rewritten README for modular redesign
README.md
Done

Usage limit reached ∙ Resets 12:30 PM ∙ limits shared with Claude Code
Keep working




Claude is AI and can make mistakes. Please double-check responses.
Readme · MD
# PASCO PS-2600A – Native WinUSB Spectrometer Dashboard
 
A high-performance, real-time spectroscopy software suite for the PASCO PS-2600A spectrometer, providing direct low-level USB communication through the native Windows WinUSB stack — completely bypassing the official PASCO software ecosystem.
 
This project bridges the gap between raw hardware access and professional-grade laboratory analysis by combining reverse-engineered USB protocol control, high-speed real-time visualization, dark current modeling, full CIE 1931 colorimetry, spectral analysis, and export tooling into a single standalone Python application.
 
The software was built entirely from scratch through empirical USB traffic analysis and protocol reconstruction using Wireshark and USBPcap, as no public SDK documentation exists for raw USB transfers on the device.
 
---
 
# Screenshots
 
![GUI Plot Light Mode](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_LightMode.png "GUI Plot Light Mode")
![GUI Plot Dark Mode](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_DarkMode.png "GUI Plot Dark Mode")
 
![GUI Waterfall](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_Waterfall.png "GUI Waterfall")
 
![GUI CIE / CRI](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_CRI.png "GUI CIE Colorimetry")
 
![Zadig Driver Setup](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/Zadig_USB.png "Zadig Driver Setup")
 
---
 
# Project Structure
 
The application is split into focused modules, each with a single responsibility:
 
| Module | Responsibility |
|---|---|
| `PS-2600A_Pro.py` | Application entry point |
| `spectrometer_core.py` | WinUSB hardware layer, constants, acquisition thread |
| `gui_dashboard.py` | Main window, control panel, scope and heatmap tabs |
| `gui_cie_tab.py` | CIE 1931 chromaticity / CCT / CRI tab |
| `gui_theme.py` | Palettes, stylesheets, custom widgets |
| `color_science.py` | CIE 1931 colorimetry math (XYZ, CCT, Duv, CRI) |
| `app_config.py` | JSON-backed user preference persistence |
 
This separation keeps the hardware protocol, the colorimetry math, and the GUI fully decoupled. Each module can be tested or reused on its own.
 
---
 
# Key Features
 
---
 
## Multi-Threaded Architecture
 
- Dedicated acquisition thread fully isolated from the GUI thread
- Maintains a responsive interface even during very long integration times
- Stable operation up to the validated 2.5-second integration limit
- Continuous USB streaming without GUI blocking
---
 
## Persistent Configuration
 
All user settings are saved to `spectrometer_config.json` and restored on the next launch. This includes the theme, exposure, display range, dark correction mode, peak detection parameters, and the last position of the measurement cursors.
 
- Settings persist automatically the moment they are changed
- The file is created with sensible defaults on first launch
- Unknown keys are tolerated, so upgrading the software never invalidates an existing config file
- A corrupt or unreadable file falls back to defaults instead of crashing
---
 
## Acquisition Features
 
- Real-time live spectrum streaming at the maximum hardware-supported rate
- Adjustable integration time from 1 ms to 2500 ms
- Automatic exposure control with configurable ADC target
- Exposure adjustment ratio clamping
- Emergency saturation protection with drop-back
- Frame averaging for noise reduction
- Live frame rate indicator
---
 
## Visualization Modes
 
The interface presents three tabs.
 
### Scope Mode
 
- High-speed live spectral waveform rendering
- Wavelength axis in nanometers, ADC intensity axis
- Spectral-color gradient fill under the live trace
- Auto-scaling or fixed Y axis
- Real-time crosshair cursor with live pixel, wavelength and intensity readout
- Draggable A / B measurement cursors with delta-wavelength and delta-intensity readout
- Reference spectrum overlay from the built-in library
### Time-Lapse Heatmap (Waterfall Plot)
 
- Rolling spectral history visualization
- 100-frame history buffer
- Inferno colormap rendering
- Real-time temporal evolution analysis
- GPU-accelerated pyqtgraph rendering
- Full crosshair cursor readout: wavelength, frame index, and intensity at cursor position
### Color · CCT / CRI
 
A complete CIE 1931 colorimetry workspace driven by the live spectrum. See the dedicated section below.
 
---
 
## Colorimetry (CIE 1931)
 
The Color tab computes a full colorimetric analysis of the live spectrum in real time, using the CIE 1931 2-degree standard observer.
 
- CIE 1931 chromaticity diagram with the live measurement plotted on the spectral horseshoe
- Planckian (blackbody) locus overlay with reference temperature markers
- Correlated Color Temperature (CCT) via the McCamy approximation
- Duv — distance from the Planckian locus, indicating tint above or below blackbody
- CIE chromaticity coordinates (x, y) and tristimulus values (X, Y, Z)
- CRI: General Color Rendering Index (Ra) per CIE 13.3-1995
- Individual test color samples R1 through R8 shown as a bar chart
- Reference illuminant selected automatically: Planckian radiator below 5000 K, CIE D-series daylight at or above 5000 K
This turns the spectrometer into a working light-quality meter suitable for characterizing LEDs, fluorescent tubes, and other illuminants.
 
---
 
## Signal Processing
 
### Dark Current Correction
 
The software supports two independent correction modes, selectable in the GUI at runtime.
 
#### Mode 1 — Optical Black (default, recommended)
 
The PS-2600A transfer header contains 30 physically masked CCD pixels (optical black pixels) that are never exposed to light. These pixels accumulate dark charge under identical conditions as the spectral pixels — same silicon die, same temperature, same integration time — and are read out in every single frame as part of the standard USB transfer at no additional cost.
 
Their mean value provides a live, per-frame dark reference:
 
```math
I_{corrected} = \max(0,\ I_{raw} - (slope \cdot \overline{OB} + offset))
```
 
Where `slope` and `offset` are calibrated from a characterization run. For the reference unit:
 
```
OB_CORRECTION_SLOPE  = 1.008180
OB_CORRECTION_OFFSET = -0.6377
```
 
This approach automatically compensates for:
 
- Temperature-driven dark current drift during a session
- Warm-up transients after cold start
- Ambient temperature differences between sessions
- Long-term sensor aging
No integration time assumption is required. The correction is physically exact regardless of exposure duration or sensor temperature, because the reference is derived from the same physical frame.
 
#### Mode 2 — Parametric (fallback)
 
Classical two-parameter linear thermal model:
 
```math
I_{dark} = Bias + Rate \cdot Time
```
 
Where `Bias` is the static sensor offset at zero integration time, `Rate` is the thermal dark charge accumulation rate in ADC counts per second, and `Time` is the current integration time in seconds.
 
Values from the cyclic characterization run:
 
```
DEFAULT_DARK_BIAS_ADC         = 61.57
DEFAULT_DARK_RATE_ADC_PER_SEC = 40.94
```
 
This mode is provided as a fallback and for comparison. It produces accurate results within the validated linear regime but cannot compensate for temperature drift during a session.
 
#### Live Dark Level Display
 
The status bar continuously displays the optical black mean for each acquired frame, alongside the active integration time. A relative sensor temperature indicator derives the thermal component from the OB mean, providing an indirect die-temperature readout without any external thermometer.
 
### Hot Pixel / Despeckle Filter
 
- Median-kernel hot-pixel suppression
- Adjustable kernel width (odd values only)
- Real-time filtering
- Removes sensor artifacts and cosmic spike noise
---
 
## Analysis Tools
 
### Peak Finder
 
Peak detection uses a Savitzky-Golay smoothing pass followed by prominence-based selection (SciPy `find_peaks`).
 
- Savitzky-Golay pre-smoothing with configurable window and polynomial order
- Prominence threshold to reject noise ripples
- Minimum height and minimum inter-peak distance enforcement
- Configurable maximum peak count
- Automatic peak labeling with wavelength and intensity
### Spectral Reference Library
 
The reference library is a CSV file generated on first launch with a broad set of synthetic reference spectra:
 
- Gas discharge lamps: hydrogen, mercury, sodium, neon, argon, helium, krypton, xenon, cadmium
- Neon-argon mixed-gas sign tubes
- Fluorescent tubes: 2-band, 3-band (tri-phosphor), and 4-band phosphor blends
- Single-color LEDs: red, orange, amber, yellow, green, cyan, blue, violet
- White LEDs: warm, neutral, and cool (blue pump plus phosphor)
Any selected reference can be overlaid on the live spectrum as a dashed comparison trace. The file is plain CSV and can be edited or extended by hand.
 
---
 
## Export Features
 
- Single-frame spectrum export to CSV with embedded metadata (OB mean, integration time, correction mode)
- Full heatmap history export to CSV (frame × wavelength matrix)
- High-resolution PNG screenshot of the active plot tab
- All exports use timestamped filenames
---
 
## User Interface
 
- Light and Dark themes, switchable at runtime, with the choice persisted
- Context-sensitive help: hovering over any control for two seconds shows a detailed tooltip
- Spectral-color gradient rendering under the live trace
- Live frame rate display
---
 
## Hardware Layer
 
- Pure Python ctypes interface to WinUSB
- No `pyusb`, no `libusb`, no third-party USB abstraction layer
- Direct WinUSB API access
- Full reverse-engineered protocol implementation
- Native overlapped I/O support
---
 
# Requirements
 
---
 
## Operating System
 
- Windows 10 (64-bit)
- Windows 11 (64-bit)
The required WinUSB components (`winusb.dll`, `setupapi.dll`) are already included in Windows.
 
---
 
## Python Version
 
- Python 3.10 or newer
- 64-bit Python required
Python 3.13 is fully supported and recommended.
 
---
 
# Driver Installation (CRITICAL)
 
The official PASCO vendor driver blocks direct low-level USB access. The device MUST be reassigned to the WinUSB driver using Zadig.
 
---
 
## Installing WinUSB via Zadig
 
1. Download Zadig: https://zadig.akeo.ie/
2. Launch Zadig as Administrator
3. Open `Options → List All Devices`
4. Select `Spectrometer (VID 0945, PID 0002)`
5. Choose target driver `WinUSB`
6. Click `Replace Driver` (or `Install Driver`)
7. Confirm WinUSB is now shown as the active driver
---
 
## Important Note
 
After replacing the driver, PASCO SPARKvue and PASCO Capstone will no longer recognize the device. The original driver can be restored at any time through Device Manager.
 
---
 
# Python Dependencies
 
Install all required packages:
 
```bash
pip install -r requirements.txt
```
 
---
 
## External Packages
 
| Package | Minimum | Purpose |
|---|---|---|
| `PyQt6` | 6.5 | GUI framework and application event loop |
| `pyqtgraph` | 0.13 | High-speed plotting and heatmap rendering |
| `numpy` | 1.24 | Numerical processing and filtering |
| `scipy` | 1.10 | Savitzky-Golay smoothing and peak detection |
| `matplotlib` | 3.7 | Optional — renders the CIE chromaticity fill |
 
`matplotlib` is optional. The CIE tab works without it and falls back to a pure-numpy polygon test for the chromaticity diagram fill.
 
---
 
## Standard Library Modules Used
 
No installation required: `ctypes`, `struct`, `time`, `os`, `sys`, `csv`, `json`, `datetime`, `collections`.
 
---
 
# Installation
 
Clone the repository:
 
```bash
git clone https://github.com/doctormord/Pasco-PS-2600A-Spectrometer
cd Pasco-PS-2600A-Spectrometer
```
 
Install dependencies:
 
```bash
pip install -r requirements.txt
```
 
Launch the application:
 
```bash
python PS-2600A_Pro.py
```
 
---
 
# First Launch
 
On first launch:
 
- `spectrometer_config.json` is created with default settings
- `reference_spectra.csv` is generated with the full reference spectrum library
- Use the **Scan USB** button to enumerate devices
- Select the PASCO spectrometer entry and click **Connect**
To regenerate the reference library (for example after upgrading), delete `reference_spectra.csv` and restart.
 
---
 
# Wavelength Calibration
 
The pixel-to-wavelength conversion uses a cubic calibration polynomial:
 
```python
WAVELENGTH_COEFFS = [
    75.45192816,
    0.345838363,
    -2.33680103e-05,
    1.22593755e-09
]
```
 
The wavelength mapping is:
 
```math
\lambda(i) = C_0 + C_1 i + C_2 i^2 + C_3 i^3
```
 
Where `i` is the pixel index and `λ(i)` is the wavelength in nanometers.
 
The calibration table is computed once during startup and reused throughout runtime. Recalibration against known emission lines (mercury at 546.07 nm, hydrogen Balmer alpha at 656.28 nm) is recommended if absolute wavelength accuracy better than 1 nm is required.
 
---
 
# Reference Spectrum Library
 
A file named `reference_spectra.csv` is automatically generated in the working directory.
 
## CSV Format
 
- Semicolon-delimited
- First column: `Wavelength_nm`
- Each additional column is one reference spectrum
- Each row corresponds to one spectrometer pixel
```csv
Wavelength_nm;Hydrogen (H);Mercury (Hg);LED White Warm;...
130.0;0;0;0;...
130.3;0;0;0;...
```
 
New spectra added as columns appear automatically in the GUI library selector after restart.
 
---
 
# USB Protocol Architecture & Reverse Engineering
 
The PASCO PS-2600A communicates through a vendor-specific USB protocol wrapped inside the Windows WinUSB stack. No official low-level protocol documentation exists. The complete communication protocol was reconstructed entirely from live USB traffic captures.
 
---
 
# Reverse Engineering Process
 
The protocol was reverse-engineered using:
 
- Wireshark
- USBPcap
- Differential USB traffic analysis
- Timestamp correlation
- Iterative packet replay testing
Traffic between the official PASCO software and the hardware was captured and filtered for `URB_CONTROL` and `URB_BULK` transfers. This allowed reconstruction of the internal acquisition state machine.
 
---
 
# Initial Access Problem
 
Early attempts to initialize the device consistently failed with `ERROR_ACCESS_DENIED` when calling `CreateFileW`, even under Administrator privileges.
 
The issue was ultimately traced to a critical WinUSB requirement: `FILE_FLAG_OVERLAPPED` must be passed during device handle creation.
 
Without this flag, `WinUsb_Initialize()` fails, handles are rejected, and asynchronous USB access breaks. This requirement is documented by Microsoft but easy to overlook.
 
---
 
# Device Identification
 
| Parameter | Value |
|---|---|
| VID | `0x0945` |
| PID | `0x0002` |
 
Observed WinUSB interface GUID:
 
```text
{DEE824EF-729B-4A0E-9C14-B7117D33A817}
```
 
Secondary GUID observed during early captures:
 
```text
{9a9a65ee-a425-482d-ae44-80da1a1210c6}
```
 
---
 
# Transfer Topology
 
## Control Transfers (Endpoint 0x00)
 
Used for device initialization, exposure control, triggering, and legacy status polling.
 
```text
0x40 → Vendor OUT
0xC0 → Vendor IN
```
 
## Bulk Transfers (Endpoint 0x82)
 
Used for spectral payload transfer, synchronization packets, and inter-frame drain packets.
 
---
 
# Command Set
 
| Request Code | Direction | Purpose |
|---|---|---|
| `0x01` | OUT | Device initialization |
| `0x02` | OUT | Set integration time |
| `0x09` | OUT | Trigger acquisition |
| `0x83` (`131`) | IN | Legacy status polling (unused in final loop) |
 
---
 
# Integration Time Encoding
 
Integration time is transmitted in microseconds as a 32-bit integer. Because USB setup packets only provide 16-bit `wValue` and `wIndex` fields, the value must be split:
 
```python
low_word  = microseconds & 0xFFFF
high_word = (microseconds >> 16) & 0xFFFF
 
usb_control_transfer_out(
    CMD_SET_EXPOSURE,
    value=low_word,
    index=high_word
)
```
 
---
 
# Actual Acquisition Cycle
 
The final validated acquisition sequence is:
 
```text
1. BULK IN  0x82  28 bytes    drain previous cycle
2. CTRL OUT 0x00  REQ=0x09    trigger acquisition
3. BULK IN  0x82  7360 bytes  spectral payload
4. BULK IN  0x82  28 bytes    trailing drain packet
5. CTRL OUT 0x00  REQ=0x02    set next exposure
```
 
---
 
# The 28-Byte Drain Packet Problem
 
One of the most critical discoveries during reverse engineering was that the device uses small 28-byte bulk transfers as synchronization acknowledgements. These packets MUST be drained every cycle.
 
Skipping them causes pipeline desynchronization, stale frame buffering, alternating zero frames, ghosting artifacts, and dropped acquisitions. This issue was invisible until full USB captures were analyzed frame-by-frame.
 
---
 
# Payload Structure
 
Main payload size: 7360 bytes.
 
| Bytes | Purpose |
|---|---|
| `0–3` | Padding (always zero) |
| `4–63` | 30 optical black pixels (uint16 LE) |
| `64–7359` | Spectral ADC payload (3648 × uint16 LE) |
 
## Optical Black Header Pixels
 
The first 64 bytes of every transfer were initially assumed to be device metadata. Systematic warm-up analysis revealed that bytes 4–63 carry 30 physically masked CCD pixels that track dark current identically to the spectral region.
 
Correlation analysis across 10 measurement cycles at 24 integration times confirmed:
 
```
Correlation slope  : 1.008180  (ideal: 1.0)
Correlation offset : -0.6377 ADC
R-squared          : 0.999943
```
 
These pixels are now used as the primary per-frame dark reference in Optical Black correction mode.
 
## Spectral Data
 
The spectral payload contains 3648 unsigned 16-bit little-endian integers (`<H`). The ADC range is 0–4095 (12-bit).
 
---
 
# Legacy Polling Discovery
 
During early experimentation, request `0x83` was used for polling, and a non-zero return value appeared to indicate acquisition readiness.
 
However, later investigation revealed that the final acquisition synchronization is actually driven by the bulk pipe itself, not by status polling. The polling mechanism was therefore removed from the final acquisition loop.
 
---
 
# Constraints & Sensor Linearity
 
## The 2.5 Second Integration Limit
 
Empirical characterization of the sensor established a hard integration time ceiling at 2500 ms.
 
During dark-current analysis with a fully shielded optical path, the parametric dark model `I_dark = Bias + Rate * t` fitted the measured data with R² = 0.9958 across the full characterization range.
 
Beyond approximately 2.5 seconds, onboard firmware signal limiting behavior has been observed in some operating conditions. To keep the spectral dynamic range fully available for actual signal peaks and to stay within the validated parametric model regime, the ceiling is retained.
 
This limit is relevant primarily when using Parametric dark correction mode. In Optical Black mode, the correction is derived from the frame itself and does not depend on any integration time assumption, so model accuracy above 2.5 seconds is not a factor. The ceiling remains in place as a conservative operating boundary regardless of correction mode.
 
```python
MAX_INTEGRATION_TIME_US = 2_500_000
```
 
---
 
# Configuration Reference
 
Hardware and algorithm constants are centralized in `spectrometer_core.py`. User-facing preferences live in `spectrometer_config.json` and are managed by `app_config.py`.
 
## Core Constants (`spectrometer_core.py`)
 
| Constant | Default | Description |
|---|---|---|
| `MIN_INTEGRATION_TIME_US` | `1000` | Minimum integration time (µs) |
| `MAX_INTEGRATION_TIME_US` | `2500000` | Maximum integration time (µs) |
| `START_INTEGRATION_TIME_US` | `20000` | Initial exposure time (µs) |
| `AUTO_EXP_TARGET_ADC` | `3400` | Auto-exposure ADC target |
| `AUTO_EXP_DEADZONE_ADC` | `100` | Auto-exposure deadzone |
| `AUTO_EXP_MIN_RATIO` | `0.2` | Minimum adjustment ratio |
| `AUTO_EXP_MAX_RATIO` | `5.0` | Maximum adjustment ratio |
| `AUTO_EXP_EMERGENCY_DROP_RATIO` | `0.2` | Saturation emergency reduction |
| `DEFAULT_DARK_BIAS_ADC` | `61.57` | Parametric model: bias offset |
| `DEFAULT_DARK_RATE_ADC_PER_SEC` | `40.94` | Parametric model: thermal rate |
| `OB_CORRECTION_SLOPE` | `1.008180` | Optical black calibration slope |
| `OB_CORRECTION_OFFSET` | `-0.6377` | Optical black calibration offset |
| `OB_PIXEL_COUNT` | `30` | Masked header pixels used as dark reference |
| `DEFAULT_DARK_MODE` | `optical_black` | Active correction mode at startup |
| `ADC_SATURATION_THRESHOLD` | `3800` | Saturation detection threshold |
| `PEAK_MIN_DISTANCE_PIXELS` | `40` | Minimum distance between detected peaks |
| `MIN_PEAK_HEIGHT_ADC` | `15.0` | Minimum peak intensity |
| `HEATMAP_HISTORY_SIZE` | `100` | Waterfall frame buffer depth |
| `WAVELENGTH_COEFFS` | see above | Cubic calibration polynomial |
 
## User Preferences (`spectrometer_config.json`)
 
| Key | Default | Description |
|---|---|---|
| `theme` | `dark` | Interface theme: `dark` or `light` |
| `exposure_ms` | `20.0` | Integration time |
| `frames_to_average` | `1` | Frame averaging count |
| `auto_exposure` | `false` | Auto-exposure enabled |
| `reference_library` | `None` | Selected reference overlay |
| `x_min_nm` / `x_max_nm` | `380` / `1050` | Display wavelength range |
| `auto_y` | `true` | Auto-scale Y axis |
| `y_max_adc` | `4000` | Fixed Y-axis maximum |
| `show_peaks` | `false` | Peak finder enabled |
| `measure_mode` | `false` | Measurement cursors enabled |
| `dark_correction` | `false` | Dark correction enabled |
| `dark_mode` | `optical_black` | Correction mode |
| `dark_bias_adc` | `61.57` | Parametric bias |
| `dark_rate_adc_per_s` | `40.94` | Parametric rate |
| `hot_pixel_filter` | `false` | Despeckle filter enabled |
| `smoothing_width` | `5` | Despeckle kernel width |
| `peak_savgol_window` | `11` | Savitzky-Golay window (odd) |
| `peak_savgol_order` | `3` | Savitzky-Golay polynomial order |
| `peak_prominence` | `50.0` | Peak prominence threshold (ADC) |
| `peak_min_height` | `15.0` | Minimum peak height (ADC) |
| `peak_min_distance_px` | `40` | Minimum peak separation (pixels) |
| `peak_max_count` | `3` | Maximum peaks to label |
| `measure_a_nm` / `measure_b_nm` | `500` / `600` | Last cursor positions |
 
---
 
# Dark Current Characterization Script
 
A standalone characterization script (`PS-2600A_Dark_Current_Characterization.py`) is included for measuring and validating the dark current parameters of a specific unit.
 
The script uses a cyclic measurement design to decouple thermal self-heating from integration time effects: instead of measuring all repetitions at one time step before advancing, one frame is acquired at each integration step per cycle, and the full cycle is repeated N times. Thermal drift is therefore distributed equally across all time steps rather than accumulating on the long-exposure end.
 
The script outputs:
 
- Fitted parametric model parameters (Bias, Rate, R²) for the Parametric correction mode
- Optical black correlation analysis (slope, offset, R²) confirming OB pixel validity
- A recommendation for which correction mode is appropriate based on the data
- Per-cycle thermal convergence data to assess whether measurements were taken at thermal equilibrium
Run the characterization with the sensor input fully sealed (lens cap or equivalent) at the ambient temperature of normal operation. A full run at the default settings takes approximately 15 minutes.
 
---
 
# File Output
 
All exports are timestamped and saved in the working directory.
 
| Filename Pattern | Content |
|---|---|
| `spectrum_data_YYYYMMDD_HHMMSS.csv` | Current averaged spectrum with OB and correction metadata |
| `heatmap_data_YYYYMMDD_HHMMSS.csv` | Full waterfall history (frame × wavelength matrix) |
| `spectrum_plot_YYYYMMDD_HHMMSS.png` | Scope tab screenshot |
| `heatmap_plot_YYYYMMDD_HHMMSS.png` | Heatmap tab screenshot |
| `reference_spectra.csv` | Reference spectrum library |
| `spectrometer_config.json` | Persistent user preferences |
| `DarkCurrent_YYYYMMDD_HHMMSS.png` | Characterization plots |
| `DarkCurrent_YYYYMMDD_HHMMSS_aggregated.csv` | Aggregated characterization data |
| `DarkCurrent_YYYYMMDD_HHMMSS_per_cycle.csv` | Per-cycle characterization data |
 
---
 
# Technical Summary
 
This project demonstrates that the PASCO PS-2600A can be fully operated through native WinUSB access without relying on proprietary vendor software.
 
The implementation includes:
 
- Complete reverse-engineered USB protocol support
- Direct WinUSB communication via ctypes
- Real-time spectroscopy with live streaming
- Dual-mode dark current correction: per-frame optical black and parametric fallback
- Live sensor die temperature readout via optical black mean
- Full CIE 1931 colorimetry: CCT, Duv, chromaticity coordinates, and CRI
- Three visualization modes: scope, waterfall, and color analysis
- Savitzky-Golay peak detection with prominence filtering
- Spectral reference library covering discharge lamps, fluorescent tubes, and LEDs
- Persistent JSON-backed configuration
- Light and dark themes with context-sensitive help
- CSV and PNG export infrastructure
- A modular codebase separating hardware, colorimetry, and GUI
All functionality operates entirely in user-space Python with no proprietary SDK dependencies.
 
---
 
# License
 
This project is intended for educational, scientific, and reverse-engineering research purposes.
 
The PASCO PS-2600A hardware and associated trademarks belong to PASCO Scientific. This repository is an independent community reverse-engineering effort and is not affiliated with PASCO Scientific.
 




























