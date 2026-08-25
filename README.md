# LINZ Netz Energy

Inoffizielle Community-Integration für Home Assistant zur perspektivischen Übernahme von Stromverbrauchsdaten aus dem LINZ-NETZ-Serviceportal.

> **Wichtig:** Dies ist keine offizielle Integration von LINZ NETZ. Eine offiziell dokumentierte öffentliche LINZ-NETZ-Endkunden-API ist derzeit nicht bestätigt. Der Zugriff basiert auf dem Webportal und kann durch Änderungen an SSO, JSF-/PrimeFaces-Strukturen, Formularfeldern oder URLs jederzeit brechen.

## Ziel

Die Integration soll verzögert bereitgestellte Viertelstunden-Energiewerte in kWh aus dem LINZ-NETZ-Onlineportal abrufen, auf Stundenwerte aggregieren und als externe Home-Assistant-Langzeitstatistik bereitstellen.

Geplanter Statistik-Identifier:

`linznetz_energy:energy_consumption`

## Status

Der aktuelle Stand ist ein Prototyp. Die HACS-/Home-Assistant-Struktur ist vorbereitet, der automatische Portal-Login und der Datenimport müssen jedoch noch gegen ein reales Benutzerkonto und bekannte Portalwerte validiert werden.

## Installation über HACS

1. HACS öffnen.
2. Dieses Repository als benutzerdefiniertes Repository hinzufügen.
3. Kategorie **Integration** wählen.
4. `LINZ Netz Energy` installieren.
5. Home Assistant bei Bedarf neu starten.
6. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach `LINZ Netz Energy` suchen.

## Sicherheit

Keine Zugangsdaten, Cookies, Session-Werte, Tokens, ViewState-Werte, Zählpunktnummern oder Meter-IDs gehören in dieses Repository oder in Issues/Logs. Zugangsdaten werden ausschließlich lokal in Home Assistant eingegeben.

## Technischer Hinweis

Der aktuelle Prototyp verwendet das LINZ-NETZ-Serviceportal und beobachtete JSF-/PrimeFaces-Mechanismen. Es wird keine nicht bestätigte öffentliche API behauptet oder erfunden.

## Haftung

Nutzung auf eigene Verantwortung. Portaländerungen können die Integration jederzeit beeinträchtigen.
