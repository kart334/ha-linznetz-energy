# LINZ Netz Energy

Inoffizielle Community-Integration für Home Assistant zur Übernahme verzögert bereitgestellter Stromverbrauchsdaten aus dem LINZ-NETZ-Serviceportal.

> **Wichtig:** Dies ist keine offizielle Integration von LINZ NETZ. Eine offiziell dokumentierte öffentliche LINZ-NETZ-Endkunden-API ist nicht bestätigt. Der Zugriff basiert auf dem Webportal und kann durch Änderungen an SSO, JSF-/PrimeFaces-Strukturen, Formularfeldern oder URLs jederzeit brechen.

## Funktionsumfang

Die Integration liest Viertelstunden-Energiewerte in kWh aus dem LINZ-NETZ-Serviceportal, lädt paginierte Tage vollständig, aggregiert die Werte auf Stunden und schreibt sie als externe Home-Assistant-Langzeitstatistiken.

Statistik-Identifier:

- `linznetz_energy:energy_consumption` – **LINZ NETZ Stromverbrauch**, kWh
- `linznetz_energy:energy_cost` – **LINZ NETZ Energiekosten**, EUR

Zusätzliche Gerätesensoren:

- Verbrauch gestern
- Kosten gestern
- Letzter Import

Die Kostenstatistik bedeutet ausdrücklich:

**Energiekosten inkl. konfiguriertem Grundpreis, exkl. Netzgebühren und Abgaben.**

Netzgebühren, Steuern und Abgaben werden nicht geschätzt und nicht als pauschale Werte hinterlegt.

## Tarifhistorie

Tarifdaten sind **installations- und vertragsabhängig**. Dieses öffentliche Repository enthält bewusst keine Anbieter-, Vertrags- oder kundenspezifischen Tarifdefaults.

Die Integration unterstützt eine frei konfigurierbare Tarifhistorie. Nutzer müssen ihre eigenen Vertragsdaten prüfen und in den Integrationsoptionen hinterlegen. Ein aktueller Tarif wird nicht rückwirkend auf frühere Zeiträume angewendet; jede Verbrauchsstunde wird mit der zu diesem Zeitpunkt gültigen Tarifperiode berechnet.

Eine Tarifperiode verwendet logisch folgende Felder:

```text
valid_from: YYYY-MM-DD
energy_price: individueller Arbeitspreis in EUR/kWh
base_price_month: individueller Grundpreis in EUR/Monat
provider: optionaler Anbietername
name: optionaler Tarifname
```

Ist keine Tarifhistorie konfiguriert, funktioniert der Verbrauchsimport weiterhin. Kostenstatistik und der Sensor „Kosten gestern“ bleiben dann ohne berechenbaren Wert, bis eigene Tarifdaten hinterlegt wurden.

### Grundpreis-Verteilung

Der monatliche Grundpreis wird nicht auf eine einzelne Stunde gebucht. Er wird anteilig auf die tatsächlichen Stunden des jeweiligen Kalendermonats verteilt. Dabei werden auch Monate mit Sommer-/Winterzeitwechsel korrekt mit 23-/25-Stunden-Tagen berücksichtigt.

Bei einem Tarifwechsel innerhalb eines Monats erhält jede Stunde den Grundpreisanteil der für diese Stunde gültigen Tarifperiode. Dadurch wird der jeweilige Monatsgrundpreis zeitanteilig und nachvollziehbar abgebildet.

## Historie und Backfill

Der normale Coordinator-Refresh lädt absichtlich nur einen kleinen Überlappungszeitraum erneut, damit bestehende Werte korrigiert werden können, ohne bei jedem Lauf die gesamte Historie vom Portal abzurufen.

Für einen einmaligen historischen Backfill gibt es in den Integrationsoptionen:

- `Backfill-Zeitraum` bis maximal **395 Tage** (rund 13 Monate)
- `Backfill einmalig jetzt ausführen`

Der Standard bleibt konservativ bei 30 Tagen. Ein 395-Tage-Backfill wird **nicht automatisch bei jedem Update** gestartet; er muss über die Optionen einmalig angefordert werden. Danach wird die Anforderung automatisch zurückgesetzt.

Energie- und Kostenstatistiken nutzen Home Assistants offiziellen External-Statistics-Import. Re-Imports schreiben identische Zeitstempel erneut und korrigieren damit vorhandene Werte ohne direkten Datenbank- oder `.storage`-Zugriff.

## Management-Auswertungen

Die Langzeitstatistiken sind die Basis für spätere Auswertungen wie:

- laufender Monat / laufendes Jahr
- Vergleich zum Vormonat
- YTD gegen Vorjahr
- Jahreshochrechnung Verbrauch und Kosten

Dafür werden bewusst nicht für jede Kennzahl zusätzliche Template-Sensoren erzeugt. Home Assistant kann diese Auswertungen aus den Langzeitstatistiken bzw. darauf aufbauenden Dashboards ableiten. Historische Kosten bleiben dabei tarifperiodengerecht.

## Installation über HACS

1. HACS öffnen.
2. Dieses Repository als benutzerdefiniertes Repository hinzufügen.
3. Kategorie **Integration** wählen.
4. `LINZ Netz Energy` installieren bzw. aktualisieren.
5. Home Assistant neu starten.
6. Unter **Einstellungen → Geräte & Dienste** die Integration hinzufügen bzw. nach einem Update neu laden.
7. Eigene Tarifdaten in den Integrationsoptionen hinterlegen, wenn die Kostenstatistik genutzt werden soll.
8. Für einen größeren Historienimport die Integrationsoptionen öffnen und den einmaligen Backfill aktivieren.

## Sicherheit und Datenschutz

Zugangsdaten bleiben lokal in Home Assistant. Keine Zugangsdaten, Cookies, Session-Werte, Tokens, ViewState-Werte, Zählpunktnummern, Meter-IDs, Kundennummern oder persönliche Portalexporte gehören in dieses Repository, in Issues oder Logs.

Lokale Debug-Dumps, HAR-Dateien, Portalantworten und Session-Dateien sollen ebenfalls nicht committed werden; entsprechende typische Dateinamen und Verzeichnisse sind über `.gitignore` ausgeschlossen.

## Technischer Hinweis

Der Zugriff verwendet den beobachteten LINZ-NETZ-Webportal-Flow mit SSO sowie JSF-/PrimeFaces-Formularen und -Paginierung. Es wird keine nicht bestätigte öffentliche API behauptet oder erfunden.

Da dieses Repository gezielt eine Integration für LINZ NETZ bereitstellt, sind der Dienstname und der technische Portal-Endpunkt Bestandteil des Integrationscodes. Sie sind keine privaten Hausdaten. Das Verbergen dieser Zielplattform würde die Integration nicht sicherer machen und wäre mit dem Zweck eines öffentlichen Community-Repositories nicht vereinbar.

## Haftung

Nutzung auf eigene Verantwortung. Portal- und Tarifänderungen müssen geprüft werden; insbesondere sind Kostenwerte ohne Netzgebühren und Abgaben keine vollständige Stromrechnung.
