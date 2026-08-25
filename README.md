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

**Energiekosten inkl. Grundpreis, exkl. Netzgebühren und Abgaben.**

Netzgebühren, Steuern und Abgaben werden nicht geschätzt und nicht als pauschale Werte hinterlegt.

## Tarifhistorie

Kosten werden immer mit der zum jeweiligen Verbrauchszeitpunkt gültigen Tarifperiode berechnet. Ein aktueller Preis wird nicht rückwirkend auf historische Verbrauchsdaten angewendet.

Die bestätigte Standard-Tarifhistorie dieser Installation ist:

| gültig ab | Anbieter / Tarif | Arbeitspreis | Grundpreis |
| --- | --- | ---: | ---: |
| 24.12.2024 | E.ON Energie Österreich – historischer Tarif | 0,264 EUR/kWh | 5,40 EUR/Monat |
| 01.10.2025 | E.ON Energie Österreich – E.ON ÖkoStrom Treue | 0,152388 EUR/kWh | 2,754 EUR/Monat |

Die Tarifhistorie kann in den Integrationsoptionen als JSON angepasst oder erweitert werden. Für andere Installationen müssen die dort hinterlegten Standardwerte vor Nutzung der Kostenstatistik geprüft und angepasst werden.

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
7. Für einen größeren Historienimport die Integrationsoptionen öffnen und den einmaligen Backfill aktivieren.

## Sicherheit

Keine Zugangsdaten, Cookies, Session-Werte, Tokens, ViewState-Werte, Zählpunktnummern oder Meter-IDs gehören in dieses Repository oder in Issues/Logs. Zugangsdaten werden ausschließlich lokal in Home Assistant eingegeben.

## Technischer Hinweis

Der Zugriff verwendet den beobachteten LINZ-NETZ-Webportal-Flow mit SSO sowie JSF-/PrimeFaces-Formularen und -Paginierung. Es wird keine nicht bestätigte öffentliche API behauptet oder erfunden.

## Haftung

Nutzung auf eigene Verantwortung. Portal- und Tarifänderungen müssen geprüft werden; insbesondere sind Kostenwerte ohne Netzgebühren und Abgaben keine vollständige Stromrechnung.
