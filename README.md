# Mercatorum Didattiche Monitor

Monitor personale per controllare periodicamente le didattiche sincrone programmate su Universitas Mercatorum e sincronizzarle con un calendario Google dedicato.

## Sicurezza

Il repository non contiene credenziali in chiaro.

Le credenziali Mercatorum, la chiave del service account Google, la chiave di cifratura dello stato ed eventuali token di notifica devono essere configurati esclusivamente tramite **GitHub Actions Secrets**.

Lo stato persistente viene salvato come `state.enc`, cifrato con una chiave custodita nei Secrets. Il vecchio `state.json` in chiaro non deve essere committato.

I log del workflow omettono i dettagli delle lezioni e il workflow non pubblica artifact diagnostici.

## Esecuzione

Il workflow può essere avviato manualmente e viene eseguito automaticamente ogni 5 minuti tramite GitHub Actions.
